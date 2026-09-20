"""psycopg 写入：三个事务 + NaN sanitizer + ``runs`` 日志（§9.1.2 / §9.1.3）。

**为什么是三个事务，不是一个。** 把 ``runs`` 的 ``running`` 行也放进那个大事务里，
OOM / 超时 / 被取消时它会和数据一起回滚 —— 于是「开跑即插 running」根本没有解决
§7.2 说的那个可观测性盲区（硬崩溃时一行都不留）。

===  ==========================================================  ====================
事务  内容                                                        目的
===  ==========================================================  ====================
T1   ``insert into runs (status='running')`` 并**立即提交**        崩溃也留痕
T2   prices → metrics → strength（含删+插）**一个原子单元**        不会出现「昨天的
                                                                  榜单配今天的指标」
T3   ``update runs set status=…, finished_at=…`` 并提交           终态
===  ==========================================================  ====================

崩溃落在 T2 中间 → 数据完整回滚，而 ``runs`` 里留下一行永远停在 ``running``
—— **这正是我们想要的信号**：下一跑看到本日存在未完成的 running 行即可判定上一跑硬崩。

**为什么不走 PostgREST**：它做不了多语句事务。于是不只是删+插不原子，
``prices → metrics → strength → runs`` 这整条链都不原子 —— 在第 2、3 步之间崩溃，
会留下「昨天的榜单」配「今天的指标」，且无任何标记。

**每张表的冲突目标不同。** 初稿笼统写成「所有写入用 ``on conflict (symbol, date)``」
是错的：``strength_daily`` 主键是 ``(date, symbol)`` 且必须删+插；
``runs`` 是「先追加一行，随后只更新这一行的终态，从不删除」。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import psycopg
from psycopg import sql

from pipeline.sanitize import clean_records

__all__ = [
    "METRICS_WRITE_COLUMNS",
    "PRICE_WRITE_COLUMNS",
    "STRENGTH_WRITE_COLUMNS",
    "PriceDiff",
    "RunStatus",
    "connect",
    "data_transaction",
    "delete_events_window",
    "diff_prices",
    "finish_run",
    "insert_events",
    "replace_strength",
    "start_run",
    "sync_symbols",
    "touch_fetch_state",
    "upsert_metrics",
    "upsert_prices",
]

RunStatus = Literal[
    "running",
    "ok",
    "ok_events_stale",
    "skipped_holiday",
    "skipped_too_early",
    "skipped_already_done",
    "stale_vendor",
    "partial",
    "failed",
]

#: ``adj_factor`` 是生成列，不写。``updated_at`` 由触发器维护。
PRICE_WRITE_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "source",
)

#: ``computed_at`` 由触发器维护。
METRICS_WRITE_COLUMNS = (
    "symbol",
    "date",
    "rsi_14",
    "ema_60",
    "close_vs_ema60_pct",
    "ema60_slope_20d",
    "alpha_annual",
    "beta",
    "r2",
    "corr",
    "resid_vol_annual",
    "alpha_t_stat",
    "n_obs",
    "mom_20",
    "days_to_next_earnings",
    "days_since_last_earnings",
    "days_to_next_dividend",
    "days_since_last_dividend",
    "next_earnings_date",
    "next_dividend_date",
    "next_earnings_is_estimated",
    "next_dividend_is_estimated",
    "extra",
    "provisional_metrics",
)

STRENGTH_WRITE_COLUMNS = (
    "date",
    "symbol",
    "rank",
    "score",
    "score_metric",
    "rank_pool",
    "benchmark",
    "top_n",
    "in_top_n",
    "delta_to_next",
    "delta_to_median",
)

#: ``jsonb`` 列要先序列化；``text[]`` 列直接交给 psycopg。
_JSON_COLUMNS = frozenset({"extra"})

#: ``prices_daily.close`` / ``adj_close`` 是 ``numeric(14,4)``。
#:
#: **差分必须按存储精度比，不能按 float 比。** 写进去的是全精度 float，
#: 读回来的是 4 位小数的 Decimal —— 直接比会让**每一行都「变了」**。
#: 实测：第二次回填报了 6698/6800 行变化，以及 12 个标的的「复权因子变化」，
#: 而两次运行之间隔了几分钟、供应商那边什么都没发生。
#:
#: 两个后果都很坏：§7.3.1 的写入量优化完全失效（6698 行而不是 1 行），
#: 而 §3.0 规则 2 的复权因子探测器**每一跑都误报** ——
#: 一个天天喊狼来了的探测器，等于没有探测器。
_PRICE_SCALE = 4


def connect(dsn: str) -> psycopg.Connection[Any]:
    """建连接。**session 模式的 pooler**，见 §8.1.1 的实测结论。

    直连 ``db.<ref>.supabase.co:5432`` 在 Actions 上连不通（免费层直连是
    IPv6-only，runner 是 IPv4）；pooler 的 transaction 模式（6543）会坏掉
    §9.1.2 的多语句事务与 psycopg 的预编译语句。用户名是 ``<role>.<ref>``。
    """
    return psycopg.connect(dsn, autocommit=False)


# ---------------------------------------------------------------------------
# T1 / T3：runs
# ---------------------------------------------------------------------------
def start_run(conn: psycopg.Connection[Any], session_date: date, git_sha: str | None) -> int:
    """T1：插一行 ``running`` 并**立即提交**。

    立即提交是这条设计的全部意义 —— 它必须在数据事务**之外**，
    否则硬崩时会和数据一起回滚，而那正是它要记录的那一刻。
    """
    with conn.cursor() as cur:
        cur.execute(
            "insert into private.runs (session_date, status, started_at, git_sha) "
            "values (%s, 'running', now(), %s) returning id",
            (session_date, git_sha),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:  # pragma: no cover - returning 必然给一行
        raise RuntimeError("insert into private.runs 没有返回 id")
    return int(row[0])


def finish_run(
    conn: psycopg.Connection[Any],
    run_id: int,
    status: RunStatus,
    *,
    rows_prices: int | None = None,
    rows_metrics: int | None = None,
    message: str | None = None,
) -> None:
    """T3：写终态并提交。``runs`` **只追加和改终态，从不删除**。"""
    with conn.cursor() as cur:
        cur.execute(
            "update private.runs set status = %s, finished_at = now(), "
            "rows_prices = %s, rows_metrics = %s, message = %s where id = %s",
            (status, rows_prices, rows_metrics, _truncate(message), run_id),
        )
    conn.commit()


def _truncate(message: str | None, limit: int = 4000) -> str | None:
    if message is None:
        return None
    return message if len(message) <= limit else message[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# T2：一个原子单元
# ---------------------------------------------------------------------------
@contextmanager
def data_transaction(conn: psycopg.Connection[Any]) -> Any:
    """T2：``prices → metrics → strength`` 必须在**同一个**事务里。

    任何一步失败都整体回滚，于是不会出现「昨天的榜单配今天的指标」。
    """
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def _rows_to_tuples(
    rows: Sequence[dict[str, Any]], columns: Sequence[str]
) -> list[tuple[Any, ...]]:
    """**唯一的写入入口**：每一行都过 sanitizer（§9.1.3）。

    把它做成一个窄入口，是为了让「哪里可能漏掉 sanitize」有唯一的答案。
    """
    cleaned = clean_records(rows)
    out: list[tuple[Any, ...]] = []
    for row in cleaned:
        values: list[Any] = []
        for col in columns:
            v = row.get(col)
            values.append(json.dumps(v) if col in _JSON_COLUMNS and v is not None else v)
        out.append(tuple(values))
    return out


def _upsert(
    conn: psycopg.Connection[Any],
    table: str,
    columns: Sequence[str],
    conflict: Sequence[str],
    rows: Sequence[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    tuples = _rows_to_tuples(rows, columns)
    updatable = [c for c in columns if c not in conflict]
    stmt = sql.SQL(
        "insert into {table} ({cols}) values ({ph}) "
        "on conflict ({conflict}) do update set {assigns}"
    ).format(
        table=sql.Identifier(*table.split(".")),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        ph=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        conflict=sql.SQL(", ").join(sql.Identifier(c) for c in conflict),
        assigns=sql.SQL(", ").join(
            sql.SQL("{c} = excluded.{c}").format(c=sql.Identifier(c)) for c in updatable
        ),
    )
    with conn.cursor() as cur:
        cur.executemany(stmt, tuples)
    return len(tuples)


def upsert_prices(conn: psycopg.Connection[Any], rows: Sequence[dict[str, Any]]) -> int:
    """``prices_daily``：冲突目标 ``(symbol, date)``。**不删除** ——
    写入角色对这张表根本没有 DELETE 权限（§8.1.1 实测）。"""
    return _upsert(conn, "prices_daily", PRICE_WRITE_COLUMNS, ("symbol", "date"), rows)


def upsert_metrics(conn: psycopg.Connection[Any], rows: Sequence[dict[str, Any]]) -> int:
    """``metrics_daily``：同上。修复只能走 UPDATE / upsert，永远不走 DELETE。"""
    return _upsert(conn, "metrics_daily", METRICS_WRITE_COLUMNS, ("symbol", "date"), rows)


def replace_strength(
    conn: psycopg.Connection[Any], day: date, rows: Sequence[dict[str, Any]]
) -> int:
    """``strength_daily``：**先删当日、再插**（§9.1.2）。

    只 upsert 不删，当某次重跑产出的标的数比上次少时（例如闸门 3 排除了两只），
    上一次的尾部名次会**存活下来**，显示出的榜单是**两次计算的拼接** ——
    而这恰恰是 §7.2「重跑、补跑…结果都一样」承诺要保证的那张表。
    """
    with conn.cursor() as cur:
        cur.execute("delete from strength_daily where date = %s", (day,))
    if not rows:
        return 0
    tuples = _rows_to_tuples(rows, STRENGTH_WRITE_COLUMNS)
    stmt = sql.SQL("insert into strength_daily ({cols}) values ({ph})").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in STRENGTH_WRITE_COLUMNS),
        ph=sql.SQL(", ").join(sql.Placeholder() * len(STRENGTH_WRITE_COLUMNS)),
    )
    with conn.cursor() as cur:
        cur.executemany(stmt, tuples)
    return len(tuples)


def sync_symbols(conn: psycopg.Connection[Any], rows: Sequence[dict[str, Any]]) -> int:
    """config → ``symbols``。**移出 config 的标的置 ``enabled = false``，不删除。**

    ``prices_daily.symbol`` 对 ``symbols`` 有外键，``delete from symbols``
    会被挡住；而且真删会让回补与历史榜单都断（§6.1.1 的那个例外）。
    """
    n = _upsert(
        conn,
        "symbols",
        ("symbol", "name", "type", "is_benchmark", "enabled", "expects_earnings"),
        ("symbol",),
        rows,
    )
    keep = [r["symbol"] for r in rows]
    with conn.cursor() as cur:
        if keep:
            cur.execute(
                "update symbols set enabled = false where not (symbol = any(%s)) and enabled",
                (keep,),
            )
        else:
            cur.execute("update symbols set enabled = false where enabled")
    return n


# ---------------------------------------------------------------------------
# 事件：整窗删 + 插（§3.5(1)）
# ---------------------------------------------------------------------------
def delete_events_window(
    conn: psycopg.Connection[Any], symbol: str, event_type: str, coverage_start: date
) -> None:
    """**只在该标的该端点抓取成功时调用**，且与 insert 同一事务。

    即便本次要插入 0 行，delete 也照常执行 —— 那正是「财报被取消」
    这条路径：已作废的未来行必须消失。
    ``coverage_start`` 之前的历史永远不动。
    """
    with conn.cursor() as cur:
        cur.execute(
            "delete from symbol_events where symbol = %s and event_type = %s and event_date >= %s",
            (symbol, event_type, coverage_start),
        )


def insert_events(conn: psycopg.Connection[Any], rows: Sequence[dict[str, Any]]) -> int:
    """``symbol_events`` 只追加（幂等性来自上面的整窗删）。``id`` 是 identity，不写。"""
    if not rows:
        return 0
    cols = ("symbol", "event_type", "event_date", "is_estimated", "amount", "source")
    tuples = _rows_to_tuples(rows, cols)
    stmt = sql.SQL("insert into symbol_events ({cols}) values ({ph})").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        ph=sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )
    with conn.cursor() as cur:
        cur.executemany(stmt, tuples)
    return len(tuples)


def touch_fetch_state(conn: psycopg.Connection[Any], symbols: Iterable[str]) -> None:
    """记「我今天查过了」（§3.5(4)）。

    **不要依赖 ``symbol_events.updated_at``** —— 那张表走整窗删+插，
    每次重写后它都是「最近一次写入」而不是「最近一次尝试抓取」。
    抓取成功但内容没变时，你仍然需要知道自己查过。
    """
    rows = [(s,) for s in symbols]
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "insert into private.fetch_state (symbol, last_event_fetch_at) "
            "values (%s, now()) "
            "on conflict (symbol) do update set last_event_fetch_at = excluded.last_event_fetch_at",
            rows,
        )


# ---------------------------------------------------------------------------
# 差分：只写与库里真正不同的行（§7.3.1）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PriceDiff:
    changed: tuple[dict[str, Any], ...]
    unchanged: int
    factor_changed: tuple[str, ...]

    @property
    def n_changed(self) -> int:
        return len(self.changed)


def diff_prices(
    fetched: Sequence[dict[str, Any]], existing: dict[tuple[str, date], dict[str, Any]]
) -> PriceDiff:
    """抓回整窗之后，**只 upsert 与库里真正不同的行**（§7.3.1）。

    正常日子只有今天那一行是新的，其余 399 行原样不变 → 写入量从 400 行降到 1 行；
    除息那天则有几百行确实变了，它们会被正确写入。
    供应商侧成本不变，数据库侧成本降两个数量级，而正确性保证一点没丢
    —— 因为整窗**抓取**照旧，变的只是**写入**。

    同时报告 ``adj_factor`` 变化（§3.0 规则 2 的探测器）。
    **注意这只是诊断，不是正确性机制**：正确性来自「每天重写整窗」，
    与探测器是否工作无关。Stooq 行的 ``close`` 是 NULL → ``adj_factor``
    为 NULL，**不得把「NULL 比 NULL」当成「没变」**，所以用 ``adj_close`` 兜底。
    """
    changed: list[dict[str, Any]] = []
    unchanged = 0
    factor_changed: set[str] = set()

    for row in fetched:
        key = (str(row["symbol"]), row["date"])
        old = existing.get(key)
        if old is None:
            changed.append(row)
            continue
        if _same_price(row, old):
            unchanged += 1
            continue
        if _factor_moved(row, old):
            factor_changed.add(str(row["symbol"]))
        changed.append(row)

    return PriceDiff(
        changed=tuple(changed),
        unchanged=unchanged,
        factor_changed=tuple(sorted(factor_changed)),
    )


def _num(v: Any) -> float | None:
    """取数值。**不做舍入** —— 见 :data:`_EPS`。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN


