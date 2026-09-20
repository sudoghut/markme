"""把整窗价格算成 ``metrics_daily`` 与 ``strength_daily`` 的行。

三条规则决定了这里的形状：

1. **§3.0 规则 2：三层一起重写。** 价格层、指标层、榜单层同一个窗口，
   不是只写当日一行。只重写价格层是无效的 —— 损害发生在指标层：
   老的 ``metrics_daily`` 行仍带着除息前的复权基准，而
   ``ema60_slope_20d`` 会拿一个新基准的 EMA 去比一个 20 天前旧基准的 EMA，
   **所有闸门全绿**。
2. **§3.3 的双闸门**：``min_bars`` 以下一律写 NULL（硬闸门，不出值）；
   ``provisional_below`` 以下出值但标 ``provisional``（软闸门，前端灰标）。
   两者混用是 §3.3 专门写出来防的那件事。
3. **§3.5(3)：事件列只写最新一行，其余行显式写 NULL。**
   不是「不管它」—— §7.3.1 说得很清楚：非最新行的八个事件列要被
   **显式写成 NULL**，否则一次口径变更会在历史行里留下永久的残值。

``ordinals`` 一路传下去，是因为 ``mom_20`` 与 ``ema60_slope_20d`` 的
「20 个 session 前」必须按**交易日**数，而不是按行号 ——
价格表里少一行（停牌、日历修订）就会让行号和交易日错位。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from pipeline.metrics.alpha_beta import alpha_beta, session_returns
from pipeline.metrics.ema import close_vs_ema_pct, ema, ema_slope
from pipeline.metrics.momentum import momentum
from pipeline.metrics.rsi import rsi_wilder
from pipeline.metrics.strength import rank_pool

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from collections.abc import Mapping, Sequence

    from pipeline.config import Config
    from pipeline.metrics.events import EventDistances

__all__ = [
    "EVENT_COLUMNS",
    "SymbolWindow",
    "build_windows",
    "compute_metrics",
    "compute_strength",
]

#: §3.5(3) 的八列。**非最新行必须显式写 NULL。**
EVENT_COLUMNS = (
    "days_to_next_earnings",
    "days_since_last_earnings",
    "days_to_next_dividend",
    "days_since_last_dividend",
    "next_earnings_date",
    "next_dividend_date",
    "next_earnings_is_estimated",
    "next_dividend_is_estimated",
)


@dataclass(frozen=True, slots=True)
class SymbolWindow:
    """一个标的在窗口内的价格序列，已按 session 对齐。"""

    symbol: str
    dates: tuple[date, ...]
    ordinals: tuple[int, ...]
    adj_close: pd.Series

    @property
    def n_bars(self) -> int:
        return len(self.dates)


def build_windows(
    prices: pd.DataFrame, ordinal_by_date: Mapping[date, int]
) -> dict[str, SymbolWindow]:
    """tidy 价格表 → 每标的一个窗口。

    **只保留在 ``trading_sessions`` 里的日期**（§9.1.4 第 4 条）——
    即便有陈旧行残留在价格表里，它也进不了计算窗口。
    """
    out: dict[str, SymbolWindow] = {}
    if prices.empty:
        return out
    for sym, g in prices.groupby("symbol", sort=True):
        g = g[g["date"].isin(ordinal_by_date)].sort_values("date")
        if g.empty:
            continue
        dates = tuple(g["date"])
        out[str(sym)] = SymbolWindow(
            symbol=str(sym),
            dates=dates,
            ordinals=tuple(ordinal_by_date[d] for d in dates),
            adj_close=pd.Series(
                pd.to_numeric(g["adj_close"], errors="coerce").to_numpy(), index=range(len(dates))
            ),
        )
    return out


def _gate(series: pd.Series, min_bars: int) -> pd.Series:
    """§3.3 的**硬**闸门：前 ``min_bars - 1`` 根一律 NULL，不出值。"""
    out = series.copy()
    if min_bars > 1:
        out.iloc[: min_bars - 1] = np.nan
    return out


def compute_metrics(
    cfg: Config,
    windows: Mapping[str, SymbolWindow],
    *,
    event_distances: Mapping[str, EventDistances] | None = None,
    latest_date: date | None = None,
) -> list[dict[str, Any]]:
    """整窗的 ``metrics_daily`` 行。

    ``latest_date`` 是「最新一个 session」—— 只有它那一行带事件列（§3.5(3)）。
    其余行的八个事件列显式写 ``None``，于是一次删+插之后不会留下残值。
    """
    bench_sym = cfg.universe.benchmark
    bench = windows.get(bench_sym)
    bench_returns = (
        session_returns(bench.adj_close, pd.Series(bench.ordinals))
        if bench
        else pd.Series(dtype="float64")
    )

    by_id = {m.id: m for m in cfg.metrics.metrics}
    rsi_cfg, ema_cfg = by_id.get("rsi_14"), by_id.get("ema_60")
    ab_cfg, mom_cfg = by_id.get("alpha_beta_126"), by_id.get("mom_20")

    rows: list[dict[str, Any]] = []
    for sym, w in windows.items():
        adj = w.adj_close

        rsi = _gate(rsi_wilder(adj, period=_p(rsi_cfg, "period", 14)), _mb(rsi_cfg, 15))
        ema_v = _gate(ema(adj, period=_p(ema_cfg, "period", 60)), _mb(ema_cfg, 60))
        vs_ema = close_vs_ema_pct(adj, ema_v)
        slope = ema_slope(ema_v, lag=20)
        mom = _gate(momentum(adj, period=_p(mom_cfg, "period", 20)), _mb(mom_cfg, 21))

        ab = _alpha_beta_for(w, bench_returns, ab_cfg, same=sym == bench_sym)
        prov_all = _provisional(w.n_bars, by_id)

        for i, d in enumerate(w.dates):
            row: dict[str, Any] = {
                "symbol": sym,
                "date": d,
                "rsi_14": _at(rsi, i),
                "ema_60": _at(ema_v, i),
                "close_vs_ema60_pct": _at(vs_ema, i),
                "ema60_slope_20d": _at(slope, i),
                "mom_20": _at(mom, i),
                "alpha_annual": None,
                "beta": None,
                "r2": None,
                "corr": None,
                "resid_vol_annual": None,
                "alpha_t_stat": None,
                "n_obs": None,
                # **这两列是 `not null default '{}'`。**
                # 显式写 None 会撞 NOT NULL —— 而「有默认值」只在
                # *不提供该列* 时生效，提供了 NULL 就是 NULL。
                # 实测：第一次真实回填就栽在这里，而它在单元测试里看不出来。
                "extra": {},
                "provisional_metrics": prov_all,
            }
            # alpha/beta 是窗口末端的一个标量（§3.4），只落在最后一行。
            if ab is not None and i == len(w.dates) - 1:
                row |= {
                    "alpha_annual": ab.alpha_annual,
                    "beta": ab.beta,
                    "r2": ab.r2,
                    "corr": ab.corr,
                    "resid_vol_annual": ab.resid_vol_annual,
                    "alpha_t_stat": ab.alpha_t_stat,
                    "n_obs": ab.n_obs,
                }
            # §3.5(3)：事件列只在最新一行，其余**显式** NULL。
            ed = (event_distances or {}).get(sym) if d == latest_date else None
            for col in EVENT_COLUMNS:
                row[col] = getattr(ed, col, None) if ed is not None else None
            rows.append(row)
    return rows


def _p(metric: Any, key: str, default: int) -> int:
    if metric is None:
        return default
    return int(metric.params.get(key, default))


def _mb(metric: Any, default: int) -> int:
    return default if metric is None else int(metric.min_bars)


def _at(series: pd.Series, i: int) -> float | None:
    if i >= len(series):
        return None
    v = series.iloc[i]
    return None if pd.isna(v) else float(v)


def _alpha_beta_for(w: SymbolWindow, bench_returns: pd.Series, ab_cfg: Any, *, same: bool) -> Any:
    if bench_returns.empty and not same:
        return None
    rets = session_returns(w.adj_close, pd.Series(w.ordinals))
    other = rets if same else bench_returns
    n = min(len(rets), len(other))
    if n == 0:
        return None
    params = dict(ab_cfg.params) if ab_cfg is not None else {}
    return alpha_beta(
        rets.iloc[-n:].reset_index(drop=True),
        other.iloc[-n:].reset_index(drop=True),
        window=int(params.get("window", 126)),
        min_obs=int(params.get("min_obs", 120)),
        annualization=str(params.get("annualization", "linear")),
        risk_free_daily=_risk_free_daily(params.get("risk_free")),
        # QQQ 对自己：β=1、α=0、R²=1（§11 M2 的验收标准之一）。
        # 交给 M2 显式处理，而不是指望浮点回归恰好给出那三个数。
        is_benchmark=same,
    )


def _risk_free_daily(spec: Any) -> float:
    """``risk_free`` 在 config 里是一个块，不是一个数（§12 #4）。

    ``{mode: constant, annual: 0.0, units: percent}`` —— 默认 rf = 0，
    留了接 ``^IRX`` 的开关。这里只实现 ``constant``：把年化按 252 换成日频，
    并按 ``units`` 决定要不要除以 100。
    **把 0.5 当成 0.5% 还是 50% 是一个不会报错的错误**，所以单位写死在 config 里。
    """
    if spec is None:
        return 0.0
    if isinstance(spec, int | float):
        return float(spec) / 252.0
    if not isinstance(spec, dict):
        return 0.0
    annual = float(spec.get("annual", 0.0))
    if str(spec.get("units", "percent")) == "percent":
        annual /= 100.0
    return annual / 252.0


def _provisional(n_bars: int, by_id: Mapping[str, Any]) -> list[str]:
    """§3.3 的**软**闸门：出值但标 provisional。

    与 ``min_bars`` 不是一回事 —— 一个说「出不出值」，一个说「信不信得过」。
    §3.3 写这条就是为了防止这两者被混用。
    """
    # provisional_below 可以是 None（事件类指标没有「喂入量」这回事）——
    # 那种指标不参与软闸门。
    flagged = [
        m.id
        for m in by_id.values()
        if m.provisional_below is not None and n_bars < int(m.provisional_below)
    ]
    return sorted(flagged)


def compute_strength(
    cfg: Config,
    metrics_rows: Sequence[dict[str, Any]],
    *,
    day: date,
    pool: Mapping[str, str],
) -> list[dict[str, Any]]:
    """当日的 ``strength_daily`` 行 —— **全部 16 名**，前端只显示前 3（§4.4）。

    排名池由 ``strength.yaml`` 的 ``rank_pool`` 决定；``pool`` 给的是
    每个标的的 ``type``，于是「哪些进池」这个决定留在 config 里。
    """
    st = cfg.strength
    want_all = st.rank_pool == "all"
    scores: dict[str, float | None] = {}
    for r in metrics_rows:
        if r["date"] != day:
            continue
        sym = str(r["symbol"])
        if not want_all and pool.get(sym) != "stock":
            continue
        scores[sym] = r.get(st.score_metric)

    if not scores:
        return []
    ranked = rank_pool(scores, top_n=st.top_n)
    return [
        {
            "date": day,
            "symbol": r.symbol,
            "rank": r.rank,
            "score": r.score,
            "score_metric": st.score_metric,
            "rank_pool": st.rank_pool,
            "benchmark": cfg.universe.benchmark,
            "top_n": st.top_n,
            "in_top_n": r.in_top_n,
            "delta_to_next": r.delta_to_next,
            "delta_to_median": r.delta_to_median,
        }
        for r in ranked
    ]
