"""注册表与 config 的一致性（§6.2 / §6.3）。

这组测试守的是同一件事：**`metrics.yaml` 里写的每个 `fn` 都真的有实现，
反过来每个实现也都被声明过。** 少了它，「配置驱动」在第一次改动时就会断。
"""

from __future__ import annotations

import pytest

import pipeline.metrics as metrics_pkg
from pipeline.config import PARAM_MODELS, load_config
from pipeline.metrics.registry import get_metric, metric_kinds, register, registered_names


def test_importing_the_package_fills_the_registry() -> None:
    """只 import 本包就该填满注册表。

    否则 ``get_metric("rsi_wilder")`` 会不会成功，取决于调用方有没有恰好
    import 过 ``pipeline.metrics.rsi`` —— 那是最难查的那种偶发失败。
    """
    assert "rsi_wilder" in metrics_pkg.registered_names()


def test_every_configured_fn_has_an_implementation() -> None:
    cfg = load_config()
    for m in cfg.metrics.metrics:
        assert m.fn in registered_names(), f"指标 {m.id!r} 的 fn {m.fn!r} 没有实现"


def test_every_implementation_is_declared_in_config() -> None:
    """反向：实现了却没人声明，就是一段没人调用的死代码。"""
    cfg = load_config()
    declared = {m.fn for m in cfg.metrics.metrics}
    assert registered_names() == declared


def test_param_models_and_registry_agree() -> None:
    """``PARAM_MODELS`` 与注册表必须是同一套 key。

    少一个 → 那个指标的 params 无人校验（§6.2 承诺的后门）；
    多一个 → 一个校验着并不存在的指标的模型。
    """
    assert set(PARAM_MODELS) == set(registered_names())


def test_every_metric_declares_the_right_signature_kind() -> None:
    """§3.6 那句「新增指标不需要改 schema、不需要改前端」只对 series 成立。

    把不同签名混在一起而不加标记，那句承诺会在下一个人读到时变成谎话。
    注意有**三种**而不是两种：``alpha_beta`` 吃两条序列、吐一组标量，
    它既不是「一进一出的序列」也不读别的表。
    """
    expected = {
        "rsi_wilder": "series",
        "ema": "series",
        "momentum": "series",
        "alpha_beta": "pairwise",
        "event_distances": "table",
    }
    assert metric_kinds() == expected


def test_duplicate_registration_is_loud() -> None:
    """静默覆盖是最难查的那种。"""
    with pytest.raises(ValueError, match="已经登记过"):
        register("rsi_wilder")(lambda: None)


def test_unknown_metric_lists_the_known_ones() -> None:
    with pytest.raises(KeyError, match="未登记"):
        get_metric("no_such_metric")
