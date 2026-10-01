"""``run_once`` 的**行为**测试 —— 这个文件存在的理由本身就是一条发现。

在它之前，``test_run_daily.py`` 里**没有一条测试真正调用过 ``run_once``**：
每一条要么测纯 dataclass，要么是 ``_code(run_once)`` 的源码 grep。
而闸门抓到的 SERIOUS 里，有三条整整齐齐地全部发生在 ``run_once`` 内部：

1. 日历修复顶开闸门 2 之后 ``session`` 取成了**今天**（还没定稿的 bar）；
2. 闸门 3 把落后标的的**整窗**价格丢掉，于是整窗重排把它从历史里抹掉；
3. **全员落后**时一路走到底，写 16 行全 NULL 并把当天已发布的榜单删空。

grep 抓不到「session 取错了哪一天」，也抓不到「replace_strength 被拿 ``[]`` 调了」。
所以这里搭一套假 conn + 假抓取的夹具，让这三件事都变成可断言的行为。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from pipeline.calendar_gate import ET
from pipeline.config import load_config
from pipeline.fetch import FetchOutcome
from pipeline.sessions import CalendarRevision, Session

D0 = date(2024, 6, 3)  # 周一


def _sessions(n: int) -> list[Session]:
    """n 个连续工作日的 session。足够让闸门与窗口逻辑跑起来。"""
    out: list[Session] = []
    d = D0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(
                Session(date=d, ordinal=len(out) + 1, close_et=time(16, 0), is_half_day=False)
            )
        d += timedelta(days=1)
    return out


class FakeCursor:
    def execute(self, *a: Any, **k: Any) -> None:
        return None

    def executemany(self, *a: Any, **k: Any) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def fetchone(self) -> tuple[Any, ...] | None:
        return None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class FakeConn:
    """只需要 ``cursor()`` 和 ``rollback()``。所有真正的写都被打桩拦截。"""

    def __init__(self) -> None:
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor()

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        return None


def _frame(symbols: list[str], days: list[date], *, price: float = 100.0) -> pd.DataFrame:
    rows = []
    for s in symbols:
        for i, d in enumerate(days):
            rows.append(
                {
                    "symbol": s,
                    "date": d,
                    "open": float("nan"),
                    "high": float("nan"),
                    "low": float("nan"),
                    "close": price + i * 0.1,
                    "adj_close": price + i * 0.1,
                    "volume": float("nan"),
                    "source": "yfinance",
                }
            )
    return pd.DataFrame(rows)


def _no_revision() -> CalendarRevision:
    return CalendarRevision(
        added_future=(), removed_future=(), added_historical=(), removed_historical=()
    )


def _install_plan(
    rd: Any, monkeypatch: Any, sessions: list[Session], rev: CalendarRevision
) -> None:
    monkeypatch.setattr(rd, "plan_sessions", lambda conn, cfg, today: (sessions, rev))


class TestRepairNeverUsesTodaysUnsettledBar:
    """闸门 2 被 ``needs_repair`` 顶开时，**不能**拿今天那根还没定稿的 bar。

    16:00 ET 正好是敲钟那一刻，``settle_minutes``（60）一分钟都没过。
    闸门 3 比的是日期相等（今天，通过），闸门 4 的阈值是 50% 日内波动
    与 2% 跨源差（preliminary 与 consolidated 的差是千分位，通过）——
    两道后闸门都拦不住，写进去的就是一个**会变的数字**。
    """

    def test_session_is_clamped_to_the_previous_settled_day(self, harness: Any) -> None:
        _, monkeypatch, rd = harness
        cfg = load_config()
        sessions = _sessions(40)
        today = sessions[-1].date
        revision = CalendarRevision(
            added_future=(),
            removed_future=(),
            added_historical=(sessions[5].date,),
            removed_historical=(),
        )
        assert revision.needs_repair

        _install_plan(rd, monkeypatch, sessions, revision)
        # 抓取返回空帧 → 在 T2 之前就 return，但 session_date 已经定下来了。
        monkeypatch.setattr(rd, "fetch_window", lambda *a, **k: FetchOutcome(frame=pd.DataFrame()))

        # 敲钟那一刻：今天收盘 16:00，settle 330 分钟 → 今天还没定稿。
        now = datetime.combine(today, time(16, 0), tzinfo=ET)
        report = rd.run_once(FakeConn(), cfg, now=now)

        assert report.session_date == sessions[-2].date, (
            "修复跑必须钳到上一个已定稿的 session，而不是用今天那根未定稿的 bar"
        )

    def test_after_settle_today_is_used(self, harness: Any) -> None:
        _, monkeypatch, rd = harness
        cfg = load_config()
        sessions = _sessions(40)
        today = sessions[-1].date
        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(rd, "fetch_window", lambda *a, **k: FetchOutcome(frame=pd.DataFrame()))
        now = datetime.combine(today, time(22, 0), tzinfo=ET)
        report = rd.run_once(FakeConn(), cfg, now=now)
        assert report.session_date == today


class TestStaleVendorOnlyAlertsWhenTheDayIsOut:
    """**「供应商还没出数」与「供应商坏了」现象一模一样，区分它们的是时刻。**

    实测（M12，2026-09-29）：Yahoo 要到 22:15 ET 前后才结算完收盘价，
    在那之前给的是半根 bar。而排期里早于它的那几跑**每天**都会撞上
    「全员落后」——若照旧 exit 1，`daily` 每个交易日都要红好几次，
    于是 09-29 那天真正的故障淹在噪音里没人看见。

    判据是 ``vendor_deadline_et``（22:30 ET）：之前只记录，之后才告警。
    那个数**必须 <= 每种情形下「最晚那一跑」的落点**，否则最后一跑也被宽限，
    就成了静默丢数据 —— 见 `test_schedule_dst.py` 里绑住它的那条。
    """

    @staticmethod
    def _lagging_run(harness: Any, at: time) -> Any:
        _, monkeypatch, rd = harness
        cfg = load_config()
        sessions = _sessions(60)
        today = sessions[-1].date
        symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
        days = [s.date for s in sessions[:-1]]  # 全员停在 D-1
        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(
            rd,
            "fetch_window",
            lambda *a, **k: FetchOutcome(
                frame=_frame(symbols, days),
                per_symbol_source=dict.fromkeys(symbols, "yfinance"),
            ),
        )
        return rd.run_once(FakeConn(), cfg, now=datetime.combine(today, at, tzinfo=ET))

    def test_before_the_deadline_it_records_but_does_not_alert(self, harness: Any) -> None:
        report = self._lagging_run(harness, time(22, 0))  # 早于截止
        assert report.status == "stale_vendor", "状态照记 —— runs 里的历史必须诚实"
        assert report.vendor_retry_pending is True
        assert report.exit_code == 0, "当天还有后续跑，这不是故障"
        assert "当天仍有后续跑" in report.message

    def test_after_the_deadline_it_alerts(self, harness: Any) -> None:
        report = self._lagging_run(harness, time(23, 30))
        assert report.status == "stale_vendor"
        assert report.vendor_retry_pending is False
        assert report.exit_code == 1, "当天已无补救机会，必须有人看见"
        assert "没有补救机会" in report.message

    def test_the_deadline_itself_alerts(self, harness: Any) -> None:
        """**边界归告警那一侧。** 截止时刻那一跑是当天最后的机会，不该被宽限。"""
        assert self._lagging_run(harness, time(22, 30)).exit_code == 1


class TestOnlyTheBenchmarkLaggingAlsoGetsTheGrace:
    """**结算不是 17 只同时翻的。**

    `fetch.py` 丢掉 `adj_close` 为 NaN 的半根 bar，于是「16 只已结算、
    基准 QQQ 还没有」是结算过程中最正常的中间态 —— 它走的是
    `if lagging:` 里 `bench in lagging` 那一支，和「全员落后」不是同一段代码。

    第一版只给「全员落后」那一支装了宽限，而当时 §7.2 的表里写的恰恰是
    「**基准** bar 落后（当天还有后续跑）→ exit 0」。表和实现对不上，
    M12 要消灭的噪音会从这一支原样漏回来。
    """

    @staticmethod
    def _bench_lagging_run(harness: Any, at: time) -> Any:
        _, monkeypatch, rd = harness
        cfg = load_config()
        bench = cfg.universe.benchmark
        sessions = _sessions(60)
        today = sessions[-1].date
        symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
        days = [s.date for s in sessions]
        others = [s for s in symbols if s != bench]
        # 只有基准停在 D-1，其余都拿到了今天。
        frame = pd.concat([_frame(others, days), _frame([bench], days[:-1])], ignore_index=True)
        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(
            rd,
            "fetch_window",
            lambda *a, **k: FetchOutcome(
                frame=frame, per_symbol_source=dict.fromkeys(symbols, "yfinance")
            ),
        )
        return rd.run_once(FakeConn(), cfg, now=datetime.combine(today, at, tzinfo=ET))

    def test_before_the_deadline_it_does_not_alert(self, harness: Any) -> None:
        report = self._bench_lagging_run(harness, time(22, 0))
        assert report.status == "stale_vendor", "基准落后仍然是 stale_vendor"
        assert report.exit_code == 0, "当天还有后续跑，这是结算中的正常中间态"
        assert "当天仍有后续跑" in report.message

    def test_after_the_deadline_it_alerts(self, harness: Any) -> None:
        report = self._bench_lagging_run(harness, time(23, 30))
        assert report.status == "stale_vendor"
        assert report.exit_code == 1, "当天已无补救机会，基准还缺就必须有人看见"


class TestOnlyANonBenchmarkLaggingAlsoGetsTheGrace:
    """**上一条的镜像：基准已结算，某只非基准还是半根 bar。**

    它和「只有基准落后」同样是结算中的正常中间态，却曾经无条件走 `partial`
    —— 截止之前 exit 1。新排期下多数情形的第一跑落在 22:00 ET，正好卡在
    21:30 放行与实测 22:15 结算之间，哪种情形响取决于 Yahoo 先结算哪只。
    （闸门 A 第 5 轮 S1。）

    截止之后它**仍然**是 `partial` 并告警 —— 宽限只覆盖「当天还有后续跑」。
    """

    @staticmethod
    def _other_lagging_run(harness: Any, at: time) -> tuple[Any, dict[str, Any]]:
        calls, monkeypatch, rd = harness
        cfg = load_config()
        bench = cfg.universe.benchmark
        sessions = _sessions(60)
        today = sessions[-1].date
        symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
        days = [s.date for s in sessions]
        others = [s for s in symbols if s != bench]
        # 基准与其余 15 只拿到了今天，只有一只非基准停在 D-1。
        frame = pd.concat(
            [_frame([bench, *others[1:]], days), _frame(others[:1], days[:-1])],
            ignore_index=True,
        )
        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(
            rd,
            "fetch_window",
            lambda *a, **k: FetchOutcome(
                frame=frame, per_symbol_source=dict.fromkeys(symbols, "yfinance")
            ),
        )
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, at, tzinfo=ET))
        return report, calls

    def test_before_the_deadline_it_does_not_alert(self, harness: Any) -> None:
        report, _ = self._other_lagging_run(harness, time(22, 0))
        assert report.status == "stale_vendor", "截止前任何一只落后都是在等供应商"
        assert report.exit_code == 0, "当天还有后续跑，这是结算中的正常中间态"
        assert "当天仍有后续跑" in report.message

    def test_before_the_deadline_it_still_writes(self, harness: Any) -> None:
        """和基准那一支一样：只是尾部缺一根，其余照常入库。"""
        _, calls = self._other_lagging_run(harness, time(22, 0))
        assert calls["replace_strength"], "这一跑仍然要写库"

    def test_after_the_deadline_it_is_partial_and_alerts(self, harness: Any) -> None:
        report, _ = self._other_lagging_run(harness, time(23, 30))
        assert report.status == "partial", "截止之后非基准落后仍是 partial"
        assert report.exit_code == 1, "当天已无补救机会，必须有人看见"

    def test_the_deadline_itself_alerts(self, harness: Any) -> None:
        """**边界归告警那一侧**，与另外两支一致。"""
        report, _ = self._other_lagging_run(harness, time(22, 30))
        assert report.exit_code == 1


class TestAGracedStaleVendorDoesNotSilenceRealProblems:
    """**这是整条 M12 最贵的一个交互，而它一度只被一条 dataclass 单测钉住。**

    `stale_vendor` 现在可以 exit 0（在等供应商结算，是良性的）。而「基准落后」
    那一支**不 return、会继续往下走**。于是只要它先把状态锁成 `stale_vendor`，
    下游每一个 `escalate("partial")` —— 窗口内有空洞 / 事件预算耗尽 /
    非预热区算不出横截面 / 重验证失败 —— 就都被吞掉，
    **而那一跑照样写库、照样把结果推上线**。

    修法是让 `partial` 在 rank 上压过 `stale_vendor`。这条测试守的是
    **端到端的那个行为**，不是 rank 表本身：把 rank 改回去，
    `run_once` 这一层原本一条都不红。
    """

    @staticmethod
    def _bench_lagging_with_a_gap(harness: Any) -> Any:
        _, monkeypatch, rd = harness
        cfg = load_config()
        bench = cfg.universe.benchmark
        sessions = _sessions(60)
        today = sessions[-1].date
        symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
        days = [s.date for s in sessions]
        others = [s for s in symbols if s != bench]
        frame = pd.concat([_frame(others, days), _frame([bench], days[:-1])], ignore_index=True)
        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(
            rd,
            "fetch_window",
            lambda *a, **k: FetchOutcome(
                frame=frame, per_symbol_source=dict.fromkeys(symbols, "yfinance")
            ),
        )
        # 窗口内有空洞 —— 它自己的 docstring 说，一个内部空洞会完整地过掉闸门，
        # 而后果是安静的。这里让它响一次，看还听不听得见。
        monkeypatch.setattr(rd, "interior_gaps", lambda bars, window: {others[0]: 1})
        return rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))

    def test_an_interior_gap_still_alerts_even_while_the_vendor_is_graced(
        self, harness: Any
    ) -> None:
        report = self._bench_lagging_with_a_gap(harness)
        assert report.status == "partial", "空洞不是良性的，它必须盖掉宽限"
        assert report.exit_code == 1, "宽限只对「在等供应商」成立"
        assert "窗口内有空洞" in report.message

    def test_it_still_writes_the_data(self, harness: Any) -> None:
        """**修复不能做成「不写了」。** 这一支本来就该写 —— 只是尾部缺一根。"""
        calls, _, _ = self._harness_calls(harness)
        assert calls, "这一跑仍然要写库"

    @staticmethod
    def _harness_calls(harness: Any) -> tuple[list[Any], Any, Any]:
        calls, monkeypatch, rd = harness
        TestAGracedStaleVendorDoesNotSilenceRealProblems._bench_lagging_with_a_gap(harness)
        return calls["replace_strength"], monkeypatch, rd


class TestEveryoneLaggingWritesNothing:
    """**全员落后**时一个字都不能写。

    这半个场景曾经由「闸门 3 过滤之后再查一次 ``prices.empty``」挡着。
    闸门 3 不再过滤之后那次检查成了死代码被删掉，这一半就没人管了 ——
    于是 16 行全 NULL 进库、前端 asOf 前进到今天、当天榜单被删空。
    """

    def test_it_returns_stale_vendor_without_touching_anything(self, harness: Any) -> None:
        calls, monkeypatch, rd = harness
        cfg = load_config()
        sessions = _sessions(60)
        today = sessions[-1].date
        symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
        # 每个标的的最新 bar 都停在 D-1 —— 供应商全线陈旧的典型形态。
        days = [s.date for s in sessions[:-1]]
        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(
            rd,
            "fetch_window",
            lambda *a, **k: FetchOutcome(
                frame=_frame(symbols, days),
                per_symbol_source=dict.fromkeys(symbols, "yfinance"),
            ),
        )
        now = datetime.combine(today, time(22, 0), tzinfo=ET)
        report = rd.run_once(FakeConn(), cfg, now=now)

        assert report.status == "stale_vendor"
        assert calls["upsert_metrics"] == [], "全员落后时不能写任何指标行"
        assert calls["replace_strength"] == [], "更不能把当天已发布的榜单删空"


class TestRankingsAreNeverBlanked:
    """``replace_strength`` 是**无条件** delete 再插，所以拿 ``[]`` 调它
    等于「删掉这一天已发布的榜单」。算不出横截面时该做的是**不动它**。"""

    def test_no_day_is_replaced_with_an_empty_ranking(self, harness: Any) -> None:
        calls, monkeypatch, rd = harness
        cfg = load_config()
        sessions = _sessions(60)
        today = sessions[-1].date
        symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
        days = [s.date for s in sessions]
        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(
            rd,
            "fetch_window",
            lambda *a, **k: FetchOutcome(
                frame=_frame(symbols, days),
                per_symbol_source=dict.fromkeys(symbols, "yfinance"),
            ),
        )
        now = datetime.combine(today, time(22, 0), tzinfo=ET)
        report = rd.run_once(FakeConn(), cfg, now=now)

        assert report.status in {"ok", "partial", "ok_events_stale", "stale_vendor"}
        empty = [day for day, rows in calls["replace_strength"] if not rows]
        assert empty == [], f"这些天被拿 [] 调用了 replace_strength：{empty}"


class TestTheWarmupEdgeDoesNotCryWolf:
    """窗口最左端那 ``min_bars - 1`` 天**按定义**排不出横截面，不该告警。

    排序分 ``mom_20`` 的硬闸门是 ``min_bars: 21``，于是本窗口最早的 20 个
    交易日上池内每一只的分数都是 NULL，``compute_strength`` 只能返回空表。
    它**每一跑都在**，而且随窗口右移每天换一批日期。

    实测代价（2026-09-24…09-30）：``daily`` 每一跑都带着
    「20 天算不出横截面」并 ``escalate("partial")`` → exit 1 → 每天都红。
    于是 09-29 那天真正的 ``stale_vendor`` 淹在噪音里没人看见 ——
    正是 ``invariants.sql`` 里写了三遍的那条：
    「长期飘红的断言会把整套补偿策略训练成『反正它总是红的』」。
    """

    def test_it_is_exactly_the_leading_min_bars_minus_one_sessions(self) -> None:
        import pipeline.run_daily as rd

        cfg = load_config()
        sessions = _sessions(60)
        warm = rd._structural_blanks(sessions, sessions[0].date, cfg)
        assert warm == {s.date for s in sessions[:20]}, "mom_20 的 min_bars 是 21"

    def test_a_day_past_the_edge_is_not_claimed_as_structural(self) -> None:
        import pipeline.run_daily as rd

        cfg = load_config()
        sessions = _sessions(60)
        warm = rd._structural_blanks(sessions, sessions[0].date, cfg)
        assert warm is not None
        assert sessions[20].date not in warm, "第 21 根起就该算得出来，排不出就是真失败"

    def test_a_later_start_slides_the_edge_with_it(self) -> None:
        """窗口右移时这段也跟着移 —— 它跟的是 ``start``，不是绝对日期。"""
        import pipeline.run_daily as rd

        cfg = load_config()
        sessions = _sessions(60)
        warm = rd._structural_blanks(sessions, sessions[5].date, cfg)
        assert warm == {s.date for s in sessions[5:25]}

    def test_an_unmeasurable_score_metric_claims_nothing(self) -> None:
        """排序分不是 metrics.yaml 里的指标（如 ``composite``）时返回 ``None``。

        此时调用方照旧全部升级 —— **宁可多告警，不可少告警**。
        """
        import pipeline.run_daily as rd

        class _Metrics:
            @staticmethod
            def by_id(_: str) -> None:
                return None

        class _Strength:
            score_metric = "composite"

        class _Cfg:
            metrics = _Metrics()
            strength = _Strength()

        sessions = _sessions(60)
        assert rd._structural_blanks(sessions, sessions[0].date, _Cfg()) is None  # type: ignore[arg-type]

    def test_a_full_run_notes_the_edge_without_escalating(self, harness: Any) -> None:
        """整跑一次：左端那段只该进 ``note``，**不该**带出升级那句话。"""
        _, monkeypatch, rd = harness
        cfg = load_config()
        sessions = _sessions(60)
        today = sessions[-1].date
        symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
        days = [s.date for s in sessions]
        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(
            rd,
            "fetch_window",
            lambda *a, **k: FetchOutcome(
                frame=_frame(symbols, days),
                per_symbol_source=dict.fromkeys(symbols, "yfinance"),
            ),
        )
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert "窗口最左端" in report.message, "左端那段仍然要如实记一笔"
        assert "算不出横截面，已跳过而非清空" not in report.message, (
            "左端是定义使然，不该走升级那条分支"
        )


class TestAbsentSymbolsKeepTheirHistoricalRankings:
    """本跑缺席的标的，不能被从**历史**榜单里抹掉。

    ``compute_strength`` 的输入是本跑内存里的 metrics，不是库。
    于是整窗缺席（跨源比对剔除 / 两源都没拿到）的标的会被从 rerank 窗口
    覆盖的每一天抹掉，其余名次集体上移 —— 而它在 ``metrics_daily`` 里
    那些天的分数还好端端地在库里。损害是永久的：窗口每天右移，
    掉出去的那些天再也不会被重排。
    """

    @staticmethod
    def _run(harness: Any, *, carry: bool) -> tuple[list[Any], str]:
        calls, monkeypatch, rd = harness
        cfg = load_config()
        sessions = _sessions(60)
        today = sessions[-1].date
        symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
        # **缺席的那个必须是 stock。** `rank_pool: stocks` 会把 ETF（基准 QQQ）
        # 挡在榜单外，拿它当样本时断言恒假 —— 而且基准缺席还会连带毁掉 alpha/beta。
        # 按 type 取，不按下标取：universe.yaml 的顺序不该决定测试是否有意义。
        absent = next(s.symbol for s in cfg.universe.symbols if s.enabled and s.type == "stock")
        present = [s for s in symbols if s != absent]
        days = [s.date for s in sessions]

        _install_plan(rd, monkeypatch, sessions, _no_revision())
        monkeypatch.setattr(
            rd,
            "fetch_window",
            lambda *a, **k: FetchOutcome(
                frame=_frame(present, days),
                per_symbol_source=dict.fromkeys(present, "yfinance"),
                rejected=(absent,),
            ),
        )
        col = cfg.strength.score_metric
        if carry:
            # 库里**有**这个标的的历史分数 —— 它只是本跑没算出来。
            monkeypatch.setattr(
                rd,
                "_carried_scores",
                lambda conn, syms, start, end, c: [
                    {"symbol": absent, "date": d, c: 99.0} for d in days
                ],
            )
        rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        historical = [(d, r) for d, r in calls["replace_strength"] if r and d != today]
        assert historical, "应当有历史天被重排"
        del col
        return historical, absent

    def test_a_carried_score_puts_the_symbol_back_into_the_ranking(self, harness: Any) -> None:
        historical, absent = self._run(harness, carry=True)
        for day, rows in historical:
            assert absent in {r["symbol"] for r in rows}, (
                f"{day} 的榜单把本跑缺席的 {absent} 抹掉了"
            )

    def test_without_the_carry_the_symbol_would_vanish(self, harness: Any) -> None:
        """对照组：``_carried_scores`` 返回空时它确实会消失 ——
        证明上一条断言的是**补回机制**，不是一个恒真的性质。"""
        historical, absent = self._run(harness, carry=False)
        assert all(absent not in {r["symbol"] for r in rows} for _, rows in historical), (
            "没有补回机制时它就是会消失 —— 这正是那条 SERIOUS"
        )
