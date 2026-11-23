"""解析失败。

**宁可拒收，不可猜。** 一个来源的响应只要有一处对不上预期，就整次拒收、
库里的旧数据原样保留 —— 拒收的代价是数据停在旧版本（页面会标陈旧），
误收的代价是播报一场不存在的会议、或一个差了 100 倍的利率。
"""

from __future__ import annotations

__all__ = ["MacroParseError"]


class MacroParseError(ValueError):
    """来源的响应不符合预期形状，整次拒收。"""