#: 因子是两个价格的商；真实的复权因子变化（分红/拆股）至少是 1e-3 量级，
#: 所以 1e-6 的相对容差离得很远。
_FACTOR_EPS = 1e-6

#: ``numeric(14,4)`` 的一个最小刻度。**差小于它 = 存进去长得一样。**
_ONE_ULP = 10.0**-_PRICE_SCALE

#: 供应商自己的数值噪声（相对）。
#:
#: **实测：yfinance 的 ``Adj Close`` 只稳定到 float32 精度。**
#: 同一只股票、同一天、同一个窗口，两次调用分别给出
#: ``97.33495330810547`` 与 ``97.33494567871094`` —— 差 7.6e-6。
#: 这不是我们这边的问题，是它把 close × factor 算在 float32 上。
#:
#: 后果很具体：存储是 4 位小数，而真值只要落在某个 ``.00005`` 边界
#: 7.6e-6 以内，这点噪声就会把第 4 位小数翻过去。随机值落进那条缝的概率
#: 约 7.6%，而实测每跑都有 **577/6800 ≈ 8.5%** 的行「变了」且**不收敛**。
#:
#: 于是差分要的不是「位不位相等」，而是「**看起来会不会不一样**」：
#: 差不到一个存储刻度、或落在供应商自身噪声以内，就当没变。
#: 代价是一次真实的、小于 1e-4 的价格变动不会被改写 ——
#: 那已经低于我们存储与展示的精度，任何指标都看不出区别。
_VENDOR_REL_EPS = 2e-6


