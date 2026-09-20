"""``pipeline/calendar_gate.py`` 的测试（§7.2 闸门 1/2/3）。

**M5 的 DST 测试是里程碑表里最重要的一条验收标准**（§11）：
§13 把夏令时列为「数据错误且不易察觉」，而你**无法靠等待来验证它** ——
要等到 3 月或 11 月。冻结时钟的单元测试是唯一能在今天就知道
冬令时那几条 cron 写对没有的办法。这里先把闸门侧的那一半钉住。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import ClassVar

import pytest

from pipeline.calendar_gate import (
    ET,
    gate_opens_at,
    interior_gaps,
    session_on_or_before,
    stale_symbols,
    when_to_run,
)
from pipeline.sessions import Session

SETTLE = 60


def _s(d: str, close: time = time(16, 0), ordinal: int = 1) -> Session:
    return Session(
        date=date.fromisoformat(d),
        ordinal=ordinal,
        close_et=close,
        is_half_day=close < time(16, 0),
    )


def _et(d: str, hh: int, mm: int) -> datetime:
    return datetime.combine(date.fromisoformat(d), time(hh, mm), tzinfo=ET)


class TestGateOneAndTwo:
    SESSIONS: ClassVar[list[Session]] = [
        _s("2024-06-12"),
        _s("2024-07-03", close=time(13, 0), ordinal=2),
    ]

    def test_a_non_session_day_is_a_holiday_skip(self) -> None:
        out = when_to_run(self.SESSIONS, _et("2024-06-15", 18, 0), SETTLE)
        assert out.decision == "skipped_holiday"
        assert not out.should_run

    def test_before_close_plus_settle_is_too_early(self) -> None:
        out = when_to_run(self.SESSIONS, _et("2024-06-12", 16, 30), SETTLE)
        assert out.decision == "skipped_too_early"
        assert out.opens_at == _et("2024-06-12", 17, 0)

    def test_exactly_at_the_gate_runs(self) -> None:
        assert when_to_run(self.SESSIONS, _et("2024-06-12", 17, 0), SETTLE).should_run

    def test_after_the_gate_runs(self) -> None:
        assert when_to_run(self.SESSIONS, _et("2024-06-12", 17, 1), SETTLE).should_run

    def test_a_half_day_gate_adapts_automatically(self) -> None:
        """半日市 13:00 收盘则闸门 14:00 ET 放行，规则自动适配。

        写死 16:00 会让半日市当天晚 3 小时 —— 而管道 17:00 才跑，
        所以这个错误在生产上**永远不会表现出来**，只会在某次改动之后
        突然变成「半日市当天没数据」。
        """
        assert gate_opens_at(self.SESSIONS[1], SETTLE) == _et("2024-07-03", 14, 0)
        assert when_to_run(self.SESSIONS, _et("2024-07-03", 14, 0), SETTLE).should_run
        assert not when_to_run(self.SESSIONS, _et("2024-07-03", 13, 59), SETTLE).should_run

    def test_a_naive_now_is_rejected(self) -> None:
        """裸 datetime 是一整类时区 bug 的入口，而它们的表现是
        「闸门早放行三小时」这种看起来完全正常的行为。"""
        with pytest.raises(ValueError, match="必须带时区"):
            when_to_run(self.SESSIONS, datetime(2024, 6, 12, 17, 0), SETTLE)

    def test_the_reason_names_the_half_day(self) -> None:
        out = when_to_run(self.SESSIONS, _et("2024-07-03", 13, 30), SETTLE)
        assert "半日市" in out.reason


class TestDaylightSaving:
    """**这组是 §13「数据错误且不易察觉」那一条的直接对应。**

    17:00 ET 在 EDT 是 21:00 UTC，在 EST 是 22:00 UTC。用 UTC 写死 cron 的
    实现会在换季那天整整错一小时 —— 而错的方向是「太早」，
    于是闸门 2 判 ``skipped_too_early``，当天**零数据且不告警**。
    """

    @pytest.mark.parametrize(
        ("day", "utc_hour", "expect_run"),
        [
            # EDT（UTC-4）：17:00 ET == 21:00 UTC
            ("2024-06-12", 21, True),
            ("2024-06-12", 20, False),
            # EST（UTC-5）：17:00 ET == 22:00 UTC
            ("2024-12-11", 22, True),
            ("2024-12-11", 21, False),
        ],
    )
    def test_the_same_utc_cron_lands_differently_across_dst(
        self, day: str, utc_hour: int, expect_run: bool
    ) -> None:
        from zoneinfo import ZoneInfo

        sessions = [_s(day)]
        now = datetime.combine(date.fromisoformat(day), time(utc_hour, 0), tzinfo=ZoneInfo("UTC"))
        assert when_to_run(sessions, now, SETTLE).should_run is expect_run

    def test_the_gate_is_computed_in_wall_clock_not_utc_offset(self) -> None:
        """两个季节的闸门都是 17:00 **ET 墙上时间**，UTC 偏移自己会变。"""
        summer = gate_opens_at(_s("2024-06-12"), SETTLE)
        winter = gate_opens_at(_s("2024-12-11"), SETTLE)
        assert summer.hour == winter.hour == 17
        assert summer.utcoffset() != winter.utcoffset()


class TestGateThree:
    """闸门 3：**逐标的**，不是只看基准。"""

    DAY = date(2024, 6, 12)

    def test_all_fresh_is_empty(self) -> None:
        assert stale_symbols({"QQQ": self.DAY, "AAPL": self.DAY}, self.DAY) == ()

    def test_one_lagging_symbol_is_caught_even_when_the_benchmark_is_fine(self) -> None:
        """**这是初稿那个洞。**

        只断言 QQQ 时，AVGO 拿到昨天的 bar 既不算抓取失败、也过得了闸门 ——
        而排名是横截面的，一个错位一天的窗口会和 15 个日期正确的同行一起排。
        """
        latest = {"QQQ": self.DAY, "AVGO": self.DAY - timedelta(days=1), "AAPL": self.DAY}
        assert stale_symbols(latest, self.DAY) == ("AVGO",)

    def test_a_missing_symbol_counts_as_stale(self) -> None:
        assert stale_symbols({"QQQ": self.DAY, "MU": None}, self.DAY) == ("MU",)

    def test_a_future_bar_is_also_stale(self) -> None:
        """日期**不等于**当日 session 就是不对，不只是「落后」。
        一个超前的 bar 同样会让横截面混日期。"""
        latest = {"QQQ": self.DAY + timedelta(days=1)}
        assert stale_symbols(latest, self.DAY) == ("QQQ",)

    def test_the_result_is_sorted_for_stable_messages(self) -> None:
        latest = {s: self.DAY - timedelta(days=1) for s in ("MU", "AAPL", "NVDA")}
        assert stale_symbols(latest, self.DAY) == ("AAPL", "MU", "NVDA")


class TestInteriorGaps:
    """闸门 3 的**另一半**。

    ``stale_symbols`` 只看最新一根，于是窗口中间的空洞完整地过掉闸门 ——
    而后果是安静的：收益样本被悄悄缩短，alpha/beta 按日期对齐会丢掉空洞
    两侧那两天，却仍可能满足 ``min_obs`` 并给出一个看起来完全合理的数。
    """

    SESSIONS: ClassVar[list[date]] = [date(2024, 6, d) for d in (3, 4, 5, 6, 7)]

    def test_a_complete_window_has_no_gaps(self) -> None:
        assert interior_gaps({"A": self.SESSIONS}, self.SESSIONS) == {}

    def test_an_interior_hole_is_caught(self) -> None:
        bars = [d for d in self.SESSIONS if d != date(2024, 6, 5)]
        assert interior_gaps({"A": bars}, self.SESSIONS) == {"A": 1}

    def test_several_holes_are_counted(self) -> None:
        bars = [d for d in self.SESSIONS if d.day not in (4, 6)]
        assert interior_gaps({"A": bars}, self.SESSIONS) == {"A": 2}

    def test_a_short_history_is_not_a_gap(self) -> None:
        """**历史本来就短不是缺口。**

        新加入的标的由 §3.3 的 provisional 灰标负责；把它算成缺口会让它
        在补够历史之前**每天都 partial** —— 而长期飘红的告警等于没有告警。
        """
        assert interior_gaps({"A": self.SESSIONS[2:]}, self.SESSIONS) == {}

    def test_a_missing_tail_is_left_to_gate_three(self) -> None:
        """末尾缺失是 ``stale_symbols`` 的职责，不在这里重复报。"""
        assert interior_gaps({"A": self.SESSIONS[:3]}, self.SESSIONS) == {}

    def test_an_empty_symbol_is_skipped(self) -> None:
        assert interior_gaps({"A": []}, self.SESSIONS) == {}


class TestSessionLookup:
    SESSIONS: ClassVar[list[Session]] = [_s("2024-06-10", ordinal=1), _s("2024-06-11", ordinal=2)]

    def test_exact_match(self) -> None:
        got = session_on_or_before(self.SESSIONS, date(2024, 6, 11))
        assert got is not None and got.date == date(2024, 6, 11)

    def test_a_weekend_falls_back_to_the_last_session(self) -> None:
        got = session_on_or_before(self.SESSIONS, date(2024, 6, 15))
        assert got is not None and got.date == date(2024, 6, 11)

    def test_before_everything_is_none(self) -> None:
        assert session_on_or_before(self.SESSIONS, date(2020, 1, 1)) is None
