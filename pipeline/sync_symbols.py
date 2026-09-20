"""``config/universe.yaml`` → ``symbols`` 表（§6.1.1 的那个例外）。

**删除不是真删。** ``prices_daily.symbol`` 对 ``symbols`` 有外键，
``delete from symbols`` 会被挡住；而且真删会让回补与历史榜单都断。
移出 config 的标的一律 ``enabled = false`` 软删除，历史数据保留。

``expects_earnings`` 按 ``type`` 推断（§3.5(5)）：ETF 永远没有财报，
而那条新鲜度不变式若不排除它们，**从第一天起就是红的**。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from pipeline.config import Config

__all__ = ["symbol_rows"]


def symbol_rows(cfg: Config) -> list[dict[str, Any]]:
    """config 里的标的 → ``symbols`` 的行。

    ``is_benchmark`` 与 ``enabled`` 都从 config 推，**不从库里读** ——
    这张表是 config 的投影，不是第二个事实来源（§6.1.1）。
    """
    bench = cfg.universe.benchmark
    return [
        {
            "symbol": s.symbol,
            "name": s.name,
            "type": s.type,
            "is_benchmark": s.symbol == bench,
            "enabled": s.enabled,
            # ETF 永远没有财报（§3.5(5) 的三个必须容忍的情形之一）。
            "expects_earnings": s.type != "etf",
        }
        for s in cfg.universe.symbols
    ]
