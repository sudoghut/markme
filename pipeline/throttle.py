"""抓取礼仪：**充分尊重数据源的限流，我们不追求抓取速度**（§7.3.1）。

这是一条**设计约束，不是实现细节**。数据源（尤其免费的 yfinance）没有义务
承受我们的量。整个 run 花 5–10 分钟完全可以接受：收盘后一小时才跑的 EOD 看板，
**没有任何理由跑得快**，而 Actions 的 job 上限是 6 小时。

这个模块把那条约束变成代码里**绕不过去**的东西：每一次供应商请求都必须
经由 :meth:`Budget.request`，于是「间隔」「上限」「退避」不是调用方的自觉。

四条规则各自防一件具体的事：

===================  ==========================================================
规则                  不做会怎样
===================  ==========================================================
串行 + 固定间隔        并发 17 个 ticker 在对方看来就是一次小型压测
全局请求预算           某个循环 bug 会变成一场无意的压测，而且**没人会发现**，
                      因为它不报错，只是很慢
退避有上限（3 次）     死重试打的是同一个已经在限流我们的服务；
                      §7.1 的条件重试 40 分钟后会再来 —— **等待比重试便宜**
尊重 ``Retry-After``   对方明确告诉了我们该等多久，忽略它就是明知故犯
===================  ==========================================================
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TypeVar

__all__ = [
    "USER_AGENT",
    "BudgetExceeded",
    "RequestBudget",
    "RetryAfterTooLong",
    "RetryExhausted",
    "backoff_delays",
]

T = TypeVar("T")

#: 诚实的 User-Agent：标明项目名与仓库地址，让对方能找到我们，
#: 而不是伪装成浏览器（§7.3.1）。
USER_AGENT = "markme/0.1 (+https://github.com/sudoghut/markme) market-metrics dashboard"

#: **自己算的**退避的上限。2s → 4s → 8s …，但不超过这个数（§7.3.1）。
#: 注意这条只管「没有 Retry-After 头」那条路径 —— 见 :func:`_retry_after`。
_MAX_BACKOFF_SECONDS = 60.0

#: 对方说「等这么久」时，我们愿意在**一次 run 之内**等待的上限。
#: 超过它就不等了：§7.1 的条件重试 40 分钟后会再来，而 Actions 的 job
#: 时间不该花在干等上。**这不是把对方的要求打折**（那是 §7.3.1 禁止的），
#: 而是放弃这一跑 —— 两者的区别是：前者会在更短的时间后再次打扰对方，
#: 后者不会。
_MAX_HONOURED_RETRY_AFTER = 600.0


class BudgetExceeded(RuntimeError):
    """单次 run 的请求预算用尽。

    调用方应当记 ``partial`` 并退出，**而不是**继续请求或者悄悄少抓几个标的
    —— 后者会产出一个「看起来完整」的部分结果。
    """


class RetryExhausted(RuntimeError):
    """退避到上限仍然失败。等下一跑（§7.1 的条件重试）比死重试便宜。"""


class RetryAfterTooLong(RuntimeError):
    """对方给出的 ``Retry-After`` 超过一次 run 愿意等待的上限。

    **把它当成「放弃这一跑」，不要当成「那就少等一会儿」** ——
    后者是 §7.3.1 明确禁止的明知故犯。
    """


def backoff_delays(
    attempts: int,
    *,
    base: float = 2.0,
    cap: float = _MAX_BACKOFF_SECONDS,
    jitter: Callable[[], float] | None = None,
) -> Iterator[float]:
    """指数退避 + 抖动：2s → 4s → 8s …，上限 ``cap``。

    抖动是必要的而不是讲究：没有抖动时，同一时刻被限流的多个请求会在
    **同一时刻**一起重试，对方看到的是一个尖峰而不是退让。
    """
    rand = jitter or random.random
    for i in range(attempts):
        # 抖动加在**封顶之前**，否则 1.5 × 60 = 90 秒，而 §7.3.1 说的是上限 60。
        # 方向上偏长是安全的，但「上限」就该是上限。
        yield min(base * (2**i) * (0.5 + rand()), cap)


@dataclass
class RequestBudget:
    """一次 run 的请求预算 + 节流器。

    **一次 run 只应当有一个实例，自上而下传下去。**
    §7.3.1 说的是「**单次 run** 的总请求数设硬上限」——
    价格层和事件层各建一个，上限就悄悄翻倍了，而那正是这条规则要防的
    「某个循环 bug 变成一场无意的压测」。这一点没法在类型上强制，
    所以写在这里：``run_daily`` / ``backfill`` 各建一个，其余都接受参数。

    同理，它**不是线程安全的** —— 但 §7.3.1 的第二条规则就是
    「串行，不并发」，所以这不是缺陷，是同一条约束的另一面。

    ``sleep`` / ``monotonic`` 可注入，于是测试不必真的睡 2 秒。
    """

    max_requests: int
    interval_seconds: float
    retry_max_attempts: int
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] | None = None

    used: int = field(default=0, init=False)
    slept_seconds: float = field(default=0.0, init=False)
    _last_request_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.max_requests <= 0:
            raise ValueError("max_requests 必须为正")
        if self.interval_seconds < 0:
            raise ValueError("interval_seconds 不能为负")
        if self.retry_max_attempts <= 0:
            raise ValueError("retry_max_attempts 必须为正")

    @property
    def remaining(self) -> int:
        return self.max_requests - self.used

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleep(seconds)
            self.slept_seconds += seconds

    def _wait_for_interval(self) -> None:
        """距上一次请求不足 ``interval_seconds`` 就补足。

        记的是**上一次请求发出的时刻**而不是「每次都睡 2 秒」：
        如果上一个请求本身花了 3 秒，我们已经让出了 3 秒，没必要再等。
        对方感知到的是请求密度，不是我们 sleep 了几次。
        """
        if self._last_request_at is None:
            return
        elapsed = self.monotonic() - self._last_request_at
        self._sleep(self.interval_seconds - elapsed)

    def request(self, fn: Callable[[], T], *, what: str = "请求") -> T:
        """发一次供应商请求：先占预算、再等间隔、失败则有上限地退避。

        ``fn`` 应当是一次**完整的**供应商调用。每次尝试（含重试）都各占
        一次预算 —— 预算防的是「总请求数」，而重试同样打在对方身上。
        """
        delays = backoff_delays(self.retry_max_attempts - 1, jitter=self.jitter)
        last: Exception | None = None

        for attempt in range(self.retry_max_attempts):
            if self.used >= self.max_requests:
                # `from last`：预算在重试之间耗尽时，`last` 里装着**真正的原因**
                # （429？DNS？）。丢掉它，排查的人只看到「预算用尽」，
                # 永远不知道预算是被什么烧掉的。
                raise BudgetExceeded(
                    f"请求预算用尽（{self.max_requests}），停在「{what}」。"
                    "记 partial 退出，不要继续请求 —— "
                    "一个「看起来完整」的部分结果比一次失败危险。"
                ) from last
            self._wait_for_interval()
            self.used += 1
            self._last_request_at = self.monotonic()
            try:
                return fn()
            except (BudgetExceeded, RetryAfterTooLong):
                raise
            except Exception as exc:
                last = exc
                if attempt + 1 < self.retry_max_attempts:
                    self._sleep(_retry_after(exc, next(delays)))

        raise RetryExhausted(
            f"「{what}」尝试 {self.retry_max_attempts} 次（即重试 "
            f"{self.retry_max_attempts - 1} 次）仍失败：{last}。"
            "不再重试 —— §7.1 的条件重试会在下一跑再来，等待比重试便宜。"
        ) from last


def _retry_after(exc: Exception, fallback: float) -> float:
    """收到 429 时优先读 ``Retry-After``；读不到才用退避值。

    对方明确说了该等多久，忽略它就是明知故犯。头可能是秒数，
    也可能是 HTTP-date —— 只认秒数那种，认不出就退回退避值，
    **不猜**（猜错的方向是「等得太短」，那正是我们要避免的）。

    **这个值不被截断。** 初版对它也套了 60 秒上限，于是对方说「等 300 秒」
    我们 60 秒就回去敲门 —— 那正是这条规则要防的那件事，而且和上面那句
    「猜错的方向是等得太短」直接矛盾。§7.3.1 里的「上限 60s」
    说的是**我们自己算的**指数退避，不是对方给的指令。

    超过 :data:`_MAX_HONOURED_RETRY_AFTER` 时抛 :class:`RetryAfterTooLong`：
    我们既不打折也不干等，而是放弃这一跑交给 §7.1 的条件重试。
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return fallback
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return fallback
    if raw is None:
        return fallback
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return fallback
    # `float("nan")` 解析得出来，而 `nan < 0` 是 False、`nan > 0` 也是 False ——
    # 于是它会一路穿到 `_sleep` 并被静默跳过，**完全不等**。
    # 那是「等得太短」的极端情形，正是这段代码要防的。
    if not math.isfinite(seconds) or seconds < 0:
        return fallback
    if seconds > _MAX_HONOURED_RETRY_AFTER:
        raise RetryAfterTooLong(
            f"对方要求等待 {seconds:.0f} 秒，超过本次 run 愿意等待的上限"
            f"（{_MAX_HONOURED_RETRY_AFTER:.0f} 秒）。**不打折、也不干等** —— "
            "放弃这一跑，记 partial，让 §7.1 的条件重试稍后再来。"
        )
    return seconds
