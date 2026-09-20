"""``pipeline/fetch_events.py`` 的测试（§3.5）。

§3.5 的六条规则里，最值钱的两条都关于**沉默**：
一条已作废的预告行会让倒计时走向一个不存在的日子，到期后翻成「财报后 1 天」
—— **播报一场从未发生的财报**，再过几天数字又自己对了，于是事后更难发现；
而事件抓取失败若记成 `partial`，会让好数据被更粗的源静默覆盖。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import pytest

from pipeline.fetch_events import (
    EARNINGS_COVERAGE_DAYS,
    SymbolEvent,
    coverage_start,
    distances_for,
    fetch_symbol_events,
    should_refresh,
    to_metric_events,
)
from pipeline.throttle import RequestBudget

TODAY = date(2024, 6, 12)
SESSIONS_START = date(2024, 1, 2)


def _budget(**kw: Any) -> RequestBudget:
    opts: dict[str, Any] = {
        "max_requests": 100,
        "interval_seconds": 0,
        "retry_max_attempts": 1,
        "sleep": lambda _s: None,
        "monotonic": lambda: 0.0,
    }
    opts.update(kw)
    return RequestBudget(**opts)


def _ev(etype: str, d: date, est: bool = False) -> SymbolEvent:
    return SymbolEvent(symbol="X", event_type=etype, event_date=d, is_estimated=est)  # type: ignore[arg-type]


class TestCoverageWindow:
    """§3.5(1)：**覆盖窗口由端点契约给出，不由「本次返回了什么」给出。**"""

    def test_earnings_window_is_fixed_not_data_derived(self) -> None:
        """拿「本次最早日期」当左端点时，一次返回空集的抓取会让删除无从下手
        —— 于是那条**已作废的未来事件永远留在库里**。"""
        assert coverage_start("earnings", TODAY, SESSIONS_START) == TODAY - timedelta(
            days=EARNINGS_COVERAGE_DAYS
        )

    def test_dividend_window_starts_at_sessions_start(self) -> None:
        """``Ticker.dividends`` 给全历史，所以左端点就是 sessions_start_date。"""
        assert coverage_start("dividend", TODAY, SESSIONS_START) == SESSIONS_START


class TestRefreshCadence:
    """§3.5(4) 的刷新节奏。"""

    def _call(self, **kw: Any) -> bool:
        opts: dict[str, Any] = {
            "today": TODAY,  # 2024-06-12 是周三
            "is_trading_day": True,
            "last_fetch_at": None,
            "nearest_event": None,
            "refresh_weekday": 3,
            "within_days": 10,
        }
        opts.update(kw)
        return should_refresh(**opts)

    def test_a_near_event_forces_a_daily_refresh(self) -> None:
        assert self._call(nearest_event=TODAY + timedelta(days=3), last_fetch_at=TODAY)

    def test_a_near_event_overrides_even_a_non_trading_day(self) -> None:
        assert self._call(nearest_event=TODAY + timedelta(days=1), is_trading_day=False)

    def test_a_far_event_does_not_force_a_refresh(self) -> None:
        assert not self._call(nearest_event=TODAY + timedelta(days=40), last_fetch_at=TODAY)

    def test_weekly_cadence_on_the_configured_weekday(self) -> None:
        assert self._call()  # 周三，没抓过

    def test_already_fetched_this_week_is_skipped(self) -> None:
        assert not self._call(last_fetch_at=TODAY - timedelta(days=2))

    def test_a_non_trading_day_defers(self) -> None:
        """非交易日则顺延到下一个交易日。"""
        assert not self._call(is_trading_day=False)

    def test_an_event_in_the_past_does_not_trigger(self) -> None:
        """只有**未来**事件临近才改为每日刷新。"""
        assert not self._call(nearest_event=TODAY - timedelta(days=1), last_fetch_at=TODAY)


class TestThreeEndpoints:
    """§3.5(4)：三个端点，不是两个。"""

    def _fetch(self, **kw: Any) -> Any:
        opts: dict[str, Any] = {
            "calendar_fn": lambda s: {
                "Earnings Date": [TODAY + timedelta(days=20)],
                "Ex-Dividend Date": [TODAY + timedelta(days=5)],
            },
            "earnings_dates_fn": lambda s: [TODAY - timedelta(days=70)],
            "dividends_fn": lambda s: pd.Series(
                [0.25], index=pd.DatetimeIndex([pd.Timestamp("2024-03-08")])
            ),
        }
        opts.update(kw)
        return fetch_symbol_events(
            "X", today=TODAY, sessions_start=SESSIONS_START, budget=_budget(), **opts
        )

    def test_all_three_are_called_and_cost_three_requests(self) -> None:
        """每标的 3 次请求 × 17 ≈ 51 次 —— §3.5(4) 的预算核对基数。"""
        b = _budget()
        out = fetch_symbol_events(
            "X",
            today=TODAY,
            sessions_start=SESSIONS_START,
            budget=b,
            calendar_fn=lambda s: {"Earnings Date": [TODAY + timedelta(days=20)]},
            earnings_dates_fn=lambda s: [TODAY - timedelta(days=70)],
            dividends_fn=lambda s: pd.Series(dtype="float64"),
        )
        assert out.ok
        assert b.used == 3

    def test_history_supplies_days_since_last_earnings(self) -> None:
        """初稿只写了两个端点，于是 ``days_since_last_earnings``
        **根本没有数据源**。"""
        out = self._fetch()
        past = [e for e in out.events if e.event_type == "earnings" and e.event_date < TODAY]
        assert past, "没有历史财报行 → days_since_last_earnings 无从算起"

    def test_historical_dividends_are_facts_not_estimates(self) -> None:
        """§3.5(2)：把 settled 的事实标成「估计」是另一种不诚实，
        而且会让虚线到处都是、从而失去意义。"""
        out = self._fetch()
        hist_div = [e for e in out.events if e.event_type == "dividend" and e.event_date < TODAY]
        assert hist_div and all(not e.is_estimated for e in hist_div)

    def test_a_future_date_is_an_estimate(self) -> None:
        out = self._fetch()
        future = [e for e in out.events if e.event_date > TODAY]
        assert future and all(e.is_estimated for e in future)

    def test_dividend_amounts_are_kept(self) -> None:
        out = self._fetch()
        paid = [e for e in out.events if e.event_type == "dividend" and e.amount is not None]
        assert paid and paid[0].amount == pytest.approx(0.25)

    def test_a_confirmed_row_wins_over_an_estimated_duplicate(self) -> None:
        """``calendar`` 与 ``earnings_dates`` 可能覆盖到同一天。
        两条都留下会让 §3.5(1) 的孤儿行不变式报警。"""
        d = TODAY - timedelta(days=3)
        out = fetch_symbol_events(
            "X",
            today=TODAY,
            sessions_start=SESSIONS_START,
            budget=_budget(),
            calendar_fn=lambda s: {"Earnings Date": [d]},
            earnings_dates_fn=lambda s: [d],
            dividends_fn=lambda s: pd.Series(dtype="float64"),
        )
        same = [e for e in out.events if e.event_type == "earnings" and e.event_date == d]
        assert len(same) == 1

    def test_at_most_one_future_row_per_event_type(self) -> None:
        """**§3.5(1) 的孤儿行不变式，实测逼出来的那条。**

        AAPL 的 ``calendar`` 说下一次财报是 10-30，``earnings_dates`` 说 10-29
        —— 同一件事，两个端点差一天。两条都留下，17 个标的里有 14 个会
        触发那条不变式，而后果是倒计时指向更早的那个日期、到期翻成
        「财报后 1 天」，**播报一场从未发生的财报**。
        """
        out = fetch_symbol_events(
            "X",
            today=TODAY,
            sessions_start=SESSIONS_START,
            budget=_budget(),
            calendar_fn=lambda s: {"Earnings Date": [TODAY + timedelta(days=48)]},
            earnings_dates_fn=lambda s: [
                TODAY - timedelta(days=40),
                TODAY + timedelta(days=47),
            ],
            dividends_fn=lambda s: pd.Series(dtype="float64"),
        )
        future = [e for e in out.events if e.event_date > TODAY]
        assert len(future) == 1
        assert future[0].event_date == TODAY + timedelta(days=47), "保留最早的那条"
        assert any(e.event_date < TODAY for e in out.events), "历史行一条都不能少"


class TestFailureDoesNotTouchGoodData:
    """§3.5(1)/(4)：**抓取失败时整个 delete+insert 都不执行。**"""

    def test_any_endpoint_failing_makes_the_whole_fetch_not_ok(self) -> None:
        for bad in ("calendar_fn", "earnings_dates_fn", "dividends_fn"):
            kw: dict[str, Any] = {
                "calendar_fn": lambda s: {},
                "earnings_dates_fn": lambda s: [],
                "dividends_fn": lambda s: pd.Series(dtype="float64"),
            }

            def boom(s: str) -> Any:
                raise RuntimeError("vendor 503")

            kw[bad] = boom
            out = fetch_symbol_events(
                "X", today=TODAY, sessions_start=SESSIONS_START, budget=_budget(), **kw
            )
            assert not out.ok, bad
            assert out.error and "503" in out.error
            assert out.coverage == {}, "不 ok 就不该给出覆盖窗口 —— 调用方据此跳过删+插"

    def test_an_empty_but_successful_fetch_still_carries_coverage(self) -> None:
        """**即便本次要插入 0 行，delete 也照常执行**（§3.5(1)）。

        这正是「财报被取消」那条路径：返回空集，而那条已作废的未来行
        必须被删掉。
        """
        out = fetch_symbol_events(
            "X",
            today=TODAY,
            sessions_start=SESSIONS_START,
            budget=_budget(),
            calendar_fn=lambda s: {},
            earnings_dates_fn=lambda s: [],
            dividends_fn=lambda s: pd.Series(dtype="float64"),
        )
        assert out.ok
        assert out.events == ()
        assert out.coverage["earnings"] == TODAY - timedelta(days=EARNINGS_COVERAGE_DAYS)


class TestVendorShapes:
    """供应商给的形状千奇百怪，压成日期列表这一步不能炸。"""

    @pytest.mark.parametrize(
        "value",
        [
            None,
            [],
            pd.Series(dtype="float64"),
            pd.DatetimeIndex([]),
            pd.Timestamp("2024-06-20"),
            [pd.Timestamp("2024-06-20")],
            pd.Series([1.0], index=pd.DatetimeIndex([pd.Timestamp("2024-06-20")])),
        ],
    )
    def test_calendar_shapes_do_not_crash(self, value: Any) -> None:
        out = fetch_symbol_events(
            "X",
            today=TODAY,
            sessions_start=SESSIONS_START,
            budget=_budget(),
            calendar_fn=lambda s: {"Earnings Date": value},
            earnings_dates_fn=lambda s: [],
            dividends_fn=lambda s: pd.Series(dtype="float64"),
        )
        assert out.ok

    def test_nat_is_dropped_not_passed_through(self) -> None:
        """**实测：``NaT.date()`` 不抛异常，它返回 ``NaT``。**

        于是一个缺失日期会伪装成一个「日期」一路走到写库。
        """
        out = fetch_symbol_events(
            "X",
            today=TODAY,
            sessions_start=SESSIONS_START,
            budget=_budget(),
            calendar_fn=lambda s: {"Earnings Date": [pd.NaT, pd.Timestamp("2024-06-20")]},
            earnings_dates_fn=lambda s: [],
            dividends_fn=lambda s: pd.Series(dtype="float64"),
        )
        assert [e.event_date for e in out.events] == [date(2024, 6, 20)]


class TestTheAdapterToM2:
    """§3.5(6) 的四个参数**不在这里实现**。

    M2 的 ``pipeline/metrics/events.py`` 已经有语义完全相同的
    ``event_distances``（含「今天」那两条边界）。再写一份就是
    §6.1.1 那句「两个要对齐的地方，就是将来会不对齐的地方」——
    而这四个参数正是 §3.5 花了整节论证「错了很难发现」的东西。
    """

    def test_it_splits_by_event_type(self) -> None:
        evs = [_ev("earnings", TODAY), _ev("dividend", TODAY + timedelta(days=2), True)]
        assert [e.event_date for e in to_metric_events(evs, "earnings")] == [TODAY]
        assert [e.event_date for e in to_metric_events(evs, "dividend")] == [
            TODAY + timedelta(days=2)
        ]

    def test_it_carries_the_estimated_flag_through(self) -> None:
        out = to_metric_events([_ev("earnings", TODAY, True)], "earnings")
        assert out[0].is_estimated is True

    def test_the_day_of_the_event_is_not_self_contradictory(self) -> None:
        """**这是这四个参数一年中唯一真正被人盯着看的那一天。**

        财报当天 ``days_since_last_earnings = 0``，而 ``days_to_next_earnings``
        指向下一季 —— 不会出现「距财报 0 天」和「财报后 0 天」同时显示。
        （语义由 M2 保证，这里只确认接线没接反。）
        """
        d = distances_for(
            [_ev("earnings", TODAY), _ev("earnings", TODAY + timedelta(days=91), True)], TODAY
        )
        assert d.days_since_last_earnings == 0
        assert d.days_to_next_earnings == 91
        assert d.next_earnings_is_estimated is True

    def test_no_known_future_event_is_null_not_a_big_number(self) -> None:
        """§3.5(2)：没有任何已知未来事件时写 ``NULL``，**不写一个大数字**。"""
        d = distances_for([_ev("earnings", TODAY - timedelta(days=5))], TODAY)
        assert d.days_to_next_earnings is None
        assert d.next_earnings_date is None
        assert d.days_since_last_earnings == 5

    def test_dividends_do_not_leak_into_earnings(self) -> None:
        d = distances_for([_ev("dividend", TODAY + timedelta(days=3), True)], TODAY)
        assert d.days_to_next_earnings is None
        assert d.days_to_next_dividend == 3
