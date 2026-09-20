"""交易日历 → ``trading_sessions`` 的**幂等全量对账**（§9.1.4）。

顺序是「同步日历 → 闸门判定 → 抓取 → 计算」。闸门 1 和闸门 2 都读这张表，
而日历包会随新公布的假日更新 —— **用一张过期的表做闸门判定毫无意义**。

对账是全量重生成，不是增量追加：``ordinal`` 由排序后的序列重新推导。
``ordinal int unique`` 只保证唯一，**不保证无缺口**，而「相邻 session」
这个判据完全建立在「差 1」之上。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from pipeline.sessions import (
    CalendarRevision,
    assert_left_endpoint,
    build_sessions,
    diff_sessions,
    horizon_end,
    load_calendar,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    import psycopg

    from pipeline.config import Config
    from pipeline.sessions import Session

__all__ = ["plan_sessions", "reconcile", "session_rows", "write_sessions"]


def session_rows(sessions: list[Session]) -> list[dict[str, Any]]:
    return [
        {
            "date": s.date,
            "ordinal": s.ordinal,
            "is_half_day": s.is_half_day,
            "close_et": s.close_et,
        }
        for s in sessions
    ]


def reconcile(cfg: Config, today: date) -> list[Session]:
    """从固定左端点到 ``today + sessions_horizon`` 重新生成整张表。

    左端点校验是**显式**的：``build_sessions`` 接受任意 start 并无条件从 1
    重新编号，而「ordinal 无缺口」那条不变式抓不到整体重编号 ——
    它依然是无缺口的（§9.1.4）。
    """
    cal = load_calendar()
    end = horizon_end(cal, today, cfg.app.sessions_horizon)
    sessions = build_sessions(cal, cfg.app.sessions_start_date, end)
    assert_left_endpoint(sessions, cfg.app.sessions_start_date)
    return sessions


def plan_sessions(
    conn: psycopg.Connection[Any], cfg: Config, today: date
) -> tuple[list[Session], CalendarRevision]:
    """**只读**：算出新表并与库里现有的比对。不写、不提交。

    写入被单独拆到 :func:`write_sessions`，由调用方放进 T2 —— 两条约束
    必须同时满足，而它们看起来是矛盾的：

    1. **不能在这里提交**（§9.1.4）：一次以 failed 收场的运行若已经改写了
       ordinal，就可能删掉 strength_daily 还在引用的历史日期，
       于是那些行从视图的 INNER join 里无声消失、`days_in_top_n` 从头数起。
    2. **不能把事务一直开着**：整窗抓取按 §7.3.1 的设计要跑 5–10 分钟，
       挂着一个 idle-in-transaction 的连接会挡住 vacuum，
       而且是 pooler 空闲事务杀手最爱的形状 —— 被杀的表现是一次健康运行报 failed。

    拆成「先只读地算完、放掉事务、抓取、再在 T2 里写」就同时满足了两条。

    返回的差异要分类（§9.1.4 第 3 条）：地平线延长每天都会发生，
    **无需任何重算**；历史日增减才要从最早变化点起做一次完整修复。
    不分开的话每天都会触发一次「完整修复」，那既昂贵又会把 ``partial``
    变成常态 —— 而常态化的告警等于没有告警。
    """
    with conn.cursor() as cur:
        cur.execute("select date from trading_sessions")
        old: list[date] = [r[0] for r in cur.fetchall()]

    sessions = reconcile(cfg, today)
    revision = diff_sessions(old, [s.date for s in sessions], today=today)
    return sessions, revision


def write_sessions(conn: psycopg.Connection[Any], sessions: list[Session]) -> None:
    """**在 T2 里**把整张表重写一遍。见 :func:`plan_sessions` 的两条约束。"""
    rows = session_rows(sessions)
    with conn.cursor() as cur:
        # 全量对账：先清空再写。这张表可从日历包完整再生，删得起 ——
        # 写入角色对它**有** DELETE 权限正是为了这一步（§8.1.1）。
        cur.execute("delete from trading_sessions")
        cur.executemany(
            "insert into trading_sessions (date, ordinal, is_half_day, close_et) "
            "values (%s, %s, %s, %s)",
            [(r["date"], r["ordinal"], r["is_half_day"], r["close_et"]) for r in rows],
        )
