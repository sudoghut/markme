"""供应商抽象 + **整窗**降级 + §7.2 闸门 4 的合理性断言。

三条规则决定了这个模块的全部形状，每一条都来自「否则会产出看起来正常的错数」：

1. **一个指标窗口内不得混用数据源**（§3.0 规则 3）。
   若 yfinance 某天只有一行限流、那一行降级到 Stooq，它会落进 60 根 EMA 窗口
   和 126 日收益窗口里，接缝两侧的日收益各自错一整个累计复权差，并污染其后
   126 个交易日的 beta / resid_vol / alpha t 值。
   → **降级时用备源重抓整个窗口，绝不单行拼接。** 这里没有「行级 fallback」这个函数。
2. **两个源写入哪一列必须写死**（§3.0 规则 3 的表）：

   ======  ===========================  ==============================
   源       ``close``                    ``adj_close``
   ======  ===========================  ==============================
   yfinance 供应商 ``Close``（未复权）    供应商 ``Adj Close``
   Stooq    **``NULL``**                 供应商 ``close``（已复权）
   ======  ===========================  ==============================

   图省事把 Stooq 的价格同时写进两列，会同时坏三件事：「最新价」对每只分红股
   悄悄变成复权价；生成列 ``adj_factor`` 恒等于 1.0 让复权因子探测器失明；
   §3.0 规则 4 那条 CI 不变式变成「测哪个源跑了」。
3. **``auto_adjust=False`` 必须显式传**（§3.0 规则 4）。新版 yfinance 默认 True，
   此时 ``Close`` 就是复权价且**没有** ``Adj Close`` 列。

实测过的三件事（写适配器之前必须知道，见 M4 的 review 记录）：

- 多 ticker + ``group_by="column"`` → 两级列 ``(field, ticker)``；
  **单元素列表也是两级**，所以不必为「只抓一个」特殊处理。
- 一个抓不到的 ticker **不抛异常**，它只是返回一整块 NaN 并往 stderr 打一行。
  → 「整块 NaN」必须被当成**失败**，不是数据。这是这里最容易漏的一条：
  不认它的话，NaN 会一路走到写库，而 ``adj_close`` 是 NOT NULL，
  于是当天整批写入失败 —— 原因却一点都不明显。
- 索引是 tz-naive 的 ``datetime64``，列名 ``Date``。
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd

from pipeline.throttle import USER_AGENT, BudgetExceeded, RequestBudget, RetryAfterTooLong

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from pipeline.sessions import Session

__all__ = [
    "PRICE_COLUMNS",
    "FetchOutcome",
    "SanityIssue",
    "check_sanity",
    "cross_source_gap",
    "fetch_window",
    "restrict_to_sessions",
    "stooq_frame",
    "yfinance_frame",
]

Source = Literal["yfinance", "stooq"]

#: 归一化之后的列。两个源都产出这一组，差别只在 ``close`` 是否为 NULL。
PRICE_COLUMNS = (
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

#: §7.2 闸门 4：日间跳变上限。超过它且当天没有公司行动 → 该标的 partial。
_MAX_DAILY_MOVE = 0.5


# ---------------------------------------------------------------------------
# yfinance
# ---------------------------------------------------------------------------
def yfinance_frame(
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    download: object = None,
) -> pd.DataFrame:
    """一次批量请求拿回全部标的的整窗 OHLCV，归一化成 tidy 表。

    §7.3.1：**优先批量接口**。17 个标的一次请求拿完，胜过 17 次 ——
    减少请求数是最有效的礼貌。``threads=False`` 同理：不开并发。

    ``end`` 按**闭区间**语义传入，内部 +1 天，因为 yfinance 的 ``end`` 是开区间。
    这个 off-by-one 会静默丢掉最后一天 —— 也就是**今天那一行**，
    而闸门 3「每个标的的最新 bar 日期 == 当日 session」会把它报成 stale_vendor。
    """
    if not symbols:
        return _empty_frame()

    fn = download or _yf_download
    raw = fn(  # type: ignore[operator]
        list(symbols),
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,  # §3.0 规则 4 —— 不传就没有 Adj Close 列
        progress=False,
        group_by="column",
        threads=False,  # §7.3.1：串行，不并发
    )
    return _tidy_yfinance(raw, symbols)


def _yf_download(*args: object, **kwargs: object) -> pd.DataFrame:
    import yfinance as yf

    return yf.download(*args, **kwargs)  # type: ignore[no-any-return]


def _tidy_yfinance(raw: pd.DataFrame, symbols: Sequence[str]) -> pd.DataFrame:
    """两级列 ``(field, ticker)`` → tidy。**整块 NaN 的标的被丢掉。**

    yfinance 对一个抓不到的 ticker 不抛异常，只返回一整块 NaN。
    把它当数据写下去，``adj_close`` 的 NOT NULL 会让当天整批失败；
    而调用方需要知道的是「这个标的没拿到」，好去走整窗降级。
    """
    if raw is None or raw.empty:
        return _empty_frame()

    frames: list[pd.DataFrame] = []
    for sym in symbols:
        try:
            block = raw.xs(sym, axis=1, level=1) if raw.columns.nlevels == 2 else raw
            block = pd.DataFrame(block)
        except KeyError:
            continue
        if block.empty or block.isna().all().all():
            continue  # 整块 NaN = 没拿到，不是数据
        out = pd.DataFrame(
            {
                "symbol": sym,
                "date": pd.to_datetime(block.index).date,
                "open": _col(block, "Open"),
                "high": _col(block, "High"),
                "low": _col(block, "Low"),
                "close": _col(block, "Close"),
                "adj_close": _col(block, "Adj Close"),
                "volume": _col(block, "Volume"),
                "source": "yfinance",
            }
        )
        # adj_close 是 NOT NULL，缺它的行没有任何用处（所有指标都用它）。
        frames.append(out[out["adj_close"].notna()])

    if not frames:
        return _empty_frame()
    return (
        pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)
    )


def _col(block: pd.DataFrame, name: str) -> pd.Series:
    if name not in block.columns:
        return pd.Series(np.nan, index=range(len(block)), dtype="float64")
    return pd.Series(pd.to_numeric(block[name], errors="coerce")).reset_index(drop=True)


def _opt_col(raw: pd.DataFrame, name: str) -> pd.Series:
    """Stooq 的 CSV 未必有 Open/High/Low/Volume —— 缺了就是一列 NaN。"""
    if name not in raw.columns:
        return pd.Series(np.nan, index=range(len(raw)), dtype="float64")
    return pd.Series(pd.to_numeric(raw[name], errors="coerce")).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Stooq（备源）
# ---------------------------------------------------------------------------
def stooq_frame(
    symbol: str,
    start: date,
    end: date,
    *,
    fetch_csv: object = None,
) -> pd.DataFrame:
    """Stooq 的整窗日线。**``close`` 一律写 NULL**（§3.0 规则 3）。

    Stooq 给的价格本身就是已复权的，且没有独立的复权列。把它同时写进两列
    会让「最新价」悄悄变成复权价，并让 ``adj_factor`` 恒等于 1.0 ——
    于是复权因子探测器在换源时误报、在用备源期间失明。
    """
    getter = fetch_csv or _stooq_csv
    text = getter(symbol, start, end)  # type: ignore[operator]
    if not text or not text.strip():
        return _empty_frame()
    raw = pd.read_csv(io.StringIO(text))
    if raw.empty or "Close" not in raw.columns:
        return _empty_frame()

    out = pd.DataFrame(
        {
            "symbol": symbol,
            "date": pd.Series(pd.to_datetime(raw["Date"])).dt.date.reset_index(drop=True),
            "open": _opt_col(raw, "Open"),
            "high": _opt_col(raw, "High"),
            "low": _opt_col(raw, "Low"),
            "close": None,  # ← 写死，见 docstring
            "adj_close": _opt_col(raw, "Close"),
            "volume": _opt_col(raw, "Volume"),
            "source": "stooq",
        }
    )
    out = out[out["adj_close"].notna()]
    mask = (out["date"] >= start) & (out["date"] <= end)
    return out[mask].sort_values("date").reset_index(drop=True)


def _stooq_csv(symbol: str, start: date, end: date) -> str:
    """Stooq 的日线 CSV。美股代码要加 ``.us`` 后缀且小写。"""
    url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&d1={start:%Y%m%d}&d2={end:%Y%m%d}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return str(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Stooq HTTP {exc.code} for {symbol}") from exc


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in PRICE_COLUMNS})


# ---------------------------------------------------------------------------
# §7.2 闸门 4：数据合理性
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SanityIssue:
    symbol: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.symbol}: {self.kind} — {self.detail}"


def check_sanity(
    df: pd.DataFrame,
    *,
    corporate_action_dates: dict[str, set[date]] | None = None,
) -> list[SanityIssue]:
    """供应商脏数据闸门。全是 O(1) 的断言，没有理由不加。

    一个 MU 的 0.01 坏收盘会给出 ``mom_20 ≈ -99.9%``、RSI 钉在 0，
    并且因为会进入 126 日窗口，**在上游修正之后仍继续污染统计半年**。

    **断言必须按源分支。** Stooq 行的 ``close`` 是 NULL（§3.0 规则 3），
    拿 NULL 去比大小或做除法，会让这条**设计好的降级路径变成永久 partial**。
    """
    issues: list[SanityIssue] = []
    if df.empty:
        return issues
    actions = corporate_action_dates or {}

    for sym, g in df.groupby("symbol", sort=True):
        sym = str(sym)
        g = g.sort_values("date").reset_index(drop=True)
        src = str(g["source"].iloc[0])

        issues += _check_positive(sym, g)
        issues += _check_ohlc(sym, g, src)
        issues += _check_jumps(sym, g, src, actions.get(sym, set()))
    return issues


def _check_positive(sym: str, g: pd.DataFrame) -> list[SanityIssue]:
    out: list[SanityIssue] = []
    bad = g[~(pd.to_numeric(g["adj_close"], errors="coerce") > 0)]
    if not bad.empty:
        out.append(SanityIssue(sym, "adj_close<=0", f"{len(bad)} 行，首个 {bad['date'].iloc[0]}"))
    vol = pd.to_numeric(g["volume"], errors="coerce")
    neg = g[vol.notna() & (vol < 0)]
    if not neg.empty:
        out.append(SanityIssue(sym, "volume<0", f"{len(neg)} 行，首个 {neg['date'].iloc[0]}"))
    return out


def _check_ohlc(sym: str, g: pd.DataFrame, src: str) -> list[SanityIssue]:
    """OHLC 一致性。**按源分支** —— Stooq 没有未复权 ``close``。"""
    lo = pd.to_numeric(g["low"], errors="coerce")
    hi = pd.to_numeric(g["high"], errors="coerce")
    op = pd.to_numeric(g["open"], errors="coerce")
    ref = pd.to_numeric(g["close" if src == "yfinance" else "adj_close"], errors="coerce")

    usable = lo.notna() & hi.notna() & op.notna() & ref.notna()
    bad = usable & ~((lo <= op) & (op <= hi) & (lo <= ref) & (ref <= hi))
    if bool(bad.any()):
        first = g.loc[bad, "date"].iloc[0]
        return [SanityIssue(sym, "ohlc 不一致", f"{int(bad.sum())} 行，首个 {first}（源 {src}）")]
    return []


def _check_jumps(sym: str, g: pd.DataFrame, src: str, action_dates: set[date]) -> list[SanityIssue]:
    """日间跳变。拆股日的**原始**价必然跳变，所以检测到公司行动就放行。"""
    col = "close" if src == "yfinance" else "adj_close"
    series = pd.to_numeric(g[col], errors="coerce")
    if series.notna().sum() < 2:
        return []
    ratio = series / series.shift(1) - 1
    over = ratio.abs() > _MAX_DAILY_MOVE
    flagged = [d for d, hit in zip(g["date"], over, strict=True) if bool(hit)]
    flagged = [d for d in flagged if d not in action_dates]
    if flagged:
        return [
            SanityIssue(
                sym,
                "日间跳变超限",
                f"{len(flagged)} 行，首个 {flagged[0]}（|Δ| > {_MAX_DAILY_MOVE:.0%}，源 {src}）",
            )
        ]
    return []


def cross_source_gap(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """两个源都有该 bar 时的收盘价一致性比对（§7.2 闸门 4 末条）。

    §3.0 规则 3 的「整窗降级」让这个比对免费可得：降级时本来就拿到了
    两份整窗数据。比的是 ``adj_close`` —— Stooq 侧只有它。
    """
    if a.empty or b.empty:
        return pd.DataFrame(columns=["symbol", "date", "a", "b", "rel_diff"])
    merged = a.merge(b, on=["symbol", "date"], suffixes=("_a", "_b"))
    if merged.empty:
        return pd.DataFrame(columns=["symbol", "date", "a", "b", "rel_diff"])
    x = pd.to_numeric(merged["adj_close_a"], errors="coerce")
    y = pd.to_numeric(merged["adj_close_b"], errors="coerce")
    merged["rel_diff"] = (x / y - 1).abs()
    return merged[["symbol", "date", "adj_close_a", "adj_close_b", "rel_diff"]].rename(
        columns={"adj_close_a": "a", "adj_close_b": "b"}
    )


# ---------------------------------------------------------------------------
# 整窗抓取 + 降级
# ---------------------------------------------------------------------------
@dataclass
class FetchOutcome:
    """一次整窗抓取的结果。

    ``per_symbol_source`` 让「一个窗口内源必须唯一」成为**可断言的事实**
    而不是口头约定：每个标的在这里只有一个源，因为降级是整窗替换的。
    """

    frame: pd.DataFrame
    per_symbol_source: dict[str, Source] = field(default_factory=dict)
    degraded: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    issues: tuple[SanityIssue, ...] = ()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.per_symbol_source))

    def sources_are_unique_per_symbol(self) -> bool:
        """§3.0 规则 3 的运行时断言：任一标的的窗口内 ``source`` 唯一。"""
        if self.frame.empty:
            return True
        per = self.frame.groupby("symbol")["source"].nunique()
        return bool((per <= 1).all())


def fetch_window(
    symbols: Sequence[str],
    start: date,
    end: date,
    budget: RequestBudget,
    *,
    yf_frame: object = None,
    stooq: object = None,
    corporate_action_dates: dict[str, set[date]] | None = None,
) -> FetchOutcome:
    """抓整个窗口；拿不到的标的**用备源重抓整个窗口**。

    **这里没有行级 fallback，那是故意的**（§3.0 规则 3）：
    单行拼接会让接缝两侧的日收益各自错一整个累计复权差，
    并污染其后 126 个交易日的 beta / resid_vol / alpha t 值。
    """
    yfn = yf_frame or yfinance_frame
    stq = stooq or stooq_frame

    primary = budget.request(
        lambda: yfn(symbols, start, end),  # type: ignore[operator]
        what=f"yfinance 批量 {len(symbols)} 标的",
    )
    got = set(primary["symbol"].unique()) if not primary.empty else set()

    frames = [primary] if not primary.empty else []
    per_source: dict[str, Source] = dict.fromkeys(got, "yfinance")
    degraded: list[str] = []
    missing: list[str] = []

    for sym in symbols:
        if sym in got:
            continue
        try:
            alt = budget.request(
                lambda s=sym: stq(s, start, end),  # type: ignore[misc,operator]
                what=f"Stooq 整窗 {sym}",
            )
        except (BudgetExceeded, RetryAfterTooLong):
            # **预算用尽 / 对方要求长时间等待，都必须向上冒泡。** 把它算成「这个标的缺了」，
            # 一次预算耗尽就会伪装成「16 个标的正常、1 个没抓到」的 partial ——
            # 而预算存在的全部理由就是防止那种「看起来完整」的部分结果。
            raise
        except Exception:
            # 备源自己失败（HTTP 5xx、CSV 坏了、重试耗尽）：这个标的确实缺了。
            missing.append(sym)
            continue
        if alt.empty:
            missing.append(sym)
            continue
        frames.append(alt)
        per_source[sym] = "stooq"
        degraded.append(sym)

    frame = (
        pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)
        if frames
        else _empty_frame()
    )
    issues = check_sanity(frame, corporate_action_dates=corporate_action_dates)
    return FetchOutcome(
        frame=frame,
        per_symbol_source=per_source,
        degraded=tuple(degraded),
        missing=tuple(missing),
        issues=tuple(issues),
    )


def restrict_to_sessions(df: pd.DataFrame, sessions: Sequence[Session]) -> pd.DataFrame:
    """§9.1.4 第 4 条：指标的输入必须显式按 ``trading_sessions`` 过滤。

    「``prices_daily`` 里有什么就用什么」是不够的 —— 即便有陈旧行残留在价格表里
    （日历修订之后的多余日期），它也进不了计算窗口。
    这条让「日历是唯一事实来源」成为**结构性保证**，而不是靠对账脚本跑对。
    """
    if df.empty:
        return df
    valid = {s.date for s in sessions}
    return df[df["date"].isin(valid)].reset_index(drop=True)
