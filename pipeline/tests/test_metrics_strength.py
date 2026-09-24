"""三强股排名与事件距离的测试（§4 / §3.5）。

排名这边最要紧的一条：**NaN 才是真正的确定性风险，不是并列。**
浮点数精确并列是零测度事件；而一个只有 8 根 bar 的新标的，``mom_20`` 就是 NaN，
``sorted(..., reverse=True)`` 把它放到哪里取决于比较链的顺序 ——
**跨输入顺序不确定**。
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from pipeline.metrics.events import Event, EventDistances, event_distances
from pipeline.metrics.strength import is_squeaky, rank_pool


class TestRanking:
    def test_basic_descending_order(self) -> None:
        rows = rank_pool({"A": 0.1, "B": 0.3, "C": 0.2}, top_n=2)
        assert [r.symbol for r in rows] == ["B", "C", "A"]
        assert [r.rank for r in rows] == [1, 2, 3]
        assert [r.in_top_n for r in rows] == [True, True, False]

    def test_delta_to_next(self) -> None:
        rows = rank_pool({"A": 0.10, "B": 0.30, "C": 0.25}, top_n=2)
        assert rows[0].delta_to_next == pytest.approx(0.05)  # B - C
        assert rows[1].delta_to_next == pytest.approx(0.15)  # C - A
        # 最后一名没有「下一名」——写 None，不写 0。
        assert rows[2].delta_to_next is None

    def test_delta_to_median(self) -> None:
        rows = rank_pool({"A": 0.1, "B": 0.3, "C": 0.2}, top_n=1)
        by_sym = {r.symbol: r for r in rows}
        assert by_sym["B"].delta_to_median == pytest.approx(0.1)  # 0.3 - 中位 0.2
        assert by_sym["C"].delta_to_median == pytest.approx(0.0)
        assert by_sym["A"].delta_to_median == pytest.approx(-0.1)

    def test_top_n_larger_than_pool(self) -> None:
        rows = rank_pool({"A": 0.1, "B": 0.2}, top_n=5)
        assert all(r.in_top_n for r in rows)

    def test_rejects_bad_top_n(self) -> None:
        with pytest.raises(ValueError, match="top_n"):
            rank_pool({"A": 0.1}, top_n=0)


class TestNaNIsTheRealDeterminismRisk:
    """非有限值一律**排除**，且结果必须与输入顺序无关。"""

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None])
    def test_non_finite_never_ranks(self, bad: float | None) -> None:
        rows = rank_pool({"A": 0.1, "BAD": bad, "C": 0.2}, top_n=3)
        assert [r.symbol for r in rows] == ["C", "A"]
        assert "BAD" not in {r.symbol for r in rows}

    def test_infinity_does_not_win(self) -> None:
        """``+inf`` 若被当成数值就会稳居第一 —— 那是最糟的静默错误。"""
        rows = rank_pool({"A": 0.1, "HUGE": float("inf")}, top_n=1)
        assert rows[0].symbol == "A"

    def test_result_is_independent_of_input_order(self) -> None:
        """**这是这一组里最重要的一条。**

        遍历全部输入顺序，断言名次完全一致。一个把 NaN 留在序列里的实现
        会在某些顺序下给出不同的名次，而那种 bug 在单次运行里看不出来。
        """
        base = {"A": 0.1, "B": float("nan"), "C": 0.2, "D": None, "E": 0.15}
        # 写死期望值而不是拿第一个排列自举 —— 后者只能证明「自洽」，
        # 一个按升序排的实现照样能通过。
        expected = [("C", 1), ("E", 2), ("A", 3)]
        for keys in itertools.permutations(base):
            rows = rank_pool({k: base[k] for k in keys}, top_n=2)
            assert [(r.symbol, r.rank) for r in rows] == expected

    def test_all_nan_pool_returns_empty(self) -> None:
        """有限值不足时只有这些有限的能上榜；一个都没有就是空榜，不是兜底名次。"""
        assert rank_pool({"A": float("nan"), "B": None}, top_n=3) == []

    def test_fewer_finite_than_top_n(self) -> None:
        rows = rank_pool({"A": 0.1, "B": float("nan"), "C": None}, top_n=3)
        assert len(rows) == 1
        assert rows[0].in_top_n is True


class TestTieBreak:
    def test_ties_break_by_symbol_ascending(self) -> None:
        rows = rank_pool({"ZZZ": 0.2, "AAA": 0.2, "MMM": 0.2}, top_n=3)
        assert [r.symbol for r in rows] == ["AAA", "MMM", "ZZZ"]

    def test_tie_break_is_order_independent(self) -> None:
        for keys in itertools.permutations(["ZZZ", "AAA", "MMM"]):
            rows = rank_pool(dict.fromkeys(keys, 0.2), top_n=3)
            assert [r.symbol for r in rows] == ["AAA", "MMM", "ZZZ"]


class TestSqueak:
    """「名次胶着」阈值**必须归一化**（§10.3）。

    把算术摆出来：平静期 16 只标的铺开 2pp，相邻间隔约 0.125pp，
    远小于一个固定的 0.5pp 阈值 → **一直触发**；
    高离散期铺开 25pp，间隔约 1.5pp → **永不触发**。
    固定阈值于是在最不需要提醒的时候最吵，在真正该提醒时沉默。

    （设计文档 §10.3 一度把这个方向写反了；是写这组测试时发现的，已更正。）
    """

    def test_tight_race_is_flagged(self) -> None:
        scores = {"A": 0.30, "B": 0.20, "C": 0.100, "D": 0.099, "E": 0.05}
        rows = rank_pool(scores, top_n=3)
        assert is_squeaky(rows, top_n=3, squeak_k=0.1) is True

    def test_clear_race_is_not_flagged(self) -> None:
        scores = {"A": 0.30, "B": 0.20, "C": 0.15, "D": 0.02, "E": 0.01}
        rows = rank_pool(scores, top_n=3)
        assert is_squeaky(rows, top_n=3, squeak_k=0.1) is False

    def test_same_absolute_gap_differs_by_dispersion(self) -> None:
        """**尺度无关性的正面表述。**

        同样的 0.1pp 绝对间隔：
        - 在一个全体密集的池里，它占总跨度的很大一块 → 是**真分开了**，不报警；
        - 在一个铺得很开的池里，它只是噪声 → 确实胶着，要报警。

        固定 pp 阈值无法区分这两种情形 —— 这就是归一化存在的理由。
        """
        gap = 0.001
        tight = {"A": 0.021, "B": 0.020, "C": 0.019, "D": 0.019 - gap, "E": 0.017}
        wide = {"A": 0.30, "B": 0.20, "C": 0.10, "D": 0.10 - gap, "E": -0.20}
        assert is_squeaky(rank_pool(tight, top_n=3), top_n=3, squeak_k=0.1) is False
        assert is_squeaky(rank_pool(wide, top_n=3), top_n=3, squeak_k=0.1) is True

    def test_verdict_is_invariant_under_rescaling(self) -> None:
        """把全池得分乘上一个常数，结论不应该变 —— 这就是「尺度无关」。

        固定 pp 阈值在这条测试下会直接翻车。
        """
        base = {"A": 0.30, "B": 0.20, "C": 0.100, "D": 0.099, "E": 0.05}
        for scale in (0.01, 1.0, 100.0):
            scaled = {k: v * scale for k, v in base.items()}
            assert is_squeaky(rank_pool(scaled, top_n=3), top_n=3, squeak_k=0.1) is True

    @pytest.mark.parametrize("pool_size", [3, 4, 5, 6, 7])
    def test_a_perfect_tie_is_always_squeaky(self, pool_size: int) -> None:
        """**全池同值是最胶着的情形** —— 第 N 名与第 N+1 名字面相等。

        初版在归一化之后才判，于是 ``np.std`` 对语义相同的池会给出
        0 或 3e-17（取决于 ``np.mean`` 能否恰好整除），让 3 个标的的池
        答 True、4 个标的的池答 False。纯浮点偶然决定的结论。
        """
        scores = {chr(ord("A") + i): 0.2 for i in range(pool_size)}
        rows = rank_pool(scores, top_n=2)
        assert is_squeaky(rows, top_n=2, squeak_k=0.1) is True

    def test_rejects_bad_top_n(self) -> None:
        rows = rank_pool({"A": 0.1, "B": 0.2}, top_n=1)
        with pytest.raises(ValueError, match="top_n"):
            is_squeaky(rows, top_n=0, squeak_k=0.1)

    def test_no_fourth_place_is_not_squeaky(self) -> None:
        rows = rank_pool({"A": 0.3, "B": 0.2, "C": 0.1}, top_n=3)
        assert is_squeaky(rows, top_n=3, squeak_k=0.1) is False

    def test_rejects_bad_k(self) -> None:
        rows = rank_pool({"A": 0.1, "B": 0.2}, top_n=1)
        with pytest.raises(ValueError, match="squeak_k"):
            is_squeaky(rows, top_n=1, squeak_k=0.0)


class TestEventDistanceTodayBoundary:
    """**财报当天**是这四个参数一年中唯一真正被人盯着看的那一天（§3.5(6)）。

    约定：``next`` 用严格大于，``last`` 用小于等于。
    于是当天 ``days_since = 0`` 且 ``days_to`` 指向下一季 ——
    不会出现「距财报 0 天」和「财报后 0 天」同时显示的自相矛盾。
    """

    OBS = date(2026, 10, 29)

    def test_on_the_event_day(self) -> None:
        events = [
            Event(date(2026, 7, 30), is_estimated=False),
            Event(self.OBS, is_estimated=False),
            Event(date(2027, 1, 28), is_estimated=True),
        ]
        got = event_distances(self.OBS, earnings=events, dividends=[])
        assert got.days_since_last_earnings == 0, "当天算「财报后 0 天」"
        assert got.days_to_next_earnings == 91, "而「距财报」指向下一季，不是 0"
        assert got.next_earnings_date == date(2027, 1, 28)
        assert got.next_earnings_is_estimated is True

    def test_day_before_and_after(self) -> None:
        events = [Event(self.OBS, is_estimated=False)]
        before = event_distances(date(2026, 10, 28), earnings=events, dividends=[])
        assert before.days_to_next_earnings == 1
        assert before.days_since_last_earnings is None
        after = event_distances(date(2026, 10, 30), earnings=events, dividends=[])
        assert after.days_to_next_earnings is None
        assert after.days_since_last_earnings == 1

    def test_no_future_event_writes_none_not_a_big_number(self) -> None:
        """没有已知未来事件时写 ``None``，**不写一个大数字**（§3.5(2)）。"""
        got = event_distances(
            self.OBS, earnings=[Event(date(2020, 1, 1), is_estimated=False)], dividends=[]
        )
        assert got.days_to_next_earnings is None
        assert got.next_earnings_date is None
        assert got.next_earnings_is_estimated is None

    def test_empty_event_lists(self) -> None:
        got = event_distances(self.OBS, earnings=[], dividends=[])
        assert got == EventDistances()

    def test_picks_nearest_on_each_side(self) -> None:
        events = [
            Event(date(2026, 1, 1), is_estimated=False),
            Event(date(2026, 7, 30), is_estimated=False),  # 最近的过去
            Event(date(2027, 1, 28), is_estimated=True),  # 最近的未来
            Event(date(2027, 4, 29), is_estimated=True),
        ]
        got = event_distances(self.OBS, earnings=events, dividends=[])
        assert got.days_since_last_earnings == (self.OBS - date(2026, 7, 30)).days
        assert got.next_earnings_date == date(2027, 1, 28)

    def test_duplicate_event_dates_are_resolved_deterministically(self) -> None:
        """同一天两行时，结果不能取决于列表顺序。

        ``symbol_events`` 故意没有自然主键（§3.5(1)），而不带 ORDER BY 的
        SELECT 没有顺序保证 —— 不显式打破并列的话，倒计时标签的「是预告还是
        已确认」会在两次运行之间无缘无故地翻。约定：**同一天优先取已确认的那行**。
        """
        day = date(2027, 1, 28)
        est = Event(day, is_estimated=True)
        confirmed = Event(day, is_estimated=False)
        for events in ([est, confirmed], [confirmed, est]):
            got = event_distances(self.OBS, earnings=events, dividends=[])
            assert got.next_earnings_is_estimated is False, "同一天应优先取已确认的"

    def test_dividends_are_independent_of_earnings(self) -> None:
        got = event_distances(
            self.OBS,
            earnings=[Event(date(2027, 1, 28), is_estimated=True)],
            dividends=[Event(date(2026, 11, 7), is_estimated=False)],
        )
        assert got.days_to_next_dividend == 9
        assert got.next_dividend_is_estimated is False
        assert got.days_to_next_earnings == 91

    def test_unit_is_calendar_days_not_sessions(self) -> None:
        """跨越一个含周末与假日的区间，断言就是自然日之差。

        全项目其余窗口都是交易日；这四个**故意**是日历日，
        防混用靠列名 ``days_*`` 与 ``sessions_*`` 写死。
        """
        obs = date(2026, 12, 24)
        got = event_distances(
            obs, earnings=[Event(date(2027, 1, 4), is_estimated=True)], dividends=[]
        )
        assert got.days_to_next_earnings == 11  # 自然日；交易日只有 6 个左右
