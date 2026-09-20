"""RSI(14) — Wilder 平滑（§3.1）。

两个必须写死的口径：

1. **显式递归，不用 ``ewm``。** ``ewm(span=14)`` 是 ``alpha = 2/15``，不是 Wilder 的
   ``1/14``；而 ``ewm(alpha=1/14, adjust=False)`` 用**首值播种**，与下面的 SMA(14)
   播种不是同一个函数。差异按 ``(13/14)^k`` 衰减，生产中可忽略，
   但一条对标手算值的 golden 测试**一定会失败**，且失败看起来像公式错而非播种错。
2. **``delta_0`` 不存在**，所以第一个 gain 是 ``gain_1``，
   **第一个 RSI 落在第 15 根收盘价上**。这是每次 RSI 重实现都会犯的 off-by-one。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.metrics.registry import register

__all__ = ["rsi_wilder"]


@register("rsi_wilder")
def rsi_wilder(adj_close: pd.Series, *, period: int = 14) -> pd.Series:
    """Wilder RSI。返回与输入等长、索引相同的序列，预热段为 ``NaN``。"""
    if period < 1:
        raise ValueError(f"period 必须 >= 1，得到 {period}")

    values = adj_close.to_numpy(dtype=float)
    n = values.size
    out = np.full(n, np.nan)
    # 需要 period + 1 根收盘价才有第一个 RSI（delta_0 不存在）。
    if n < period + 1:
        return pd.Series(out, index=adj_close.index, name="rsi_14")

    delta = np.diff(values)  # delta[i] 对应 values[i+1]
    # **NaN 必须当 NaN 传下去，不能变成 gain=0 / loss=0。**
    # `NaN > 0` 与 `NaN < 0` 都是 False，直接写 np.where 会把一个缺失价格
    # 伪造成一个「平盘日」—— 恰好是本模块自己的 docstring 与 §3.1
    # 明令禁止的「用 0 冒充」。而 Wilder 是 IIR 递归，**污染永不消散**：
    # 单一个缺失日会把其后每一天的 RSI 都拉向那一次缺失的方向。
    # 这不是假想：§7.3.1 的批量 download 在某标的缺一天时就返回 NaN。
    nan_mask = np.isnan(delta)
    gain = np.where(nan_mask, np.nan, np.where(delta > 0, delta, 0.0))
    loss = np.where(nan_mask, np.nan, np.where(delta < 0, -delta, 0.0))

    # 播种：SMA(gain_1..gain_period)，落在下标 period 的那根 bar 上。
    avg_gain = float(np.mean(gain[:period]))
    avg_loss = float(np.mean(loss[:period]))
    out[period] = _rsi_from(avg_gain, avg_loss)

    # 递归：AvgGain_t = (AvgGain_{t-1} * (period-1) + gain_t) / period
    w = period - 1
    for i in range(period, delta.size):
        avg_gain = (avg_gain * w + gain[i]) / period
        avg_loss = (avg_loss * w + loss[i]) / period
        out[i + 1] = _rsi_from(avg_gain, avg_loss)

    return pd.Series(out, index=adj_close.index, name="rsi_14")


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    """由平均涨跌幅算 RSI，并处理三种退化情形（§3.1）。"""
    if np.isnan(avg_gain) or np.isnan(avg_loss):
        return float("nan")
    if avg_loss == 0.0:
        # 窗口内连续平盘 / 停牌：两者皆 0 → **写 NaN，不写 50**。
        # 一个合成的 50 是在「没有信息」的地方画一个中性且自信的数字，
        # 与 §10.4「绝不用 0 或上一日的值冒充」是同一类错误。
        return float("nan") if avg_gain == 0.0 else 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
