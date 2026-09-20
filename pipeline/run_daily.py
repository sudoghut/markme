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

from pipeline.calendar_gate import ET, stale_symbols, when_to_run
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
from pipeline.sync_sessions import sync_sessions
from pipeline.sync_symbols import symbol_rows
from pipeline.throttle import RequestBudget

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from datetime import date

    import psycopg

    from pipeline.config import Config
    from pipeline.store import RunStatus

__all__ = ["RunReport", "main", "run_once"]


@dataclass
class RunReport:
    status: RunStatus
    messages: list[str] = field(default_factory=list)
    rows_prices: int = 0
    rows_metrics: int = 0

    @property
    def exit_code(self) -> int:
        """§7.2 的退出语义。**只有这一处决定告警不告警。**"""
        return 0 if self.status.startswith(("ok", "skipped")) else 1

    def note(self, text: str) -> None:
        self.messages.append(text)

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

    # 1. 同步日历（必须在闸门之前 —— 闸门读的就是这张表）
    sessions, revision = sync_sessions(conn, cfg, now.astimezone(ET).date())
    conn.commit()
    if revision.needs_repair:
        report.note(f"日历修订：{revision.describe()}，自 {revision.repair_from} 起需重算")
    elif revision.added_future:
        report.note(revision.describe())

    # 2. 闸门 1 + 2
    gate = when_to_run(sessions, now, cfg.app.settle_minutes)
    if not gate.should_run and not force:
        report.status = gate.decision  # type: ignore[assignment]
        report.note(gate.reason)
        return report
    session = gate.session or max(
        (s for s in sessions if s.date <= now.astimezone(ET).date()),
        key=lambda s: s.ordinal,
    )

    if not force and _already_done(conn, session.date):
        report.status = "skipped_already_done"
        report.note(f"{session.date} 已有成功记录，跳过（§7.1 条件重试）")
        return report

    # 3. 抓取整窗（§3.0 规则 2）
    symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
    start, n_bars = lookback_window(sessions, session.date, cfg.app.lookback_bars)
    if n_bars < cfg.app.lookback_bars:
        report.note(f"窗口只有 {n_bars} 根（要 {cfg.app.lookback_bars}）")

    budget = RequestBudget(
        max_requests=cfg.app.max_requests_per_run,
        interval_seconds=(
            cfg.app.backfill_interval_seconds if backfill else cfg.app.request_interval_seconds
        ),
        retry_max_attempts=cfg.app.retry_max_attempts,
    )
    outcome = fetch_window(symbols, start, session.date, budget)
    if outcome.degraded:
        report.status = "partial"
        report.note(f"整窗降级到 Stooq：{', '.join(outcome.degraded)}")
    if outcome.missing:
        report.status = "partial"
        report.note(f"两个源都没拿到：{', '.join(outcome.missing)}")
    if outcome.issues:
        report.status = "partial"
        report.note("脏数据：" + "；".join(str(i) for i in outcome.issues[:5]))

    prices = restrict_to_sessions(outcome.frame, sessions)
    if prices.empty:
        report.status = "failed"
        report.note("窗口内没有任何价格行")
        return report

    # 4. 闸门 3：**逐标的**最新 bar 日期 == 当日 session
    latest = {str(sym): max(g["date"]) for sym, g in prices.groupby("symbol", sort=False)}
    lagging = stale_symbols({s: latest.get(s) for s in symbols}, session.date)
    if lagging:
        bench = cfg.universe.benchmark
        report.status = "stale_vendor" if bench in lagging else "partial"
        report.note(f"bar 落后：{', '.join(lagging)}")
        prices = prices[~prices["symbol"].isin(lagging)].reset_index(drop=True)

    # 4b. 事件（§3.5）。**失败不得惊动主管道** —— 见下面的 ok_events_stale。
    events_by_symbol, events_ok = _refresh_events(conn, cfg, symbols, session.date, budget)
    if not events_ok and report.status == "ok":
        report.status = "ok_events_stale"
        report.note("事件抓取失败，核心指标照常写入（§3.5(4)）")

    # 5. 计算（三层一起，§3.0 规则 2）
    ordinal_by_date = {s.date: s.ordinal for s in sessions}
    windows = build_windows(prices, ordinal_by_date)
    metrics = compute_metrics(
        cfg, windows, event_distances=events_by_symbol, latest_date=session.date
    )
    pool = {s.symbol: s.type for s in cfg.universe.symbols}
    strength = compute_strength(cfg, metrics, day=session.date, pool=pool)

    # 6. T2：一个原子单元
    existing = _existing_prices(conn, symbols, start, session.date)
    price_rows = prices.to_dict("records")
    d = diff_prices(price_rows, existing)  # type: ignore[arg-type]
    if d.factor_changed:
        report.note(f"复权因子变化：{', '.join(d.factor_changed)}（§3.0 规则 2）")

    with data_transaction(conn):
        write_symbols(conn, symbol_rows(cfg))
        report.rows_prices = upsert_prices(conn, list(d.changed))
        report.rows_metrics = upsert_metrics(conn, metrics)
        replace_strength(conn, session.date, strength)

    report.note(
        f"价格 {report.rows_prices} 行（{d.unchanged} 行未变）、指标 {report.rows_metrics} 行"
    )
    report.note(f"请求 {budget.used}/{cfg.app.max_requests_per_run}")
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
        session_date = now.date()
        run_id = start_run(conn, session_date, os.environ.get("GITHUB_SHA"))
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
            rows_prices=report.rows_prices,
            rows_metrics=report.rows_metrics,
            message=report.message,
        )
        sys.stdout.write(f"{report.status}: {report.message}\n")
        return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
