"""收盘-收盘动量（§4.1）。

``mom_20 = adjC_t / adjC_{t-period} - 1``，``min_bars = period + 1``。

这是三强股的主排序分。与 low-buy 已注册的 ``Mom_20_prev`` 跨越**相同的
20 个交易日**，只差一天偏移 —— 由此有 §1.3 那条可证伪命题：
markme 第 t 日的榜单应当等于 low-buy 第 t+1 日的榜单。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.metrics.registry import register

__all__ = ["momentum"]


@register("momentum")
def momentum(adj_close: pd.Series, *, period: int = 20) -> pd.Series:
    if period < 1:
        raise ValueError(f"period 必须 >= 1，得到 {period}")
    prev = adj_close.shift(period).replace(0.0, np.nan)
    return (adj_close / prev - 1.0).rename(f"mom_{period}")
