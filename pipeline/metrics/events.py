"""事件距离：财报与分红的前后距离（§3.5）。

与其余指标**不是一类东西**：不是收盘价的函数，而是日历事件的函数。
由此三条：

- 单位是**日历日**（人说「还有 3 天财报」指的是自然日），与全项目其余窗口的
  交易日单位由列名 ``days_*`` / ``sessions_*`` 隔开。
- **只算最新一个 session 的那一行**，历史行一律 ``None``。
  事件改期不会让「当时的预告是 28 天后」变成假的；每天重写会让同一条历史行
  今天显示 28、明天显示 35，永不稳定。
- **今天的边界**：``next`` 用严格大于，``last`` 用小于等于。
  于是财报当天 ``days_since = 0`` 且 ``days_to`` 指向下一季，
  不会出现「距财报 0 天」和「财报后 0 天」同时显示的自相矛盾。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pipeline.metrics.registry import register

__all__ = ["Event", "EventDistances", "event_distances"]


@dataclass(frozen=True)
class Event:
    """``symbol_events`` 里的一行（只取本模块用得到的字段）。"""

    event_date: date
    is_estimated: bool


@dataclass(frozen=True)
class EventDistances:
    """一个标的在某个观测日的八个事件字段。``None`` 写库时落 NULL。"""

    days_to_next_earnings: int | None = None
    days_since_last_earnings: int | None = None
    days_to_next_dividend: int | None = None
    days_since_last_dividend: int | None = None
    next_earnings_date: date | None = None
    next_dividend_date: date | None = None
    next_earnings_is_estimated: bool | None = None
    next_dividend_is_estimated: bool | None = None


@register("event_distances", kind="table")
def event_distances(
    observation_date: date,
    *,
    earnings: list[Event],
    dividends: list[Event],
) -> EventDistances:
    """算出八个事件字段。事件列表可以为空 —— 那时对应字段全是 ``None``。"""
    nxt_e, last_e = _bracket(observation_date, earnings)
    nxt_d, last_d = _bracket(observation_date, dividends)
    return EventDistances(
        days_to_next_earnings=_delta(nxt_e, observation_date),
        days_since_last_earnings=_delta(observation_date, last_e),
        days_to_next_dividend=_delta(nxt_d, observation_date),
        days_since_last_dividend=_delta(observation_date, last_d),
        next_earnings_date=None if nxt_e is None else nxt_e.event_date,
        next_dividend_date=None if nxt_d is None else nxt_d.event_date,
        next_earnings_is_estimated=None if nxt_e is None else nxt_e.is_estimated,
        next_dividend_is_estimated=None if nxt_d is None else nxt_d.is_estimated,
    )


def _bracket(obs: date, events: list[Event]) -> tuple[Event | None, Event | None]:
    """把事件分到观测日的两侧。

    边界：``next`` 是 ``event_date > obs`` 里最早的一个（**严格大于**），
    ``last`` 是 ``event_date <= obs`` 里最晚的一个（**含当天**）。
    """
    future = [e for e in events if e.event_date > obs]
    past = [e for e in events if e.event_date <= obs]
    # 同一天可能有两行（§3.5(1)：symbol_events 故意没有自然主键，
    # 而不带 ORDER BY 的 SELECT 没有顺序保证）。不显式打破并列的话，
    # `min`/`max` 返回的是**列表里的第一个**，于是倒计时标签的「是预告还是已确认」
    # 会在两次运行之间无缘无故地翻。**受影响的只有这一个标记** —— 并列的前提就是
    # 日期相等，所以天数与日期在两种顺序下恒等，排序键也从不读 is_estimated。
    # 排序键里把 is_estimated 写在后面：同一天时**优先取已确认的那行**。
    nxt = min(future, key=lambda e: (e.event_date, e.is_estimated)) if future else None
    last = max(past, key=lambda e: (e.event_date, not e.is_estimated)) if past else None
    return nxt, last


def _delta(later: Event | date | None, earlier: Event | date | None) -> int | None:
    """两个日期相差多少**日历日**；任一侧缺失则 ``None``（不写一个大数字）。"""
    a = later.event_date if isinstance(later, Event) else later
    b = earlier.event_date if isinstance(earlier, Event) else earlier
    if a is None or b is None:
        return None
    return (a - b).days
