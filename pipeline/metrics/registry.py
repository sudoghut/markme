"""指标注册表（§6.3）。

``config/metrics.yaml`` 按**名字**引用函数，这里负责把名字解析成实现。

**两种函数签名**，因为 §3.5 的事件距离逼出了第二种：

``kind="series"``
    纯价格派生：吃一条 ``adj_close`` 序列，吐一条等长序列。
    RSI / EMA / 动量都是这种。

``kind="pairwise"``
    吃**两条**序列（标的与基准的收益率），吐一组标量。alpha/beta 是唯一一个。
    它既不是「一进一出的序列」，也不读别的表 —— 塞在 ``series`` 里会让
    上面那句契约当场变成谎话。

``kind="table"``
    不碰价格，读另一张表（``symbol_events``）。事件距离是唯一一个。

**为什么要显式区分**：§3.6 那句「新增一个指标 = 写一个纯函数 + config 加几行，
不需要改 schema、不需要改前端」只对 ``series`` 成立。把两种签名混在一个
注册表里而不加标记，那句承诺就会在下一个人读到时变成谎话。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["MetricKind", "get_metric", "metric_kinds", "register", "registered_names"]

MetricKind = Literal["series", "pairwise", "table"]


@dataclass(frozen=True)
class _Entry:
    fn: Callable[..., Any]
    kind: MetricKind


_REGISTRY: dict[str, _Entry] = {}


def register(name: str, *, kind: MetricKind = "series") -> Callable[..., Any]:
    """把一个实现登记到 ``name`` 名下。重复登记直接报错 —— 静默覆盖是最难查的那种。"""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise ValueError(f"指标函数 {name!r} 已经登记过（{_REGISTRY[name].fn.__module__}）")
        _REGISTRY[name] = _Entry(fn=fn, kind=kind)
        return fn

    return deco


def get_metric(name: str) -> _Entry:
    entry = _REGISTRY.get(name)
    if entry is None:
        raise KeyError(f"指标函数 {name!r} 未登记；已登记的有 {sorted(_REGISTRY)}")
    return entry


def registered_names() -> frozenset[str]:
    return frozenset(_REGISTRY)


def metric_kinds() -> dict[str, MetricKind]:
    """名字 → 签名种类。

    （原名叫 ``METRIC_KINDS`` —— 一个穿着常量名字的函数。
    读的人会当它是 dict，然后拿到一个读起来像「键不存在」的 TypeError。）
    """
    return {name: e.kind for name, e in _REGISTRY.items()}
