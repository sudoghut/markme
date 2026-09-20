"""``pipeline/sessions.py`` 的测试（§9.1.4）。

这张表**自己就可能成为新的错数来源**，而它出错时的表现是：
`ordinal` 整体错位 → `mom_20` 的「20 个 session 前」指到错误的行 →
**所有闸门全绿**。所以这里的断言几乎全是关于「无缺口」与「差 1」的。
"""

from __future__ import annotations

from datetime import date, time

import pandas as pd
import pytest

from pipeline.sessions import (
    CALENDAR_NAME,
    Session,
    assert_left_endpoint,
    build_sessions,
    diff_sessions,
    horizon_end,
    load_calendar,
    lookback_start,
    lookback_window,
)


class _FakeCalendar:
    """合成日历。

    真日历测不了「历史日被增减」—— 而那正是 §9.1.4 整节的主题。
    """

    def __init__(self, days: dict[date, time]) -> None:
        self._days = days

    def schedule(self, start_date: object, end_date: object) -> pd.DataFrame:
        lo, hi = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
        rows = sorted((d, t) for d, t in self._days.items() if lo <= d <= hi)
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows])
        closes = [
            pd.Timestamp.combine(pd.Timestamp(d), t).tz_localize("America/New_York")
            for d, t in rows
        ]
        return pd.DataFrame({"market_open": closes, "market_close": closes}, index=idx)


def _cal(*days: str, half: set[str] | None = None) -> _FakeCalendar:
    half = half or set()
    return _FakeCalendar(
        {date.fromisoformat(d): (time(13, 0) if d in half else time(16, 0)) for d in days}
    )


class TestOrdinalIsDerivedNotRemembered:
    def test_ordinals_are_gapless_and_start_at_one(self) -> None:
        cal = _cal("2024-01-02", "2024-01-03", "2024-01-04")
        out = build_sessions(cal, date(2024, 1, 1), date(2024, 1, 31))
        assert [s.ordinal for s in out] == [1, 2, 3]
        assert [s.date for s in out] == [date(2024, 1, d) for d in (2, 3, 4)]

    def test_a_newly_published_holiday_renumbers_everything_after_it(self) -> None:
        """**这是 §9.1.4 要防的那件事。**

        交易所公布一个临时休市日，日历包更新了。沿用旧 ordinal 时，
        「相邻 session」的判断会整体错位，而所有闸门全绿。
        每次重新推导则自动正确。
        """
        before = build_sessions(
            _cal("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"),
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        after = build_sessions(
            _cal("2024-01-02", "2024-01-04", "2024-01-05"),  # 01-03 成了休市日
            date(2024, 1, 1),
            date(2024, 1, 31),
        )
        assert [(s.date.day, s.ordinal) for s in before] == [(2, 1), (3, 2), (4, 3), (5, 4)]
        assert [(s.date.day, s.ordinal) for s in after] == [(2, 1), (4, 2), (5, 3)]
        # 关键：01-05 的 ordinal 从 4 变成 3，而不是沿用 4 留下一个缺口
        assert next(s.ordinal for s in after if s.date.day == 5) == 3

    def test_end_before_start_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="早于"):
            build_sessions(_cal(), date(2024, 2, 1), date(2024, 1, 1))


class TestHalfDays:
    def test_a_half_day_is_flagged_and_its_close_is_read(self) -> None:
        """§7.2 闸门 2 的「收盘 + settle_minutes」完全依赖这个值。
        写死 16:00 会让半日市当天的闸门晚 3 小时放行。"""
        out = build_sessions(
            _cal("2024-07-03", "2024-07-05", half={"2024-07-03"}),
            date(2024, 7, 1),
            date(2024, 7, 31),
        )
        assert (out[0].close_et, out[0].is_half_day) == (time(13, 0), True)
        assert (out[1].close_et, out[1].is_half_day) == (time(16, 0), False)


