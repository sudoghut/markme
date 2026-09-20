"""``pipeline/throttle.py`` 的测试（§7.3.1）。

**这一组测的是一条礼仪约束，不是一个功能。** 它坏掉时我们这边什么都看不出来
—— 代码照常工作，只是对方看到的是一次小型压测。所以断言要落在
「一共发了几次请求」「一共等了多久」这种能代表对方感受的量上。
"""

from __future__ import annotations

import pytest

from pipeline.throttle import (
    USER_AGENT,
    BudgetExceeded,
    RequestBudget,
    RetryAfterTooLong,
    RetryExhausted,
    backoff_delays,
)


class _Clock:
    """可注入的时钟：sleep 只记账、不真睡。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _budget(clock: _Clock, **kw: object) -> RequestBudget:
    opts: dict[str, object] = {
        "max_requests": 10,
        "interval_seconds": 2.0,
        "retry_max_attempts": 3,
        "sleep": clock.sleep,
        "monotonic": clock.monotonic,
        "jitter": lambda: 0.5,  # 去掉随机性，让退避序列可断言
    }
    opts.update(kw)
    return RequestBudget(**opts)  # type: ignore[arg-type]


class TestSerialAndSpaced:
    def test_first_request_does_not_wait(self) -> None:
        clock = _Clock()
        b = _budget(clock)
        assert b.request(lambda: "ok") == "ok"
        assert clock.sleeps == []

    def test_subsequent_requests_are_spaced(self) -> None:
        clock = _Clock()
        b = _budget(clock)
        b.request(lambda: 1)
        b.request(lambda: 2)
        b.request(lambda: 3)
        assert clock.sleeps == [2.0, 2.0]

    def test_time_already_spent_counts_toward_the_interval(self) -> None:
        """记的是**上一次请求发出的时刻**，不是「每次都睡 2 秒」。

        上一个请求自己花了 3 秒，我们已经让出了 3 秒 —— 对方感知到的是
        请求密度，不是我们 sleep 了几次。再睡 2 秒是白等。
        """
        clock = _Clock()
        b = _budget(clock)

        def slow() -> int:
            clock.advance(3.0)
            return 1

        b.request(slow)
        b.request(lambda: 2)
        assert clock.sleeps == []  # 3 秒 > 2 秒间隔，不必补

    def test_partial_wait(self) -> None:
        clock = _Clock()
        b = _budget(clock)

        def a_bit_slow() -> int:
            clock.advance(0.5)
            return 1

        b.request(a_bit_slow)
        b.request(lambda: 2)
        assert clock.sleeps == [1.5]


class TestTheBudgetIsHard:
    def test_exhausting_the_budget_raises(self) -> None:
        clock = _Clock()
        b = _budget(clock, max_requests=3, interval_seconds=0)
        for _ in range(3):
            b.request(lambda: 1)
        with pytest.raises(BudgetExceeded, match="预算用尽"):
            b.request(lambda: 1)

    def test_retries_also_consume_budget(self) -> None:
        """**重试同样打在对方身上。**

        只给成功的请求记账，一个反复失败的循环会在预算内发出
        3 倍的请求量 —— 而预算存在的理由正是防止那件事。
        """
        clock = _Clock()
        b = _budget(clock, max_requests=10, interval_seconds=0, retry_max_attempts=3)

        def always_fails() -> int:
            raise RuntimeError("boom")

        with pytest.raises(RetryExhausted):
            b.request(always_fails)
        assert b.used == 3

    def test_budget_exhaustion_keeps_the_root_cause(self) -> None:
        """预算在重试之间耗尽时，``last`` 里装着**真正的原因**。

        丢掉它，排查的人只看到「预算用尽」，永远不知道预算是被什么烧掉的。
        """
        clock = _Clock()
        b = _budget(clock, max_requests=2, interval_seconds=0, retry_max_attempts=5)

        def fn() -> int:
            raise RuntimeError("429 from vendor")

        with pytest.raises(BudgetExceeded) as ei:
            b.request(fn)
        assert ei.value.__cause__ is not None
        assert "429 from vendor" in str(ei.value.__cause__)

    def test_remaining_reports_what_is_left(self) -> None:
        clock = _Clock()
        b = _budget(clock, max_requests=5, interval_seconds=0)
        b.request(lambda: 1)
        assert b.remaining == 4

    @pytest.mark.parametrize(
        ("kw", "match"),
        [
            ({"max_requests": 0}, "max_requests"),
            ({"interval_seconds": -1}, "interval_seconds"),
            ({"retry_max_attempts": 0}, "retry_max_attempts"),
        ],
    )
    def test_nonsense_config_is_rejected(self, kw: dict[str, object], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            _budget(_Clock(), **kw)


class TestBackoffIsBounded:
    def test_it_is_exponential_and_capped(self) -> None:
        delays = list(backoff_delays(8, jitter=lambda: 0.5))
        assert delays[0] == pytest.approx(2.0)
        assert delays[1] == pytest.approx(4.0)
        assert delays[2] == pytest.approx(8.0)
        assert delays[-1] == pytest.approx(60.0)

    def test_the_cap_holds_at_maximum_jitter(self) -> None:
        """**上限就该是上限。**

        抖动加在封顶**之后**时，乘数上界 1.5 会把尾部推到 90 秒，
        而 §7.3.1 说的是 60。上一版这条测试注入 ``jitter=0.5``
        （乘数恰好 1.0），于是无论 ``min()`` 放在哪边它都通过 ——
        一条永远不会失败的断言。
        """
        assert max(backoff_delays(10, jitter=lambda: 1.0)) <= 60.0
        assert max(backoff_delays(10, jitter=lambda: 0.0)) <= 60.0
        import random as _r

        assert max(backoff_delays(10, jitter=_r.random)) <= 60.0

    def test_jitter_spreads_retries(self) -> None:
        """没有抖动时，同时被限流的多个请求会在**同一时刻**一起重试 ——
        对方看到的是一个尖峰而不是退让。"""
        lo = list(backoff_delays(3, jitter=lambda: 0.0))
        hi = list(backoff_delays(3, jitter=lambda: 1.0))
        assert lo != hi
        assert all(a < b for a, b in zip(lo, hi, strict=True))

    def test_retry_stops_at_the_limit(self) -> None:
        clock = _Clock()
        calls = 0

        def flaky() -> int:
            nonlocal calls
            calls += 1
            raise RuntimeError("429")

        b = _budget(clock, interval_seconds=0, retry_max_attempts=3)
        with pytest.raises(RetryExhausted, match="等待比重试便宜"):
            b.request(flaky)
        assert calls == 3, "至多 3 次 —— 死重试打的是同一个已经在限流我们的服务"

    def test_backoff_and_interval_do_not_double_sleep(self) -> None:
        """退避那一觉已经把间隔睡掉了，不该再补一次。

        上一版所有重试测试都用 ``interval_seconds=0``，于是
        「退避 + 间隔」这个双重 sleep 的错误实现可以完整通过 56 条测试。
        """
        clock = _Clock()
        b = _budget(clock, interval_seconds=2.0, retry_max_attempts=3)

        def always_fails() -> int:
            raise RuntimeError("boom")

        with pytest.raises(RetryExhausted):
            b.request(always_fails)
        assert clock.sleeps == [pytest.approx(2.0), pytest.approx(4.0)]

    def test_a_late_success_is_returned(self) -> None:
        clock = _Clock()
        calls = 0

        def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("temporary")
            return "ok"

        b = _budget(clock, interval_seconds=0)
        assert b.request(flaky) == "ok"
        assert calls == 3


class TestRetryAfterIsHonoured:
    class _Resp:
        def __init__(self, value: object) -> None:
            self.headers = {"Retry-After": value}

    def _raise_with(self, value: object) -> Exception:
        exc = RuntimeError("429")
        exc.response = self._Resp(value)  # type: ignore[attr-defined]
        return exc

    def test_seconds_form_is_used(self) -> None:
        """对方明确说了该等多久，忽略它就是明知故犯。"""
        clock = _Clock()
        b = _budget(clock, interval_seconds=0, retry_max_attempts=2)
        exc = self._raise_with("17")

        def fn() -> int:
            raise exc

        with pytest.raises(RetryExhausted):
            b.request(fn)
        assert clock.sleeps == [17.0]

    def test_http_date_form_falls_back_to_backoff(self) -> None:
        """认不出的格式退回退避值，**不猜** ——
        猜错的方向是「等得太短」，那正是要避免的。"""
        clock = _Clock()
        b = _budget(clock, interval_seconds=0, retry_max_attempts=2)

        def fn() -> int:
            raise self._raise_with("Wed, 21 Oct 2026 07:28:00 GMT")

        with pytest.raises(RetryExhausted):
            b.request(fn)
        assert clock.sleeps == [pytest.approx(2.0)]

    def test_a_nonsense_retry_after_does_not_become_zero_wait(self) -> None:
        """``Retry-After: nan`` 能被 ``float()`` 解析，而 ``nan < 0`` 与
        ``nan > 0`` 都是 False —— 它会一路穿到 sleep 并被静默跳过，
        **完全不等**。那是「等得太短」的极端情形。"""
        clock = _Clock()
        b = _budget(clock, interval_seconds=0, retry_max_attempts=2)

        def fn() -> int:
            raise self._raise_with("nan")

        with pytest.raises(RetryExhausted):
            b.request(fn)
        assert clock.sleeps == [pytest.approx(2.0)]

    def test_a_long_retry_after_aborts_the_run_instead_of_discounting_it(self) -> None:
        """对方说等一小时：**既不打折也不干等**。

        初版把它截断到 60 秒 —— 也就是对方说 300 秒、我们 60 秒就回去敲门。
        那正是 §7.3.1 这条规则要防的明知故犯，而且和这段代码自己的注释
        「猜错的方向是等得太短」直接矛盾。
        """
        clock = _Clock()
        b = _budget(clock, interval_seconds=0, retry_max_attempts=3)

        def fn() -> int:
            raise self._raise_with("3600")

        with pytest.raises(RetryAfterTooLong, match="不打折"):
            b.request(fn)
        assert clock.sleeps == [], "放弃这一跑，不是在这儿干等一小时"

    def test_a_long_but_tolerable_retry_after_is_honoured_in_full(self) -> None:
        clock = _Clock()
        b = _budget(clock, interval_seconds=0, retry_max_attempts=2)

        def fn() -> int:
            raise self._raise_with("300")

        with pytest.raises(RetryExhausted):
            b.request(fn)
        assert clock.sleeps == [300.0], "对方说 300 就是 300"

    def test_an_exception_without_a_response_is_fine(self) -> None:
        clock = _Clock()
        b = _budget(clock, interval_seconds=0, retry_max_attempts=2)

        def fn() -> int:
            raise RuntimeError("no response attribute")

        with pytest.raises(RetryExhausted):
            b.request(fn)
        assert clock.sleeps == [pytest.approx(2.0)]


def test_user_agent_is_honest() -> None:
    """标明项目名与仓库地址，让对方能找到我们，而不是伪装成浏览器。"""
    assert "markme" in USER_AGENT
    assert "github.com/sudoghut/markme" in USER_AGENT
    assert "Mozilla" not in USER_AGENT
