"""利率政策板块（docs/add-rate-policy.md，M16）。

**独立于价格管道**：这里的任何失败都动不了 ``run_daily`` 的状态码、重试或告警。
三个来源（纽约联储 / 财政部 / 美联储日程）互相独立，解析函数都是纯函数、不碰网络。
"""

from __future__ import annotations

from pipeline.macro.errors import MacroParseError

__all__ = ["MacroParseError"]