class TestCalendarRevision:
    TODAY = date(2024, 6, 15)

    def test_horizon_extension_alone_needs_no_repair(self) -> None:
        """**分类是 diff 存在的全部理由。**

        地平线延长每天都会发生。不把它和历史修订分开，每天都会触发一次
        「完整修复」—— 那会把 partial 变成常态，而常态化的告警等于没有告警。
        """
        rev = diff_sessions(
            [date(2024, 6, 14)],
            [date(2024, 6, 14), date(2024, 8, 1)],
            today=self.TODAY,
        )
        assert rev.added_future == (date(2024, 8, 1),)
        assert not rev.needs_repair
        assert rev.repair_from is None
        assert "地平线延长" in rev.describe()

    def test_a_historical_addition_needs_repair_from_that_day(self) -> None:
        rev = diff_sessions(
            [date(2024, 6, 3), date(2024, 6, 14)],
            [date(2024, 6, 3), date(2024, 6, 5), date(2024, 6, 14)],
            today=self.TODAY,
        )
        assert rev.needs_repair
        assert rev.repair_from == date(2024, 6, 5)

    def test_a_historical_removal_needs_repair(self) -> None:
        rev = diff_sessions(
            [date(2024, 6, 3), date(2024, 6, 5)],
            [date(2024, 6, 3)],
            today=self.TODAY,
        )
        assert rev.needs_repair
        assert rev.repair_from == date(2024, 6, 5)

    def test_repair_starts_at_the_earliest_change_of_either_kind(self) -> None:
        rev = diff_sessions(
            [date(2024, 6, 3), date(2024, 6, 10)],
            [date(2024, 6, 5), date(2024, 6, 10)],
            today=self.TODAY,
        )
        assert rev.repair_from == date(2024, 6, 3)  # 消失的那天更早

    def test_a_change_on_today_itself_is_historical(self) -> None:
        """**边界就是这个函数的全部意义，而它之前一条测试都没有。**

        实测：把 ``d <= today`` 改成 ``d < today``（并把 ``>`` 改成 ``>=``）
        能通过全部 56 条旧测试。而逃掉的正是 §9.1.4 的失败模式：
        交易所宣布**当日**临时休市，这一天从日历里消失 →
        被归成 ``removed_future`` → ``needs_repair`` 为 False →
        今天已经算出来的 metrics/strength 行留在表里，所有闸门全绿。
        """
        removed = diff_sessions([self.TODAY], [], today=self.TODAY)
        assert removed.removed_historical == (self.TODAY,)
        assert removed.removed_future == ()
        assert removed.needs_repair and removed.repair_from == self.TODAY

        added = diff_sessions([], [self.TODAY], today=self.TODAY)
        assert added.added_historical == (self.TODAY,)
        assert added.added_future == ()
        assert added.needs_repair

    def test_no_change_says_so(self) -> None:
        rev = diff_sessions([date(2024, 6, 3)], [date(2024, 6, 3)], today=self.TODAY)
        assert not rev.needs_repair
        assert rev.describe() == "日历无变化"

    def test_it_accepts_sessions_or_dates(self) -> None:
        sess = build_sessions(_cal("2024-06-03"), date(2024, 6, 1), date(2024, 6, 30))
        rev = diff_sessions(sess, [date(2024, 6, 3)], today=self.TODAY)
        assert not rev.needs_repair


class TestLookbackStart:
    def _sessions(self, n: int) -> list[Session]:
        days = pd.bdate_range("2024-01-01", periods=n).date
        return build_sessions(
            _cal(*[d.isoformat() for d in days]), days[0], days[-1] + pd.Timedelta(days=1)
        )

    def test_it_counts_bars_not_calendar_days(self) -> None:
        """**400 个交易日 ≈ 574 个自然日。**

        随手写 `start = today - 400 days` 只会拿到约 275 根，EMA 的喂入量
        直接掉到 360 以下（§12 #5 标准 B），而没有任何东西会报警。
        """
        sessions = self._sessions(500)
        today = sessions[-1].date
        start = lookback_start(sessions, today, 400)
        n_bars = sum(1 for s in sessions if start <= s.date <= today)
        assert n_bars == 400
        assert (today - start).days > 400, "400 个交易日必然跨过 400 个自然日"

    def test_a_weekend_today_anchors_on_the_last_session(self) -> None:
        """backfill.yml 是 dispatch 触发的，周末跑很正常 —— 不该抛。"""
        sessions = self._sessions(50)
        saturday = sessions[-1].date + pd.Timedelta(days=1)
        assert lookback_start(sessions, saturday, 10) == sessions[-10].date

    def test_a_short_history_returns_the_earliest_day(self) -> None:
        sessions = self._sessions(5)
        assert lookback_start(sessions, sessions[-1].date, 400) == sessions[0].date

    def test_a_today_before_every_session_is_loud(self) -> None:
        sessions = self._sessions(5)
        with pytest.raises(ValueError, match="sessions_start_date"):
            lookback_start(sessions, date(2000, 1, 1), 10)

    def test_nonpositive_lookback_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="lookback_bars"):
            lookback_start(self._sessions(5), date(2024, 1, 5), 0)


class TestItRefusesToGuessTheTimezone:
    def test_a_naive_market_close_is_rejected(self) -> None:
        """市场日历给的无时区时间压倒性地是**交易所本地**时间。

        把它当 UTC 会让一个 16:00 的常规收盘变成 11:00 ET 并被标成半日市，
        于是闸门 2 提前 5 小时放行 —— 一个看起来完全合理的行。
        """

        class _NaiveCalendar:
            def schedule(self, start_date: object, end_date: object) -> pd.DataFrame:
                idx = pd.DatetimeIndex([pd.Timestamp("2024-01-02")])
                return pd.DataFrame(
                    {
                        "market_open": [pd.Timestamp("2024-01-02 09:30")],
                        "market_close": [pd.Timestamp("2024-01-02 16:00")],
                    },
                    index=idx,
                )

        with pytest.raises(ValueError, match="没有时区"):
            build_sessions(_NaiveCalendar(), date(2024, 1, 1), date(2024, 1, 31))


