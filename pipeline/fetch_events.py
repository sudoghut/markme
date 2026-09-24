"""财报 / 分红 → ``symbol_events``：不可批量、三个端点、按周刷新（§3.5）。

这四个参数与其余指标**不是一类东西**：它们不是收盘价的函数，而是日历事件的函数。
§3.5 的六条规则里，有四条落在这个模块：

1. **存事件日期，天数由它派生；幂等性来自整窗删+插，不是主键冲突**（§3.5(1)）。
   供应商不提供稳定的事件 id，而 ``(symbol, event_type, event_date)`` 也不唯一
   —— 常规分红与特别分红可以同一个除息日。
   **覆盖窗口必须由端点本身定义，不能由「本次返回了什么」定义**：
   若供应商本次返回空集，「本次最早日期」是未定义的，于是删除语句无从下手，
   那条**已作废的未来事件会永远留在库里**，倒计时继续走向一个不存在的日子。
2. **「下一次」是预告，不是事实**（§3.5(2)）。``is_estimated`` 要有生命周期：
   ``Ticker.dividends`` 取回的历史除息日是**已发生的事实**，必须写 ``False``。
   把 settled 的事实标成「估计」是另一种不诚实，还会让标记到处都是从而失去意义。
3. **三个端点，不是两个**（§3.5(4)）。初稿只写了两个，于是
   ``days_since_last_earnings`` 根本没有数据源。
4. **抓取失败不得记 partial**（§3.5(4)）。``partial`` 是 exit 1 且不写 ``ok`` 行，
   于是 §7.1 的条件重试「本 session 已有 ok 就跳过」**不会跳过** ——
   夏令时那四跑会全部执行完整管道，而 18:40 那跑若撞上限流降级到 Stooq，
   **好数据会被更粗的源静默覆盖**。
   → 一个可选的装饰性指标，就这样获得了静默污染核心价格序列的能力。
   记 ``ok_events_stale``，exit 0，不告警。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import pandas as pd

from pipeline.metrics.events import Event as MetricEvent
from pipeline.metrics.events import EventDistances, event_distances
from pipeline.throttle import BudgetExceeded, RequestBudget, RetryAfterTooLong

__all__ = [
    "EARNINGS_COVERAGE_DAYS",
    "EventFetchOutcome",
    "SymbolEvent",
    "coverage_start",
    "distances_for",
    "fetch_symbol_events",
    "should_refresh",
    "to_metric_events",
]

EventType = Literal["earnings", "dividend"]

#: ``Ticker.earnings_dates`` 约覆盖前后各 4 次财报。覆盖窗口由**端点契约**给出，
#: 与本次返回了几行无关（§3.5(1)）。
EARNINGS_COVERAGE_DAYS = 400


@dataclass(frozen=True, slots=True)
class SymbolEvent:
    symbol: str
    event_type: EventType
    event_date: date
    is_estimated: bool
    source: str = "yfinance"
    amount: float | None = None


@dataclass
class EventFetchOutcome:
    """一个标的的事件抓取结果。

    ``ok`` 为 False 时**调用方不得执行删+插**（§3.5(1)）：
    半次失败的抓取不该抹掉好数据。
    """

    symbol: str
    ok: bool
    events: tuple[SymbolEvent, ...] = ()
    coverage: dict[EventType, date] = field(default_factory=dict)
    error: str | None = None


def coverage_start(event_type: EventType, today: date, sessions_start: date) -> date:
    """删除窗口的左端点。**由端点契约给出，不由本次返回了什么给出。**

    - ``earnings``：``today - 400 天``（``earnings_dates`` 约覆盖前后各 4 次）
    - ``dividend``：``sessions_start_date``（``Ticker.dividends`` 给全历史）

    这是 §3.5(1) 最容易漏的一条：拿「本次最早日期」当左端点时，
    一次返回空集的抓取会让删除无从下手，于是已作废的未来事件永远留在库里。
    """
    if event_type == "earnings":
        return today - timedelta(days=EARNINGS_COVERAGE_DAYS)
    return sessions_start


def should_refresh(
    *,
    today: date,
    is_trading_day: bool,
    last_fetch_at: date | None,
    nearest_event: date | None,
    refresh_weekday: int,
    within_days: int,
) -> bool:
    """这个标的今天要不要抓事件（§3.5(4)）。

    ===========================  ==========================================
    情形                          频率
    ===========================  ==========================================
    常态                          每周一次（固定 ``events_refresh_weekday``；
                                  非交易日则顺延到下一个交易日）
    最近未来事件 ``<= 10 天``     该标的**每天**刷新
    ===========================  ==========================================

    触发条件用调用方查 ``symbol_events`` 得到的 ``nearest_event``，
    **不读 ``metrics_daily``** —— 后者在管道里是**在抓取之后**才算的，
    首次运行时还是空的，那样这个触发器永远不会生效（§3.5(4) 的脚注）。
    """
    if nearest_event is not None and 0 <= (nearest_event - today).days <= within_days:
        return True
    if not is_trading_day:
        return False
    if last_fetch_at is not None and (today - last_fetch_at).days < 7:
        # 本周已经抓过了。用 private.fetch_state 而不是 symbol_events.updated_at：
        # 后者走整窗删+插，每次重写后都是「最近一次写入」而不是「最近一次尝试抓取」
        # —— 抓取成功但内容没变时，你仍然需要知道「我今天查过了」。
        return False
    return today.isoweekday() >= refresh_weekday


def fetch_symbol_events(
    symbol: str,
    *,
    today: date,
    sessions_start: date,
    budget: RequestBudget,
    calendar_fn: Callable[[str], Any],
    earnings_dates_fn: Callable[[str], Any],
    dividends_fn: Callable[[str], Any],
) -> EventFetchOutcome:
    """三个端点各一次请求（§3.5(4)）—— 每标的 3 次，17 个标的约 51 次。

    三个端点**都成功**才算 ``ok``。任何一个失败都返回 ``ok=False``，
    于是调用方跳过删+插：**半次失败的抓取不会抹掉好数据**（§3.5(1)）。
    """
    events: list[SymbolEvent] = []
    try:
        cal = budget.request(lambda: calendar_fn(symbol), what=f"{symbol} calendar")
        events += _from_calendar(symbol, cal, today)

        hist = budget.request(lambda: earnings_dates_fn(symbol), what=f"{symbol} earnings_dates")
        events += _from_earnings_dates(symbol, hist, today)

        divs = budget.request(lambda: dividends_fn(symbol), what=f"{symbol} dividends")
        events += _from_dividends(symbol, divs)
    except (BudgetExceeded, RetryAfterTooLong):
        # **这两个不是「事件抓取失败」，是全局护栏。**
        #
        # §3.5(4) 豁免的是普通的端点失败（那记 ok_events_stale、exit 0、不告警），
        # 而 §7.3.1 的请求预算是防「某个循环 bug 变成一场无意的压测」的硬上限。
        # 把它吞进 ok=False，一次烧光 200 次预算的死循环就会产出一次**绿色运行**
        # —— 正好是那条规则存在的全部理由。向上冒泡，由调用方记 partial。
        raise
    except Exception as exc:
        return EventFetchOutcome(symbol=symbol, ok=False, error=f"{type(exc).__name__}: {exc}")

    return EventFetchOutcome(
        symbol=symbol,
        ok=True,
        events=tuple(_dedupe(events, today)),
        coverage={
            "earnings": coverage_start("earnings", today, sessions_start),
            "dividend": coverage_start("dividend", today, sessions_start),
        },
    )


def _dedupe(events: Sequence[SymbolEvent], today: date) -> list[SymbolEvent]:
    """合并同一件事的多份说法，并保证**每类事件最多一条未来行**（§3.5(1)）。

    两步：

    1. 同一 ``(type, date)`` 出现两次 → 保留**已确认**的那条。
       ``calendar`` 给的下一次财报常是估计值，而 ``earnings_dates``
       可能覆盖到同一天并标为已发生；真正的语义是「同一件事，以确认的为准」。
    2. 同一类事件有多条**未来**行 → 只保留最早的那条。

    第 2 步是实测逼出来的：AAPL 的 ``calendar`` 说下一次财报是 2026-10-30，
    而 ``earnings_dates`` 说 2026-10-29 —— **同一件事，两个端点差一天**。
    两条都留下，17 个标的里有 14 个会触发 §3.5(1) 的孤儿行不变式。

    而那条不变式要防的正是这个后果：``days_to_next_earnings`` 取
    ``min(未来 event_date)``，于是倒计时会指向两个日期里更早的那个；
    到期后翻成「财报后 1 天」，**播报一场从未发生的财报**，
    再过一天数字又自己对了 —— 事后更难发现。

    只留最早的那条，与 ``min(未来 event_date)`` 的取值完全一致，
    所以下游没有任何信息损失。
    """
    best: dict[tuple[str, date], SymbolEvent] = {}
    for e in events:
        key = (e.event_type, e.event_date)
        prev = best.get(key)
        if prev is None or (prev.is_estimated and not e.is_estimated):
            best[key] = e

    out: list[SymbolEvent] = []
    for etype in ("earnings", "dividend"):
        same = [e for e in best.values() if e.event_type == etype]
        out += [e for e in same if e.event_date <= today]
        future = sorted((e for e in same if e.event_date > today), key=lambda e: e.event_date)
        if future:
            out.append(future[0])
    return sorted(out, key=lambda e: (e.event_type, e.event_date))


def _from_calendar(symbol: str, cal: Any, today: date) -> list[SymbolEvent]:
    """``Ticker.calendar``：下一次财报日（常为估计）、下一次除息日。

    **实测确认项（§11 M4 验收）**：``calendar['Ex-Dividend Date']`` 究竟是
    「下一次」还是「最近一次」。这里按「它可能是任意一次」处理 ——
    只有严格晚于今天的才当作未来事件，其余当历史事实。
    这样两种口径下都不会产出一个指向过去的「下一次」。
    """
    out: list[SymbolEvent] = []
    for key, etype in (("Earnings Date", "earnings"), ("Ex-Dividend Date", "dividend")):
        for d in _as_dates(_get(cal, key)):
            out.append(
                SymbolEvent(
                    symbol=symbol,
                    event_type=etype,  # type: ignore[arg-type]
                    event_date=d,
                    # 未来的是预告；已经发生的是事实（§3.5(2)）
                    is_estimated=d > today,
                )
            )
    return out


def _from_earnings_dates(symbol: str, hist: Any, today: date) -> list[SymbolEvent]:
    """``Ticker.earnings_dates``：最近若干次**已发生**的财报日。

    初稿只写了两个端点，于是 ``days_since_last_earnings`` 根本没有数据源。
    """
    return [
        SymbolEvent(
            symbol=symbol,
            event_type="earnings",
            event_date=d,
            is_estimated=d > today,
        )
        for d in _as_dates(hist)
    ]


def _from_dividends(symbol: str, divs: Any) -> list[SymbolEvent]:
    """``Ticker.dividends``：历史除息日与金额。

    **全部是已发生的事实，一律 ``is_estimated=False``**（§3.5(2)）。
    口径：它返回的是**拆股调整后**的每股金额，与公告原值在有拆股的窗口里
    不一致（NVDA/AVGO 2024 均 10:1）。本项目只存前者。
    """
    out: list[SymbolEvent] = []
    if divs is None:
        return out
    if isinstance(divs, pd.Series):
        for idx, amount in divs.items():
            d = _to_date(idx)
            if d is not None:
                out.append(
                    SymbolEvent(
                        symbol=symbol,
                        event_type="dividend",
                        event_date=d,
                        is_estimated=False,
                        amount=float(amount) if pd.notna(amount) else None,
                    )
                )
        return out
    for d in _as_dates(divs):
        out.append(
            SymbolEvent(symbol=symbol, event_type="dividend", event_date=d, is_estimated=False)
        )
    return out


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]
    except (KeyError, TypeError, IndexError):
        return None


def _as_dates(value: Any) -> list[date]:
    """把供应商给的各种形状（标量 / list / Series / DatetimeIndex）压成日期列表。"""
    if value is None:
        return []
    if isinstance(value, pd.Series):
        value = value.tolist()
    elif isinstance(value, pd.DatetimeIndex):
        value = list(value)
    elif isinstance(value, pd.DataFrame):
        value = list(value.index)
    elif not isinstance(value, list | tuple | set):
        value = [value]
    out = [_to_date(v) for v in value]
    return [d for d in out if d is not None]


def _to_date(v: Any) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        ts: Any = pd.Timestamp(v)
    except (TypeError, ValueError):
        return None
    # **实测：`NaT.date()` 不抛异常，它返回 `NaT`。**
    # 于是一个缺失日期会伪装成一个「日期」一路走到写库，
    # 在 `date` 列上才炸 —— 而那时已经看不出它从哪来。
    # 用 `is pd.NaT` 而不是 `pd.isna(ts)`：pandas-stubs 把后者在 Timestamp 上
    # 推成恒假，mypy 会判这个分支不可达，而它在运行时是可达的。
    if ts is pd.NaT:
        return None
    return ts.date()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# 四个派生参数（§3.5(6)）
# ---------------------------------------------------------------------------
#
# **不在这里实现。** M2 的 ``pipeline/metrics/events.py`` 已经有
# ``event_distances`` 与 ``EventDistances``，语义完全相同（含「今天」那两条
# 边界：``next`` 严格大于、``last`` 含当天）。再写一份就是 §6.1.1 那句
# 「两个要对齐的地方，就是将来会不对齐的地方」—— 而这四个参数正是
# §3.5 花了整节论证「错了很难发现」的东西。
#
# 这里只提供把本模块的 :class:`SymbolEvent` 转成 M2 的 ``Event`` 的适配器。


def to_metric_events(events: Sequence[SymbolEvent], event_type: EventType) -> list[MetricEvent]:
    """``SymbolEvent`` → M2 的 ``Event``，按类型筛选。"""
    return [
        MetricEvent(event_date=e.event_date, is_estimated=e.is_estimated)
        for e in events
        if e.event_type == event_type
    ]


def distances_for(events: Sequence[SymbolEvent], observed_on: date) -> EventDistances:
    """四个距离 + 两个日期 + 两个估计标记，**只给最新一个 session 的行**（§3.5(3)）。

    §3.0 规则 2 每天重写全部 400 行，那条规则对复权因子是对的
    （追溯改写的因子**追溯为真**），但对事件**恰好相反**：
    AAPL 把财报从 10-29 挪到 11-05，并不会让「10-01 那天公布的预告是 28 天后」
    这件事变成假的。每天重写会让同一条历史行今天显示 28、明天显示 35。
    """
    out: EventDistances = event_distances(
        observed_on,
        earnings=to_metric_events(events, "earnings"),
        dividends=to_metric_events(events, "dividend"),
    )
    return out