def _close_enough(a: float, b: float) -> bool:
    """两个价格在存储 + 供应商精度下是否不可分辨。见 :data:`_VENDOR_REL_EPS`。"""
    return abs(a - b) <= max(_ONE_ULP, _VENDOR_REL_EPS * max(abs(a), abs(b)))


def _same_price(new: dict[str, Any], old: dict[str, Any]) -> bool:
    for col in ("adj_close", "close"):
        a, b = _num(new.get(col)), _num(old.get(col))
        if a is None and b is None:
            continue
        if a is None or b is None or not _close_enough(a, b):
            return False
    return str(new.get("source")) == str(old.get("source"))


def _factor_moved(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """复权因子是否变了。

    两侧都有未复权 ``close`` 时比因子；只要有一侧是 Stooq（``close`` 为 NULL）
    就退回比 ``adj_close`` —— 「NULL 比 NULL」什么都比不出来，
    而那正是 §3.0 规则 2 警告过的「用备源期间探测器失明」。
    """
    nc, oc = _num(new.get("close")), _num(old.get("close"))
    na, oa = _num(new.get("adj_close")), _num(old.get("adj_close"))
    if na is None or oa is None:
        return False
    if nc is None or oc is None or nc == 0 or oc == 0:
        # 一侧是 Stooq（close 为 NULL）→ 退回比 adj_close 本身。
        # 「NULL 比 NULL」什么都比不出来，而那正是 §3.0 规则 2 警告过的
        # 「用备源期间探测器失明」。
        return not _close_enough(na, oa)
    # 因子是两个 4 位小数的商，所以它的可分辨精度比 4 位小数粗。
    # 用相对容差，否则低价股（除数小）会被放大成误报。
    return bool(abs((na / nc) - (oa / oc)) > _FACTOR_EPS)