class TestForwardHorizon:
    def test_it_counts_trading_days_not_calendar_days(self) -> None:
        """和 ``lookback_start`` 对称的一对。

        没有它，调用方最顺手的写法是 ``today + timedelta(days=60)`` ——
        实测那只有约 45 个交易日。
        """
        cal = load_calendar()
        today = date(2026, 9, 21)
        end = horizon_end(cal, today, 60)
        n = len([s for s in build_sessions(cal, today, end) if s.date > today])
        assert n == 60
        assert (end - today).days > 60, "60 个交易日必然跨过 60 个自然日"

    def test_nonpositive_horizon_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sessions_horizon"):
            horizon_end(load_calendar(), date(2026, 9, 21), 0)


class TestLeftEndpointIsPinned:
    def test_a_moved_left_endpoint_is_loud(self) -> None:
        """``build_sessions`` 接受**任意** start 并无条件从 1 重新编号，
        而「ordinal 无缺口」那条不变式抓不到整体重编号 —— 它依然是无缺口的。
        所以这一条必须显式断言。"""
        sessions = build_sessions(
            _cal("2024-01-03", "2024-01-04"), date(2024, 1, 1), date(2024, 1, 31)
        )
        assert_left_endpoint(sessions, date(2024, 1, 3))
        with pytest.raises(ValueError, match="左端点"):
            assert_left_endpoint(sessions, date(2024, 1, 2))

    def test_an_empty_list_is_loud(self) -> None:
        with pytest.raises(ValueError, match="为空"):
            assert_left_endpoint([], date(2024, 1, 2))


class TestLookbackWindow:
    def test_it_reports_the_achieved_bar_count(self) -> None:
        """只返回一个日期时，调用方分不清「历史本来就短」和
        「拿到的 session 列表是残缺的」—— 而 §6.1.1 点名的正是后者。"""
        days = pd.bdate_range("2024-01-01", periods=30).date
        sessions = build_sessions(
            _cal(*[d.isoformat() for d in days]), days[0], days[-1] + pd.Timedelta(days=1)
        )
        _, n = lookback_window(sessions, sessions[-1].date, 10)
        assert n == 10
        _, short = lookback_window(sessions, sessions[-1].date, 400)
        assert short == 30, "窗口不够时要说出实际拿到多少根"


class TestOrdinalsAreSorted:
    def test_an_out_of_order_calendar_still_yields_ascending_ordinals(self) -> None:
        """§9.1.4 的原话是「由**排序后的** session 序列重新推导」。

        真日历总是升序，所以这在生产上是空操作 —— 但这个模块故意暴露了
        Protocol 好让测试塞合成日历，而乱序的合成日历会**静默**产出
        错误的 ordinal，正是 §9.1.4 要防的「整体错位，所有闸门全绿」。
        """

        class _Shuffled:
            def schedule(self, start_date: object, end_date: object) -> pd.DataFrame:
                days = [date(2024, 1, 4), date(2024, 1, 2), date(2024, 1, 3)]
                idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
                closes = [
                    pd.Timestamp.combine(pd.Timestamp(d), time(16, 0)).tz_localize(
                        "America/New_York"
                    )
                    for d in days
                ]
                return pd.DataFrame({"market_open": closes, "market_close": closes}, index=idx)

        out = build_sessions(_Shuffled(), date(2024, 1, 1), date(2024, 1, 31))
        assert [(s.date.day, s.ordinal) for s in out] == [(2, 1), (3, 2), (4, 3)]


class TestTheRealCalendar:
    """对真日历的断言 —— 设计文档的 `XNAS` 在这里会直接崩。"""

    def test_the_documented_calendar_code_does_not_exist(self) -> None:
        """§7.2 初稿写的 `XNAS` 不在 registry 里。

        按文档字面实现，闸门 1 会在第一次运行时抛 RuntimeError。
        这条测试把那个事实钉住，免得有人「照文档改回去」。
        """
        with pytest.raises(RuntimeError, match="not one of the registered"):
            load_calendar("XNAS")

    def test_nasdaq_is_the_nyse_calendar(self) -> None:
        """这个包里 NASDAQ 与 NYSE 本来就是同一个类 ——
        文档里那个「明示的选择」实际上不存在。"""
        assert load_calendar(CALENDAR_NAME).name == "NYSE"

    def test_a_known_half_day_and_holiday(self) -> None:
        sessions = build_sessions(load_calendar(), date(2024, 7, 1), date(2024, 7, 8))
        by_date = {s.date: s for s in sessions}
        assert by_date[date(2024, 7, 3)].close_et == time(13, 0)
        assert by_date[date(2024, 7, 3)].is_half_day
        assert date(2024, 7, 4) not in by_date, "独立日不是交易日"
        assert by_date[date(2024, 7, 5)].close_et == time(16, 0)

    def test_ordinals_are_contiguous_over_a_real_year(self) -> None:
        sessions = build_sessions(load_calendar(), date(2024, 1, 1), date(2024, 12, 31))
        ordinals = [s.ordinal for s in sessions]
        assert ordinals == list(range(1, len(sessions) + 1))
        assert 250 <= len(sessions) <= 253, f"美股一年约 252 个交易日，得到 {len(sessions)}"
