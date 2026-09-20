"""每日管道：闸门 → 同步日历 → 抓取 → 计算 → 三事务写入（§7.2 / §9.1.2）。

顺序是 §9.1.4 第 1 条写死的：**同步日历 → 闸门判定 → 抓取 → 计算**。

退出语义（§7.2 的那张表）决定告警不告警，所以它在代码里必须是**一处**：

===========================  ====================  =========  ========
情形                          ``runs.status``       exit code  告警
===========================  ====================  =========  ========
正常写入                      ``ok``                0          否
非交易日                      ``skipped_holiday``   0          否
未到收盘 + settle_minutes     ``skipped_too_early`` 0          否
本日已有 ok（条件重试跳过）    ``skipped_already_done`` 0       否
仅事件抓取失败（§3.5(4)）     ``ok_events_stale``   **0**      **否**
基准 bar 落后                 ``stale_vendor``      1          是
部分标的落后 / 脏数据 / 降级   ``partial``           1          是
计算 / 写库异常               ``failed``            1          是
===========================  ====================  =========  ========

**``ok_events_stale`` 那一行是整张表里最容易写错的。** 把事件抓取失败记成
``partial`` 会让 §7.1 的条件重试「本 session 已有 ok 就跳过」**不跳过**，
于是夏令时那四跑会全部执行完整管道 —— 而 18:40 那跑若撞上限流降级到 Stooq，
**好数据会被更粗的源静默覆盖**。一个可选的装饰性指标，就这样获得了
静默污染核心价格序列的能力。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pipeline.calendar_gate import (
    ET,
    interior_gaps,
    last_settled_session,
    stale_symbols,
    when_to_run,
)
from pipeline.compute import build_windows, compute_metrics, compute_strength
from pipeline.config import load_config
from pipeline.fetch import fetch_window, restrict_to_sessions
from pipeline.fetch_events import distances_for, fetch_symbol_events, should_refresh
from pipeline.sessions import lookback_window
from pipeline.store import (
    connect,
    data_transaction,
    delete_events_window,
    diff_prices,
    finish_run,
    insert_events,
    replace_strength,
    start_run,
    touch_fetch_state,
    upsert_metrics,
    upsert_prices,
)
from pipeline.store import (
    sync_symbols as write_symbols,
)
from pipeline.sync_sessions import plan_sessions, write_sessions
from pipeline.sync_symbols import symbol_rows
from pipeline.throttle import BudgetExceeded, RequestBudget, RetryAfterTooLong

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from collections.abc import Sequence
    from datetime import date

    import psycopg

    from pipeline.config import Config
    from pipeline.store import RunStatus

__all__ = ["RunReport", "main", "revalidate_site", "run_once"]


@dataclass
class RunReport:
    status: RunStatus
    messages: list[str] = field(default_factory=list)
    rows_prices: int = 0
    rows_metrics: int = 0
    #: 这一跑实际使用的 session 日期。``main`` 用它把 ``runs`` 那一行对齐。
    session_date: date | None = None

    @property
    def exit_code(self) -> int:
        """§7.2 的退出语义。**只有这一处决定告警不告警。**"""
        return 0 if self.status.startswith(("ok", "skipped")) else 1

    def note(self, text: str) -> None:
        self.messages.append(text)

    def escalate(self, status: RunStatus) -> None:
        """只往「更坏」的方向改，且不覆盖已经是失败类的状态。

        散在各处的 ``report.status = ...`` 很容易互相覆盖 ——
        一个 partial 被后面一句写回 ok，或者反过来把 stale_vendor 降成 partial。
        统一走这里。
        """
        rank = {"ok": 0, "ok_events_stale": 1, "partial": 2, "stale_vendor": 2, "failed": 3}
        if rank.get(status, 0) > rank.get(self.status, 0):
            self.status = status

    @property
    def message(self) -> str:
        return "；".join(self.messages)


def _existing_prices(
    conn: psycopg.Connection[Any], symbols: list[str], start: date, end: date
) -> dict[tuple[str, date], dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "select symbol, date, close, adj_close, source from prices_daily "
            "where symbol = any(%s) and date between %s and %s",
            (symbols, start, end),
        )
        return {
            (r[0], r[1]): {"close": r[2], "adj_close": r[3], "source": r[4]} for r in cur.fetchall()
        }


def _already_done(conn: psycopg.Connection[Any], session_date: date) -> bool:
    """§7.1 的条件重试：本 session 已有 ``ok`` 就跳过。

    夏令时那几跑靠这一条从 4 跑降到 1 跑 —— 它同时把供应商负载砍掉 3/4。
    """
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from private.runs where session_date = %s "
            "and status in ('ok', 'ok_events_stale') limit 1",
            (session_date,),
        )
        return cur.fetchone() is not None


def _refresh_events(
    conn: psycopg.Connection[Any],
    cfg: Config,
    symbols: list[str],
    session_date: date,
    budget: RequestBudget,
) -> tuple[dict[str, Any], bool]:
    """按 §3.5(4) 的节奏刷新事件，并把库里已有的事件算成四个距离。

    **抓取失败不记 partial。** ``partial`` 是 exit 1 且不写 ``ok`` 行，
    于是 §7.1 的条件重试「本 session 已有 ok 就跳过」不会跳过 ——
    夏令时那四跑会全部执行完整管道，而 18:40 那跑若撞上限流降级到 Stooq，
    **好数据会被更粗的源静默覆盖**。一个可选的装饰性指标，
    就这样获得了静默污染核心价格序列的能力。
    """
    import yfinance as yf

    with conn.cursor() as cur:
        cur.execute(
            "select symbol, min(event_date) from symbol_events "
            "where event_date >= %s group by symbol",
            (session_date,),
        )
        nearest = dict(cur.fetchall())
        cur.execute("select symbol, last_event_fetch_at::date from private.fetch_state")
        last_fetch = dict(cur.fetchall())

    ok = True
    fetched: list[str] = []
    for sym in symbols:
        if not should_refresh(
            today=session_date,
            is_trading_day=True,
            last_fetch_at=last_fetch.get(sym),
            nearest_event=nearest.get(sym),
            refresh_weekday=cfg.app.events_refresh_weekday,
            within_days=cfg.app.events_refresh_within_days,
        ):
            continue
        ticker = yf.Ticker(sym)

        def _cal(_s: str, t: Any = ticker) -> Any:
            return t.calendar

        def _earn(_s: str, t: Any = ticker) -> Any:
            return t.earnings_dates

        def _divs(_s: str, t: Any = ticker) -> Any:
            return t.dividends

        outcome = fetch_symbol_events(
            sym,
            today=session_date,
            sessions_start=cfg.app.sessions_start_date,
            budget=budget,
            calendar_fn=_cal,
            earnings_dates_fn=_earn,
            dividends_fn=_divs,
        )
        if not outcome.ok:
            ok = False
            continue
        # 删+插在**同一个事务**里，且仅当抓取成功（§3.5(1)）。
        with data_transaction(conn):
            for etype, start in outcome.coverage.items():
                delete_events_window(conn, sym, etype, start)
            insert_events(
                conn,
                [
                    {
                        "symbol": e.symbol,
                        "event_type": e.event_type,
                        "event_date": e.event_date,
                        "is_estimated": e.is_estimated,
                        "amount": e.amount,
                        "source": e.source,
                    }
                    for e in outcome.events
                ],
            )
            touch_fetch_state(conn, [sym])
        fetched.append(sym)

    # 距离一律**从库里**算 —— 这一跑没刷新的标的也要有值。
    from pipeline.fetch_events import SymbolEvent

    with conn.cursor() as cur:
        cur.execute(
            "select symbol, event_type, event_date, is_estimated from symbol_events "
            "where symbol = any(%s)",
            (symbols,),
        )
        rows = cur.fetchall()
    by_symbol: dict[str, list[SymbolEvent]] = {}
    for sym, etype, edate, est in rows:
        by_symbol.setdefault(sym, []).append(
            SymbolEvent(symbol=sym, event_type=etype, event_date=edate, is_estimated=est)
        )
    return (
        {s: distances_for(evs, session_date) for s, evs in by_symbol.items()},
        ok,
    )


def revalidate_site(report: RunReport) -> None:
    """写库成功后请前端重新生成页面（§10.1）。

    **必须断言响应是 200，非 200 升为 partial。**
    middleware 的 matcher 若漏掉 `/api/*`，这个没有 cookie 的请求会拿到
    **307 跳转到 /login** 而不是错误；把它当成功，就会出现：
    管道报 ok、重验证从未发生、`REVALIDATE_TOKEN` 成了死重量、
    页面退回 `revalidate = 3600` —— 恰好是 §10.1 特意设计掉的那个延迟，
    **而且无从得知**。

    没配 URL/token 时静默跳过：它是可选的，缺它不该让管道变红。
    """
    import urllib.error
    import urllib.request

    base = os.environ.get("SITE_URL", "").rstrip("/")
    token = os.environ.get("REVALIDATE_TOKEN", "")
    if not base or not token:
        return
    url = f"{base}/api/revalidate?token={token}"
    try:
        req = urllib.request.Request(url, data=b"", method="POST")  # noqa: S310
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as exc:
        report.escalate("partial")
        report.note(f"重验证请求失败：{exc}")
        return

    if status != 200:
        # **从任何「成功类」状态升级，不只是从 ok。**
        # 只认 ok 时，一次事件抓取失败（ok_events_stale）叠加一次重验证失败
        # 会保持 exit 0 —— 而页面可能整整一小时停在旧内容上，无人知晓。
        report.escalate("partial")
        hint = {
            307: "middleware 的 matcher 没排除 /api/*，请求被 307 到了 /login",
            302: "多半是 Vercel 的 Deployment Protection 挡住了（它会跳到 vercel.com/sso-api）",
            401: "Vercel 的 Deployment Protection，或 REVALIDATE_TOKEN 与 Vercel 上的不一致",
            403: "Vercel 的 Deployment Protection",
        }.get(status, "检查 SITE_URL 指向的部署是否开着 Deployment Protection")
        report.note(f"重验证返回 {status}（不是 200）—— {hint}")


def _null_rows(symbols: Sequence[str], day: date, cfg: Config) -> list[dict[str, Any]]:
    """给闸门 3 排除掉的标的补一行**全 NULL** 的最新行（§7.2 闸门 3）。"""
    from pipeline.compute import EVENT_COLUMNS
    from pipeline.store import METRICS_WRITE_COLUMNS

    rows: list[dict[str, Any]] = []
    for sym in symbols:
        row: dict[str, Any] = dict.fromkeys(METRICS_WRITE_COLUMNS)
        row |= {
            "symbol": sym,
            "date": day,
            "extra": {},
            # 整行没有值，所以每个有软闸门的指标都算「信不过」。
            "provisional_metrics": sorted(
                m.id for m in cfg.metrics.metrics if m.provisional_below is not None
            ),
        }
        for col in EVENT_COLUMNS:
            row[col] = None
        rows.append(row)
    return rows


def run_once(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    now: datetime,
    force: bool = False,
    backfill: bool = False,
) -> RunReport:
    """跑一次。``now`` 可注入 —— §11 M5 的冻结时钟测试押在这上面。"""
    report = RunReport(status="ok")

    # 1. 同步日历（必须在闸门之前 —— 闸门读的就是这张表）。
    #
    # **不在这里 commit。** 闸门读的是下面返回的内存列表，不是那张表，
    # 所以没有任何东西需要它先落地；而提前提交的代价是真实的：
    # 一次以 failed 收场的运行，也已经永久改写了 trading_sessions 的
    # ordinal —— 包括在一次历史修订里**删掉**某些 strength_daily 还在
    # 引用的日期，于是那些行从 v_strength_enriched 的 INNER join 里
    # 无声消失、days_in_top_n 从头数起（§9.1.4 那段加框的警告）。
    # trading_sessions 是 T2 所写指标的输入，它属于同一个原子单元。
    sessions, revision = plan_sessions(conn, cfg, now.astimezone(ET).date())
    # 只读算完就把事务放掉 —— 下面的抓取要跑 5–10 分钟。
    conn.rollback()
    if revision.needs_repair:
        # §9.1.4 第 5 条：「变更触发的重算记为 partial **而非静默进行**」。
        # 静默的后果很具体：ordinal 刚在这些标的脚下整体变过，而
        # strength_daily 的历史日期不在每日重写范围内 —— 没人被告知。
        report.escalate("partial")
        report.note(
            f"日历历史修订：{revision.describe()}；自 {revision.repair_from} 起，"
            "metrics/strength 需要一次有界重算（§9.1.4 第 3 条），请手动跑 backfill"
        )
    elif revision.added_future:
        report.note(revision.describe())

    # 2. 闸门 1 + 2
    gate = when_to_run(sessions, now, cfg.app.settle_minutes)
    if not gate.should_run and not force and not revision.needs_repair:
        report.status = gate.decision  # type: ignore[assignment]  # 跳过类，直接赋值
        report.note(gate.reason)
        conn.rollback()
        return report

    if gate.should_run or force:
        session = gate.session or last_settled_session(sessions, now, cfg.app.settle_minutes)
    else:
        # **修复跑不能用今天那根。**
        #
        # 走到这里只剩一种可能：闸门说了不跑，而 revision.needs_repair 把它顶开了。
        # 而 `when_to_run` 在 skipped_too_early 分支里返回的 gate.session 是**今天** ——
        # 于是一次自动触发的修复会去抓一根**还没过 settle_minutes** 的今日 bar：
        # 16:00 ET 那条 cron 上就是敲钟那一刻的价，workflow_dispatch 上可以是盘中价。
        #
        # 闸门 3 拦不住（日期就是今天，`stale_symbols` 比的正是日期相等），
        # 闸门 4 也拦不住（preliminary 与 consolidated 的差是千分位，
        # 离 _MAX_DAILY_MOVE / _CROSS_SOURCE_TOLERANCE 十万八千里）。
        # 写进去的就是 daily.yml 文件头那句「宁可晚一小时，不要一个会变的数字」
        # 要防的东西，而且整窗 strength 都由它导出。
        #
        # 修复要的只是历史窗口，根本不需要今天那根 —— 钳到上一个已定稿的 session。
        session = last_settled_session(sessions, now, cfg.app.settle_minutes)
        report.note(f"日历修复绕过闸门（{gate.decision}），session 钳到已定稿的 {session.date}")

    report.session_date = session.date
    # **日历修订不能被「本日已做过」吞掉。**
    #
    # 修订是在同一个 session 的后续重试里才被发现的 —— 那时本日往往已经有一条
    # ok 记录。若照常跳过，ordinal 已经改了（write_sessions 在 T2 里），
    # 而 metrics/strength 不会重算、也不会告警（exit 0）。
    # 一次真实的、需要修复的状态变更，就此变成一次绿色的「跳过」。
    must_repair = revision.needs_repair
    if not force and not must_repair and _already_done(conn, session.date):
        report.status = "skipped_already_done"
        report.note(f"{session.date} 已有成功记录，跳过（§7.1 条件重试）")
        conn.rollback()  # 连 sessions 的写一起丢掉：这一跑什么都不做
        return report
    # **把这次读打开的事务放掉。** 下面的整窗抓取按 §7.3.1 设计就要跑
    # 5–10 分钟，挂着一个 idle-in-transaction 的连接会挡住 vacuum，
    # 而且正是 pooler 的空闲事务杀手最爱的形状 —— 被杀的表现是
    # 一次健康运行报 failed。
    #
    # 这行 rollback 曾经**只有注释没有代码**：`_already_done` 那条 SELECT
    # 会开一个事务，然后一路挂到整窗抓取结束 —— 正是注释自己描述的形状。
    # （连接是 autocommit=False，见 store.py。）
    conn.rollback()

    # 3. 抓取整窗（§3.0 规则 2）
    symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
    start, n_bars = lookback_window(sessions, session.date, cfg.app.lookback_bars)
    if n_bars < cfg.app.lookback_bars:
        report.note(f"窗口只有 {n_bars} 根（要 {cfg.app.lookback_bars}）")

    # §9.1.4 第 3 条的**有界修复**：历史日发生增减时，从最早变化点起重算。
    #
    # 只记 partial 然后让人「跑一次 backfill」是**无效的建议** ——
    # backfill 走的是同一个 run_once，窗口同样被 lookback_bars 封顶。
    # 修订点若早于那个窗口，trading_sessions 已经改了，而 metrics/strength
    # 会无限期停在旧的 ordinal 上。所以这里真的把左端点前移。
    repair_from = revision.repair_from if revision.needs_repair else None

    # `write_from` 是**要写入**的最早一天；`start` 是**要抓取**的最早一天。
    # 两者不同，因为指标需要预热。
    write_from = min(start, repair_from) if repair_from is not None else start
    if write_from < start:
        # **修复点之前还要再取一整个 lookback 的历史。**
        #
        # 直接把 start 设成 repair_from 会让 EMA/RSI/alpha 在修复点上
        # 从**冷启动**开始算 —— 于是一批本来正确的行被 NULL 或冷启动值覆盖，
        # 然后还照这个结果重排了名次。预热不是可选项：
        # §12 #5 整节在论证 EMA(60) 需要 6×N 的喂入量。
        start, _ = lookback_window(sessions, write_from, cfg.app.lookback_bars)
        report.note(f"修复：写入自 {write_from} 起，抓取自 {start} 起（含预热）")

    budget = RequestBudget(
        max_requests=cfg.app.max_requests_per_run,
        interval_seconds=(
            cfg.app.backfill_interval_seconds if backfill else cfg.app.request_interval_seconds
        ),
        retry_max_attempts=cfg.app.retry_max_attempts,
    )
    try:
        outcome = fetch_window(symbols, start, session.date, budget)
    except (BudgetExceeded, RetryAfterTooLong) as exc:
        # §7.3.1 对这两件事的处置都是 **partial**，不是 failed：
        # 「超出即中止并记 partial」「放弃这一跑记 partial」。
        # 记 failed 会让它看起来像代码坏了，而它其实是一次礼让。
        report.escalate("partial")
        report.note(f"抓取中止：{exc}")
        conn.rollback()
        return report
    if outcome.degraded:
        report.escalate("partial")
        report.note(f"整窗降级到 Stooq：{', '.join(outcome.degraded)}")
    if outcome.missing:
        report.escalate("partial")
        report.note(f"两个源都没拿到：{', '.join(outcome.missing)}")
    if outcome.issues:
        report.escalate("partial")
        report.note("脏数据：" + "；".join(str(i) for i in outcome.issues[:5]))
    if outcome.rejected:
        # §7.2 闸门 4 的最后一句：「违反 → 该标的 partial，**不写毒数据**」。
        # fetch_window 已经把它们从 frame 里剔除了；这里只是把原因记下来。
        report.escalate("partial")
        diffs = ", ".join(
            f"{s}={outcome.cross_source_max_diff.get(s, float('nan')):.1%}"
            for s in outcome.rejected
        )
        report.note(f"跨源比对未通过、已剔除：{diffs}")

    prices = restrict_to_sessions(outcome.frame, sessions)
    if prices.empty:
        # **这里是唯一一次「一行都没有」的检查，而这一点是有意的。**
        #
        # 闸门 3 之后曾经还有第二次同样的检查，因为那时落后标的会被
        # 整窗过滤掉、`prices` 可能在闸门 3 之后才变空。现在闸门 3 不再丢
        # 任何行（见下面那段），于是第二次检查成了死代码 —— mypy --strict
        # 当场指出 unreachable，删掉比留一段永不执行的「保护」诚实。
        #
        # 它要防的东西仍然由这一次挡住：供应商全线故障时若让 T2 照常执行，
        # `replace_strength` 会先 `delete from strength_daily where date = ?`
        # 再插 0 行 —— **当天的榜单被静默删掉**，
        # 而 §7.2 承诺的是「重跑、补跑…结果都一样」。
        report.status = "failed"
        report.note("窗口内没有任何价格行")
        conn.rollback()
        return report

    # 4. 闸门 3：**逐标的**最新 bar 日期 == 当日 session
    latest = {str(sym): max(g["date"]) for sym, g in prices.groupby("symbol", sort=False)}
    lagging = stale_symbols({s: latest.get(s) for s in symbols}, session.date)
    if lagging:
        bench = cfg.universe.benchmark
        # 已经是 partial 的话不要被覆盖回去（两者都 exit 1，但消息要留全）。
        report.escalate("stale_vendor" if bench in lagging else "partial")
        report.note(f"bar 落后：{', '.join(lagging)}")
        # **只是尾部缺了一根，不是整窗都不可信 —— 所以这里什么都不丢。**
        #
        # 曾经这里是 `prices = prices[~prices["symbol"].isin(lagging)]`，
        # 把落后标的的**整个窗口**丢掉，而下面只用 `_null_rows` 补回
        # `session.date` 那**一行**。于是 compute_metrics 对它一行都不产出，
        # compute_strength 直接把它排除出 scores（不是给末位，是不进榜），
        # 而 replace_strength 是按日 delete+insert ——
        # 一次日历修复会把 rerank 窗口左移几百天，于是那几百天的榜单
        # **每一天**都被重写成「没有这个标的」，其余名次集体上移。
        # 修复跑结束后 rerank 窗口缩回滚动 400 根，比它更老的那些天
        # **再也不会被重排**，错名次就是最终状态 ——
        # 而 days_in_top_n / rank_delta_1d 正是从这些行导出的。
        #
        # 闸门 3 判的是**新鲜度**，不是可信度：历史那些 bar 本来就是对的。
        # 要挡住的只有「拿昨天那行当最新行」，而 `_null_rows` 已经在
        # session.date 上补了一行全 NULL，它自然成为最新行。
        #
        # 也不会和 interior_gaps 重复报：那边的 expected 上界是标的自己的
        # max(have)，尾部缺失不算空洞。

    # 闸门 3 的另一半：窗口**中间**的空洞。
    #
    # 上面只比了最新一根。一个内部空洞（某天限流、薄票、供应商单日故障）
    # 会完整地过掉闸门，而后果是安静的 —— 收益样本被悄悄缩短，
    # alpha/beta 按日期对齐会丢掉空洞两侧那两天，却仍可能满足 min_obs
    # 并给出一个看起来完全合理的数。
    if not prices.empty:
        bars = {str(sym): list(g["date"]) for sym, g in prices.groupby("symbol", sort=False)}
        window_dates = [s.date for s in sessions if start <= s.date <= session.date]
        gaps = interior_gaps(bars, window_dates)
        if gaps:
            report.escalate("partial")
            report.note(
                "窗口内有空洞：" + ", ".join(f"{s}缺{n}根" for s, n in sorted(gaps.items()))
            )

    # 4b. 事件（§3.5）。**失败不得惊动主管道** —— 见下面的 ok_events_stale。
    try:
        events_by_symbol, events_ok = _refresh_events(conn, cfg, symbols, session.date, budget)
    except (BudgetExceeded, RetryAfterTooLong) as exc:
        # 全局护栏，不是「事件失败」。§7.3.1 对这两种的处置是
        # **放弃这一跑**并记 partial —— 和价格阶段完全一样。
        #
        # 初版在这里 `events_by_symbol = {}` 然后继续跑完：那会把**每一个**
        # 标的最新行的八个事件列写成 NULL，包括那些库里本来就有、
        # 而且完全有效的事件 —— 用一次限流换掉了一批好数据。
        # 「中止」和「把事件清空之后照常发布」不是一回事。
        conn.rollback()
        report.escalate("partial")
        report.note(f"事件阶段中止（预算/限流），本跑不写入：{exc}")
        return report
    if not events_ok:
        report.escalate("ok_events_stale")
        # §3.5(4) 的原文：「核心价格与四个核心指标照常写入，**事件列写 NULL**」。
        #
        # **不能把库里的旧事件再发布一遍。** 抓取失败的时候，库里那条
        # 「下一次财报」恰恰最可能是已经被改期、已经作废的那一条 ——
        # 而改期正是我们每周要去抓一次的全部理由。
        # 拿它算出来的倒计时会走向一个不存在的日子，到期后翻成
        # 「财报后 1 天」，**播报一场从未发生的财报**（§3.5(1)）。
        #
        # 而 §9.3.2 那条「倒计时必须与 symbol_events 一致」抓不到它：
        # 它比的正是同一条陈旧的行，于是完全自洽。
        events_by_symbol = {}
        report.note("事件抓取失败：核心指标照常写入，事件列写 NULL（§3.5(4)）")

    # 5. 计算（三层一起，§3.0 规则 2）
    ordinal_by_date = {s.date: s.ordinal for s in sessions}
    windows = build_windows(prices, ordinal_by_date)
    metrics = compute_metrics(
        cfg, windows, event_distances=events_by_symbol, latest_date=session.date
    )
    # §7.2 闸门 3 的原话是「该标的**指标写 NULL**」—— 不是「不写」。
    # 不写的话，昨天那一行会成为这个标的的最新行，而它带着昨天的
    # alpha/beta **和八个事件列**；于是 §3.5(3) 的「历史行事件列必须全为 NULL」
    # 那条不变式会在任何一个标的落后的当天变红。
    # 落后的与被剔除的，都要有一行**全 NULL** 的最新行 ——
    # 否则昨天那行会成为它们的最新行，带着昨天的 alpha/beta 与八个事件列。
    metrics += _null_rows(sorted({*lagging, *outcome.rejected}), session.date, cfg)
    # 预热区只用于**计算**，不写库 —— 它自己的预热是不足的，
    # 写回去会把更早那些本来正确的行覆盖成冷启动值。
    metrics = [r for r in metrics if r["date"] >= write_from]
    pool = {s.symbol: s.type for s in cfg.universe.symbols}
    # **整个窗口都要重排，不是只排当天。**
    #
    # §3.0 规则 2 的那张表写得很直白：价格层、指标层、榜单层**三层一起**，
    # 范围相同 ——「`strength_daily` | 同样日期范围，按日期删+插，全池重排」。
    #
    # 只排当天时：一次除息会让供应商追溯改写全部历史复权因子，于是历史
    # `mom_20` 变了，而 `strength_daily` 还留着旧名次、旧 top-N 成员，
    # 以及由它们派生的 `days_in_top_n`。代码甚至**检测到了**因子变化，
    # 却只是记了一笔 —— 那正是「探测器响了但没人动手」。
    rerank_days = [s.date for s in sessions if write_from <= s.date <= session.date]
    strength_by_day = {d: compute_strength(cfg, metrics, day=d, pool=pool) for d in rerank_days}

    # 6. T2：一个原子单元
    existing = _existing_prices(conn, symbols, start, session.date)
    price_rows = prices.to_dict("records")
    d = diff_prices(price_rows, existing)  # type: ignore[arg-type]
    if d.factor_changed:
        report.note(f"复权因子变化：{', '.join(d.factor_changed)}（§3.0 规则 2）")

    with data_transaction(conn):
        # trading_sessions 是 T2 所写指标的**输入**，它属于同一个原子单元。
        write_sessions(conn, sessions)
        # 日历里消失的历史日，它的榜单行会变成**孤儿**：
        # 基表里还在（于是 §9.3.2 的「每个 strength_daily.date 都必须在
        # trading_sessions 里存在」会变红），而 v_strength_enriched 的
        # INNER join 里已经没有它 —— 一行公开的、永远对不上的历史数据。
        # 价格行按 §9.1.4 第 3 条第 2 步可以留着不管（计算侧已按 sessions 过滤），
        # 但榜单行必须删。
        for gone in revision.removed_historical:
            replace_strength(conn, gone, [])
        write_symbols(conn, symbol_rows(cfg))
        report.rows_prices = upsert_prices(conn, list(d.changed))
        report.rows_metrics = upsert_metrics(conn, metrics)
        for day, rows in strength_by_day.items():
            replace_strength(conn, day, rows)

    report.note(
        f"价格 {report.rows_prices} 行（{d.unchanged} 行未变）、指标 {report.rows_metrics} 行"
    )
    report.note(f"请求 {budget.used}/{cfg.app.max_requests_per_run}")

    # 写完了才重验证 —— 页面在管道写完几秒内更新，
    # 而不是「什么都没发生之后一小时」。
    revalidate_site(report)
    return report


def main(argv: list[str] | None = None) -> int:
    dsn = os.environ.get("MARKME_DB_URL")
    if not dsn:
        sys.stderr.write("缺少 MARKME_DB_URL\n")
        return 1
    args = argv if argv is not None else sys.argv[1:]
    force = "--force" in args
    backfill = "--backfill" in args

    cfg = load_config()
    now = datetime.now(tz=ET)
    with connect(dsn) as conn:
        # **runs.session_date 必须和数据用的是同一天。**
        # 先用 now.date() 开 run、再让 run_once 自己去挑 session，两者在
        # 任何 --force / 周末 dispatch（以及每一次 backfill）下都会分叉，
        # 而 _already_done 查的是后者 —— 它去找的那一行不是 start_run 写的那行。
        run_id = start_run(conn, now.date(), os.environ.get("GITHUB_SHA"))
        try:
            report = run_once(conn, cfg, now=now, force=force, backfill=backfill)
        except Exception as exc:
            conn.rollback()
            finish_run(conn, run_id, "failed", message=f"{type(exc).__name__}: {exc}")
            sys.stderr.write(f"failed: {exc}\n")
            return 1
        finish_run(
            conn,
            run_id,
            report.status,
            session_date=report.session_date,
            rows_prices=report.rows_prices,
            rows_metrics=report.rows_metrics,
            message=report.message,
        )
        sys.stdout.write(f"{report.status}: {report.message}\n")
        return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
