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

import contextlib
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
import pytest

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
                    "close": price + i * 0.1,
                    "adj_close": price + i * 0.1,
                    "source": "yfinance",
                }
            )
    return pd.DataFrame(rows)


def _no_revision() -> CalendarRevision:
    return CalendarRevision(
        added_future=(), removed_future=(), added_historical=(), removed_historical=()
    )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把 ``run_once`` 的所有 I/O 换成可观测的假货。"""
    from pipeline import run_daily as rd

    calls: dict[str, list[Any]] = {"replace_strength": [], "upsert_metrics": [], "revalidate": []}

    monkeypatch.setattr(rd, "write_sessions", lambda *a, **k: None)
    monkeypatch.setattr(rd, "write_symbols", lambda *a, **k: None)
    monkeypatch.setattr(rd, "upsert_prices", lambda conn, rows: len(rows))
    monkeypatch.setattr(rd, "touch_fetch_state", lambda *a, **k: None)
    monkeypatch.setattr(rd, "revalidate_site", lambda r: calls["revalidate"].append(r))
    monkeypatch.setattr(rd, "_existing_prices", lambda *a, **k: {})
    monkeypatch.setattr(rd, "_already_done", lambda *a, **k: False)
    monkeypatch.setattr(rd, "_carried_scores", lambda *a, **k: [])
    monkeypatch.setattr(rd, "_refresh_events", lambda *a, **k: ({}, True))
    monkeypatch.setattr(rd, "data_transaction", lambda conn: contextlib.nullcontext(conn))

    def _upsert_metrics(conn: Any, rows: list[dict[str, Any]]) -> int:
        calls["upsert_metrics"].append(rows)
        return len(rows)

    def _replace_strength(conn: Any, day: date, rows: list[dict[str, Any]]) -> int:
        calls["replace_strength"].append((day, rows))
        return len(rows)

    monkeypatch.setattr(rd, "upsert_metrics", _upsert_metrics)
    monkeypatch.setattr(rd, "replace_strength", _replace_strength)
    return calls, monkeypatch, rd


def _install_plan(
    rd: Any, monkeypatch: Any, sessions: list[Session], rev: CalendarRevision
) -> None:
    monkeypatch.setattr(rd, "plan_sessions", lambda conn, cfg, today: (sessions, rev))


class TestRepairNeverUsesTodaysUnsettledBar:
    """闸门 2 被 ``needs_repair`` 顶开时，**不能**拿今天那根还没定稿的 bar。

    16:00 ET 那条 cron 正好是敲钟那一刻，``settle_minutes: 60`` 一分钟都没过。
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

        # 敲钟那一刻：今天收盘 16:00，settle 60 分钟 → 今天还没定稿。
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
        now = datetime.combine(today, time(17, 30), tzinfo=ET)
        report = rd.run_once(FakeConn(), cfg, now=now)
        assert report.session_date == today


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
        now = datetime.combine(today, time(17, 30), tzinfo=ET)
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
        now = datetime.combine(today, time(17, 30), tzinfo=ET)
        report = rd.run_once(FakeConn(), cfg, now=now)

        assert report.status in {"ok", "partial", "ok_events_stale", "stale_vendor"}
        empty = [day for day, rows in calls["replace_strength"] if not rows]
        assert empty == [], f"这些天被拿 [] 调用了 replace_strength：{empty}"


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
        rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(17, 30), tzinfo=ET))
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
