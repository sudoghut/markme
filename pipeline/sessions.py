"""交易日历 → ``trading_sessions``，以及 §9.1.4 的运维契约。

**这张表自己就可能成为新的错数来源**，所以契约比 schema 重要：

1. 每次计算之前先对账（顺序：同步日历 → 闸门判定 → 抓取 → 计算）。
   闸门 1、2 都读这张表，而日历包会随新公布的假日更新 ——
   用一张过期的表做闸门判定毫无意义。
2. **幂等的全量对账，不是增量追加。** 每次从 ``sessions_start_date`` 到
   ``今天 + sessions_horizon`` 重新生成整张表，``ordinal`` 由排序后的序列
   **重新推导**。``ordinal int unique`` 只保证唯一，**不保证无缺口**，
   而「相邻 session」这个判据完全建立在「差 1」之上。
3. 日历修订要能被**发现**，并区分两种：地平线延长（无需重算）
   vs 历史日增减（必须从最早变化点起完整修复）。
4. 指标的输入必须显式按这张表过滤，而不是「``prices_daily`` 里有什么就用什么」。

> ``sessions_start_date`` **固定，永不前移** —— 整套方案的支点。
> ``ordinal`` 每天重新推导，它的正确性完全押在左端点不动上。
> 左端点一旦前移，``v_strength_enriched`` 的 INNER join 会无声吞掉老行，
> 而幸存的第一行 ``prev_ord`` 为 NULL → ``days_in_top_n`` 悄悄归 1。

日历名见 :data:`CALENDAR_NAME` —— 设计文档写的 ``XNAS`` **不存在**，实测见那里。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    import pandas as pd

__all__ = [
    "CALENDAR_NAME",
    "CalendarRevision",
    "Session",
    "assert_left_endpoint",
    "build_sessions",
    "diff_sessions",
    "horizon_end",
    "load_calendar",
    "lookback_start",
    "lookback_window",
]

#: 常规收盘。半日市会被日历识别为 13:00，见 :func:`build_sessions`。
_REGULAR_CLOSE = time(16, 0)

#: ``pandas_market_calendars`` 里的日历名。
#:
#: **设计文档 §7.2 闸门 1 写的是 ``XNAS``，而那个名字根本不存在。**
#: pandas_market_calendars 5.4.0 的 registry 里没有 ``XNAS``，
#: ``get_calendar("XNAS")`` 直接抛 ``RuntimeError`` —— 按文档字面实现，
#: 闸门 1 会在第一次运行时崩掉。
#:
#: 实测三件事（见 ``test_sessions.py`` 与 M4 的 review 记录）：
#:
#: 1. ``get_calendar("NASDAQ").name`` 返回 **``"NYSE"``** ——
#:    这个包里 NASDAQ 与 NYSE 本来就是同一个日历类。
#: 2. NASDAQ 与 ``XNYS`` 在 2023-01-01–2026-12-31 上**完全一致**：
#:    各 1003 天，无日期差异，无收盘时间差异。
#: 3. 半日市被正确识别：2024-07-03 收 13:00 ET，2024-07-04 不在表里。
#:
#: 所以 §7.2 那句「两者的假日与半日市实际完全一致」是对的，
#: 只是那个日历代码是错的。文档已按实测改写。
CALENDAR_NAME = "NASDAQ"


def load_calendar(name: str = CALENDAR_NAME) -> Any:
    """取交易日历。单独包一层，是为了让那个名字只出现在一个地方。"""
    import pandas_market_calendars as mcal

    return mcal.get_calendar(name)


@dataclass(frozen=True, slots=True)
class Session:
    """一个交易日。``ordinal`` 由排序后的序列推导，不沿用旧值。"""

    date: date
    ordinal: int
    close_et: time
    is_half_day: bool


class _Calendar(Protocol):
    """``pandas_market_calendars`` 的日历对象里我们真正用到的那一点点。

    用 Protocol 而不是直接 import，是为了让测试能塞一个合成日历进来 ——
    否则「历史日被增减」这条路径永远没法测，而它正是 §9.1.4 整节的主题。
    """

    def schedule(self, start_date: object, end_date: object) -> pd.DataFrame: ...


def build_sessions(
    calendar: _Calendar,
    start: date,
    end: date,
) -> list[Session]:
    """从日历生成 ``[start, end]`` 区间内的全部 session，``ordinal`` 从 1 重新推导。

    ``ordinal`` **必须**从固定左端点起连续编号，且每次对账都重算 ——
    这正是「无缺口」的来源。绝不能沿用库里的旧值：日历修订会让某一天
    凭空出现或消失，而沿用旧值时「相邻 session」的判断会整体错位，
    ``mom_20`` 的「20 个 session 前」指向错误的行，**而所有闸门全绿**。
    """
    if end < start:
        raise ValueError(f"end({end}) 早于 start({start})")

    import pandas as pd

    sched = calendar.schedule(start_date=start, end_date=end)
    # **显式排序。** §9.1.4 的原话是「ordinal 由**排序后的** session 序列重新推导」。
    # 真日历总是升序返回，所以这行在生产上是空操作 —— 但这个模块故意暴露了
    # 一个 Protocol 好让测试塞进合成日历，而一个乱序的合成日历会**静默**
    # 产出错误的 ordinal。一行的代价，换掉一整类无症状故障。
    sched = sched.sort_index()
    out: list[Session] = []
    for ordinal, (idx, row) in enumerate(sched.iterrows(), start=1):
        close_et = _close_time_et(row)
        # iterrows() 的 index 对 mypy 是 Hashable；这张表的 index 一定是
        # DatetimeIndex，但类型系统不知道，所以在这里收窄一次。
        out.append(
            Session(
                date=pd.Timestamp(cast("date", idx)).date(),
                ordinal=ordinal,
                close_et=close_et,
                is_half_day=close_et < _REGULAR_CLOSE,
            )
        )
    return out


def _close_time_et(row: object) -> time:
    """从 schedule 的一行里取出**当日实际**收盘时间（ET）。

    半日市 13:00 必须被正确识别 —— §7.2 闸门 2 的「收盘 + settle_minutes」
    完全依赖这个值。写死 16:00 会让半日市当天的闸门晚 3 小时放行。
    """
    import pandas as pd

    close = row["market_close"]  # type: ignore[index]
    ts = pd.Timestamp(close)
    if ts.tzinfo is None:
        # **不猜。** 初版在这里假设「无时区 = UTC」，而市场日历给的无时区时间
        # 压倒性地是**交易所本地**时间。猜错的后果不是报错，是一个看起来
        # 完全合理的行：一个 16:00 的常规收盘会变成 11:00 ET 并被标成半日市，
        # 于是闸门 2 提前 5 小时放行。宁可在这里响亮地停下。
        raise ValueError(
            f"market_close（{close!r}）没有时区。市场日历给的无时区时间通常是"
            "交易所本地时间，而把它当 UTC 会让常规收盘被误判成半日市、"
            "闸门 2 提前 5 小时放行 —— 这里拒绝猜。"
        )
    return ts.tz_convert("America/New_York").time()


@dataclass(frozen=True, slots=True)
class CalendarRevision:
    """两版日历的差异。**区分两种，因为处置完全不同。**"""

    #: 新增的历史日（``<= today``）。必须从最早那天起完整修复。
    added_historical: tuple[date, ...]
    #: 消失的历史日（``<= today``）。同上。
    removed_historical: tuple[date, ...]
    #: 地平线延长带来的未来新增。**无需任何重算。**
    added_future: tuple[date, ...]
    #: 未来日的消失（新公布的假日落在地平线内）。不影响已算出的历史。
    removed_future: tuple[date, ...]

    @property
    def needs_repair(self) -> bool:
        """是否需要触发 §9.1.4 第 3 条的完整修复。"""
        return bool(self.added_historical or self.removed_historical)

    @property
    def repair_from(self) -> date | None:
        """修复的起点：**最早发生变化的那个 session**。"""
        changed = self.added_historical + self.removed_historical
        return min(changed) if changed else None

    def describe(self) -> str:
        """给 ``runs.message`` 用的一行摘要（§9.1.4 第 5 条）。"""
        if not (
            self.added_historical
            or self.removed_historical
            or self.added_future
            or self.removed_future
        ):
            return "日历无变化"
        parts = []
        if self.added_historical:
            parts.append(f"历史新增 {len(self.added_historical)} 天（{self.added_historical[0]}…）")
        if self.removed_historical:
            parts.append(
                f"历史消失 {len(self.removed_historical)} 天（{self.removed_historical[0]}…）"
            )
        if self.added_future:
            parts.append(f"地平线延长 {len(self.added_future)} 天")
        if self.removed_future:
            parts.append(f"未来日消失 {len(self.removed_future)} 天")
        return "；".join(parts)


def diff_sessions(
    old: list[Session] | list[date],
    new: list[Session] | list[date],
    *,
    today: date,
) -> CalendarRevision:
    """比对两版日历，按「历史 / 未来」分类。

    **分类是这个函数存在的全部理由。** 地平线延长每天都会发生
    （表向未来预填 ``sessions_horizon`` 个交易日），若不把它和历史修订分开，
    每天都会触发一次「完整修复」—— 那既昂贵又会把 ``partial`` 变成常态，
    而常态化的告警等于没有告警。
    """
    old_dates = {_as_date(s) for s in old}
    new_dates = {_as_date(s) for s in new}
    added = new_dates - old_dates
    removed = old_dates - new_dates
    return CalendarRevision(
        added_historical=tuple(sorted(d for d in added if d <= today)),
        removed_historical=tuple(sorted(d for d in removed if d <= today)),
        added_future=tuple(sorted(d for d in added if d > today)),
        removed_future=tuple(sorted(d for d in removed if d > today)),
    )


def _as_date(s: Session | date) -> date:
    return s.date if isinstance(s, Session) else s


def horizon_end(calendar: _Calendar, today: date, sessions_horizon: int) -> date:
    """向未来预填 ``sessions_horizon`` 个**交易日**对应的日历日期（§9.1.4 第 2 条）。

    和 :func:`lookback_start` 是对称的一对，而对称正是它存在的理由：
    那边用整段注释论证了「bar 数换日期必须走日历」，这边却没有对应的函数，
    于是调用方最顺手的写法是 ``today + timedelta(days=60)`` —— **那是错的**。
    实测：从 2026-09-21 往后 60 个**日历日**只有 45 个交易日；
    要凑够 60 个交易日需要 84 个日历日。

    后果比 :func:`lookback_start` 轻（地平线短只是少预填，不会算错数），
    但这种不对称本身就是邀请。
    """
    if sessions_horizon <= 0:
        raise ValueError("sessions_horizon 必须为正")
    # 交易日约占日历日的 69%，乘 2 再加一周，一次查询足够覆盖。
    probe_end = today + timedelta(days=sessions_horizon * 2 + 7)
    future = [s for s in build_sessions(calendar, today, probe_end) if s.date > today]
    if len(future) >= sessions_horizon:
        return future[sessions_horizon - 1].date
    return probe_end


def assert_left_endpoint(sessions: list[Session], expected_start: date) -> None:
    """``sessions_start_date`` **固定，永不前移** —— 这是整套方案的支点（§9.1.4）。

    ``ordinal`` 每次对账都重新推导，它的正确性完全押在左端点不动上。
    左端点一旦**后**移，``v_strength_enriched`` 的 INNER join 会无声吞掉老行，
    而幸存的第一行 ``prev_ord`` 为 NULL → ``days_in_top_n`` 悄悄归 1。

    而 :func:`build_sessions` 接受**任意** start 并无条件从 1 重新编号，
    「ordinal 无缺口」那条不变式也抓不到 —— 整体重编号依然是无缺口的。
    所以这一条必须显式断言，不能指望别的检查兜住。
    """
    if not sessions:
        raise ValueError("session 列表为空，无法校验左端点")
    actual = min(s.date for s in sessions)
    if actual != expected_start:
        raise ValueError(
            f"session 左端点是 {actual}，而 app.yaml 的 sessions_start_date 是 "
            f"{expected_start}。**左端点只能往过去挪，不能往未来挪**（§9.1.4）—— "
            "它后移会让老的 strength_daily 行从视图里无声消失，"
            "而「在榜 N 天」会悄悄从头数起。"
        )


def lookback_window(sessions: list[Session], today: date, lookback_bars: int) -> tuple[date, int]:
    """回看窗口的 ``(左端点, 实际拿到的 bar 数)``。

    返回 bar 数是为了让「窗口不够长」**说得出口**。只返回一个日期时，
    调用方分不清「历史本来就短，正常」和「拿到的 session 列表是残缺的」——
    而 §6.1.1 点名的正是这个失败：「EMA 的喂入量直接掉到 360 以下，
    而**没有任何东西会报警**」。
    """
    start = lookback_start(sessions, today, lookback_bars)
    n = sum(1 for s in sessions if start <= s.date <= today)
    return start, n


def lookback_start(sessions: list[Session], today: date, lookback_bars: int) -> date:
    """回看窗口的左端点：``ordinal = 今天的 ordinal - lookback_bars + 1`` 那一天。

    **``lookback_bars`` 是 bar 数，而供应商 API 只认日期 —— 这个转换必须走日历。**
    随手写 ``start = today - 400 days`` 只会拿到约 275 根
    （400 个交易日 ≈ 574 个自然日），EMA 的喂入量直接掉到 360 以下
    （§12 #5 标准 B），而**没有任何东西会报警**。

    ``today`` 不是交易日时（周末、假日回填）取**最后一个不晚于它**的 session，
    而不是抛异常 —— ``backfill.yml`` 是 dispatch 触发的，周末跑很正常。
    窗口不足 ``lookback_bars`` 时返回最早的那天，让调用方拿到「能给的全部」。
    """
    if lookback_bars <= 0:
        raise ValueError("lookback_bars 必须为正")
    past = [s for s in sessions if s.date <= today]
    if not past:
        raise ValueError(f"{today} 之前没有任何 session —— sessions_start_date 是不是太晚了？")
    anchor = max(past, key=lambda s: s.ordinal)
    target = anchor.ordinal - lookback_bars + 1
    by_ordinal = {s.ordinal: s for s in sessions}
    if target in by_ordinal:
        return by_ordinal[target].date
    return min(s.date for s in sessions)
