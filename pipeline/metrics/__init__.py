"""指标计算。

每个指标是一个**纯函数**，由 :mod:`pipeline.metrics.registry` 按名字注册，
再由 ``config/metrics.yaml`` 按名字引用（§6.3）。

这里 import 全部实现模块，是为了让注册表在「只 import 本包」时就填满 ——
否则 ``get_metric("rsi_wilder")`` 会不会成功，取决于调用方有没有
恰好 import 过 ``pipeline.metrics.rsi``，那是最难查的那种偶发失败。
"""

from __future__ import annotations

from pipeline.metrics import alpha_beta, ema, events, momentum, rsi, strength  # noqa: F401
from pipeline.metrics.registry import get_metric, register, registered_names

__all__ = ["get_metric", "register", "registered_names"]
