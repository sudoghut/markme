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
   把 settled 的事实标成「估计」是另一种不诚实，还会让虚线到处都是从而失去意义。
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

from pipeline.throttle import RequestBudget

__all__ = [
    "EARNINGS_COVERAGE_DAYS",
    "EventFetchOutcome",
    "SymbolEvent",
    "coverage_start",
    "days_between",
    "event_distances",
    "fetch_symbol_events",
    "should_refresh",
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
    except Exception as exc:
        return EventFetchOutcome(symbol=symbol, ok=False, error=f"{type(exc).__name__}: {exc}")

    return EventFetchOutcome(
        symbol=symbol,
        ok=True,
        events=tuple(_dedupe(events)),
        coverage={
            "earnings": coverage_start("earnings", today, sessions_start),
            "dividend": coverage_start("dividend", today, sessions_start),
        },
    )


def _dedupe(events: Sequence[SymbolEvent]) -> list[SymbolEvent]:
    """同一 ``(type, date)`` 出现两次时保留**已确认**的那条。

    ``calendar`` 给的下一次财报常是估计值，而 ``earnings_dates`` 可能同时
    覆盖到同一天并标为已发生。两条都留下会让 §3.5(1) 的孤儿行不变式报警，
    而真正的语义是「同一件事，以确认的那份为准」。
    """
    best: dict[tuple[str, date], SymbolEvent] = {}
    for e in events:
        key = (e.event_type, e.event_date)
        prev = best.get(key)
        if prev is None or (prev.is_estimated and not e.is_estimated):
            best[key] = e
    return sorted(best.values(), key=lambda e: (e.event_type, e.event_date))


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
def days_between(a: date, b: date) -> int:
    """**日历日**，不是交易日（§3.5(6)）。

    人说「还有 3 天财报」指的是自然日，而全项目其余窗口都用交易日。
    防混用靠列名写死：``days_*`` vs ``sessions_*``。
    """
    return (b - a).days


def event_distances(
    events: Sequence[SymbolEvent], observed_on: date
) -> dict[str, int | date | bool | None]:
    """四个距离 + 两个日期 + 两个估计标记，**只给最新一个 session 的行**（§3.5(3)）。

    今天的边界写死（这是这四个参数一年中唯一真正被人盯着看的那一天）：

    - ``next``：``event_date > 观测日``（**严格大于**）
    - ``last``：``event_date <= 观测日``（**含当天**）

    于是财报当天 ``days_since_last_earnings = 0``，而 ``days_to_next_earnings``
    指向下一季 —— 不会出现「距财报 0 天」和「财报后 0 天」同时显示的自相矛盾。

    没有任何已知未来事件时写 ``None``，**不写一个大数字**（§3.5(2)）。
    """
    out: dict[str, int | date | bool | None] = {}
    for etype, label in (("earnings", "earnings"), ("dividend", "dividend")):
        same = [e for e in events if e.event_type == etype]
        future = sorted([e for e in same if e.event_date > observed_on], key=lambda e: e.event_date)
        past = sorted([e for e in same if e.event_date <= observed_on], key=lambda e: e.event_date)
        nxt = future[0] if future else None
        lst = past[-1] if past else None
        out[f"days_to_next_{label}"] = days_between(observed_on, nxt.event_date) if nxt else None
        out[f"days_since_last_{label}"] = days_between(lst.event_date, observed_on) if lst else None
        out[f"next_{label}_date"] = nxt.event_date if nxt else None
        out[f"next_{label}_is_estimated"] = nxt.is_estimated if nxt else None
    return out
