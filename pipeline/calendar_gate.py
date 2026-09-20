"""「今天是否开盘 / 数据是否可信」闸门（§7.2）。

**不要只靠日历或只靠时钟，两者都会骗你。** 四重判定，前三重在这里，
第四重（供应商脏数据）在 ``fetch.py`` —— 因为它需要数据本身。

1. **日历**：今天是不是交易日，当日**实际**收盘时间是几点（半日市 13:00）。
2. **时钟**：当前 ET ≥ 当日实际收盘 + ``settle_minutes``，否则 ``skipped_too_early``。
3. **数据自证（逐标的，不是只看 QQQ）**：每一个标的的最新 bar 日期 == 当日 session。
   初稿只断言 QQQ，于是「某一只股票拿到昨天的 bar」既不算抓取失败、也过得了闸门：
   AVGO 的 ``mom_20`` 会用一个错位一天的窗口去和 15 个日期正确的同行排名，
   全站头条的三强榜就是一个**混合日期的横截面**，而 §4.2 的 ``days_in_top_n``
   会把这个错误**永久烤进历史**。

时钟一律用**可注入的 ``now``**：§11 M5 的验收标准是「冻结时钟的单元测试覆盖
4 个 cron 时刻 × 2 个时区(EST/EDT) × 2 类交易日 = 16 种组合」，
而夏令时**无法靠等待来验证** —— 要等到 3 月或 11 月。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from collections.abc import Mapping, Sequence

    from pipeline.sessions import Session

__all__ = [
    "ET",
    "GateDecision",
    "gate_opens_at",
    "interior_gaps",
    "session_on_or_before",
    "stale_symbols",
    "when_to_run",
]

#: 全项目唯一的交易所时区常量。
ET = ZoneInfo("America/New_York")

Decision = Literal["run", "skipped_holiday", "skipped_too_early"]


@dataclass(frozen=True, slots=True)
class GateDecision:
    decision: Decision
    session: Session | None
    opens_at: datetime | None
    reason: str

    @property
    def should_run(self) -> bool:
        return self.decision == "run"


def gate_opens_at(session: Session, settle_minutes: int) -> datetime:
    """闸门 2 放行的那一刻：**当日实际收盘** + ``settle_minutes``（ET）。

    用 session 自己的 ``close_et`` 而不是写死 16:00 —— 半日市 13:00 收盘则
    14:00 ET 放行，规则自动适配。

    把 ET 墙上时间与 :data:`ET` 组合是无歧义的：美国夏令时切换发生在
    周日 02:00，而交易日的收盘时刻（13:00 / 16:00）永远不落在那个折叠区间里。
    """
    return datetime.combine(session.date, session.close_et, tzinfo=ET) + timedelta(
        minutes=settle_minutes
    )


def session_on_or_before(sessions: Sequence[Session], day: date) -> Session | None:
    """不晚于 ``day`` 的最后一个 session。周末 / 假日 dispatch 时用得上。"""
    past = [s for s in sessions if s.date <= day]
    return max(past, key=lambda s: s.ordinal) if past else None


def when_to_run(
    sessions: Sequence[Session],
    now: datetime,
    settle_minutes: int,
) -> GateDecision:
    """闸门 1 + 闸门 2 合在一起判一次。

    ``now`` 必须**带时区**。裸 datetime 在这里是一整类时区 bug 的入口，
    而它们的表现是「闸门早放行三小时」这种看起来完全正常的行为。
    """
    if now.tzinfo is None:
        raise ValueError("now 必须带时区 —— 裸 datetime 会让闸门 2 的判定悄悄错位")
    now_et = now.astimezone(ET)
    today = now_et.date()

    session = next((s for s in sessions if s.date == today), None)
    if session is None:
        return GateDecision(
            decision="skipped_holiday",
            session=None,
            opens_at=None,
            reason=f"{today} 不是交易日",
        )

    opens = gate_opens_at(session, settle_minutes)
    if now_et < opens:
        return GateDecision(
            decision="skipped_too_early",
            session=session,
            opens_at=opens,
            reason=(
                f"{today} 收盘 {session.close_et} ET"
                f"{'（半日市）' if session.is_half_day else ''}"
                f"，+{settle_minutes} 分钟后（{opens:%H:%M} ET）才放行；现在 {now_et:%H:%M} ET"
            ),
        )
    return GateDecision(
        decision="run",
        session=session,
        opens_at=opens,
        reason=f"{today} 已收盘 {settle_minutes} 分钟以上",
    )


def last_settled_session(
    sessions: Sequence[Session],
    now: datetime,
    settle_minutes: int,
) -> Session:
    """最后一个**已经过了 settle_minutes** 的 session。

    这个函数只为一件事存在：**修复跑不能用今天那根 bar。**

    ``when_to_run`` 在 ``skipped_too_early`` 分支里返回的 ``session`` 是**今天** ——
    它是给「还要等到几点」那条消息用的，不是「该算哪一天」的答案。
    而日历历史修订会把闸门顶开（§9.1.4），于是一次**自动触发**的修复
    会拿着这个「今天」去抓一根还没定稿的 bar：
    ``daily.yml`` 的 16:00 ET 那条 cron 上就是敲钟那一刻的价，
    手动 dispatch 可以是盘中价。

    两道后闸门都拦不住它：闸门 3 比的是日期相等（日期就是今天，通过），
    闸门 4 的阈值是 50% 日内波动与 2% 跨源差（preliminary 与 consolidated
    的差是千分位，通过）。写进去的正是 ``daily.yml`` 文件头那句
    「**宁可晚一小时，不要一个会变的数字**」要防的东西。

    修复需要的只是历史窗口，根本不需要今天那根。
    """
    now_et = now.astimezone(ET)
    today = now_et.date()
    settled = [
        s for s in sessions if s.date <= today and gate_opens_at(s, settle_minutes) <= now_et
    ]
    if not settled:
        raise ValueError(f"{today} 之前没有任何已定稿的 session（settle={settle_minutes} 分钟）")
    return max(settled, key=lambda s: s.ordinal)


def interior_gaps(
    bars_by_symbol: Mapping[str, Sequence[date]],
    sessions: Sequence[date],
) -> dict[str, int]:
    """闸门 3 的另一半：窗口**中间**缺了 bar 的标的，以及缺了几根。

    ``stale_symbols`` 只看**最新**一根，于是一个内部空洞（某天限流、薄票、
    供应商单日故障）完全过得了闸门 —— 而它的后果是安静的：
    收益样本被悄悄缩短，alpha/beta 虽然会按日期对齐丢掉空洞两侧那两天，
    却仍可能满足 ``min_obs`` 并给出一个看起来完全合理的数。

    **只数「自己第一根之后」的空洞。** 历史本来就短（新加入的标的）不是缺口，
    那种情况由 §3.3 的 ``provisional`` 灰标负责，而把它算成缺口会让那个标的
    在补够历史之前每天都 partial —— 长期飘红的告警等于没有告警。
    """
    out: dict[str, int] = {}
    for sym, bars in bars_by_symbol.items():
        if not bars:
            continue
        have = set(bars)
        first = min(have)
        expected = [d for d in sessions if first <= d <= max(have)]
        missing = len(expected) - len(have & set(expected))
        if missing > 0:
            out[sym] = missing
    return out


def stale_symbols(
    latest_bar: Mapping[str, date | None],
    session_date: date,
) -> tuple[str, ...]:
    """闸门 3：**逐标的**断言最新 bar 日期 == 当日 session 日期。

    返回落后的标的。调用方对它们：指标写 ``NULL``、排除出排名池、
    状态 ``partial``、在 §10.5 的「部分标的缺失」态里显示出来。

    **不要只看基准。** 初稿只断言 QQQ，于是一只股票拿到昨天的 bar 时，
    它既不算抓取失败也过得了闸门 —— 而排名是横截面的，
    一个错位一天的窗口会和 15 个日期正确的同行一起排，
    产出一个**混合日期的横截面**，然后被 ``days_in_top_n`` 永久烤进历史。
    """
    return tuple(sorted(sym for sym, d in latest_bar.items() if d is None or d != session_date))
