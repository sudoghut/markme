"""EMA(60) 及其派生列（§3.2）。

播种用 ``SMA(adjC[0:period])``，落在下标 ``period-1`` 的那根 bar 上。

派生列 ``close_vs_ema60_pct`` 的分子**必须是 ``adj_close``**（§3.0 规则 1）：
两列混用会让历史行读出荒谬的百分比，而全程不报任何错。
yfinance 的原始 `Close` 已做拆股调整（M0 实测确认），所以两列真正的差是
**累计分红调整** —— 量级小、不扎眼，因此比 10× 断崖更危险。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.metrics.registry import register

__all__ = ["close_vs_ema_pct", "ema", "ema_slope"]


@register("ema")
def ema(adj_close: pd.Series, *, period: int = 60) -> pd.Series:
    """EMA。返回与输入等长、索引相同的序列，预热段为 ``NaN``。"""
    if period < 1:
        raise ValueError(f"period 必须 >= 1，得到 {period}")

    values = adj_close.to_numpy(dtype=float)
    n = values.size
    out = np.full(n, np.nan)
    if n < period:
        return pd.Series(out, index=adj_close.index, name=f"ema_{period}")

    alpha = 2.0 / (period + 1.0)
    prev = float(np.mean(values[:period]))  # seed = SMA，落在下标 period-1
    out[period - 1] = prev
    for i in range(period, n):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return pd.Series(out, index=adj_close.index, name=f"ema_{period}")


def close_vs_ema_pct(adj_close: pd.Series, ema_values: pd.Series) -> pd.Series:
    """``adj_close / ema - 1``。**这是这一列的权威实现**（config 里的 expr 只是文档）。

    分母为 0 时给 ``NaN`` 而不是 ``inf`` —— 一个无穷大的百分比在前端会被
    ``dim_when`` 判成 fail-closed（打灰），而 ``inf`` 本身没有任何可读含义。
    """
    if not adj_close.index.equals(ema_values.index):
        # 索引不一致时 pandas 会对齐成一条更长的、全是 NaN 的序列 ——
        # 一个安静的空结果比一个响亮的错误难查得多。
        raise ValueError("adj_close 与 ema 必须同索引")
    denom = ema_values.replace(0.0, np.nan)
    return (adj_close / denom - 1.0).rename("close_vs_ema60_pct")


def ema_slope(ema_values: pd.Series, *, lag: int = 20) -> pd.Series:
    """``ema_t / ema_{t-lag} - 1``：中期趋势方向。"""
    if lag < 1:
        raise ValueError(f"lag 必须 >= 1，得到 {lag}")
    prev = ema_values.shift(lag).replace(0.0, np.nan)
    return (ema_values / prev - 1.0).rename(f"ema60_slope_{lag}d")
