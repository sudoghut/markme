"""配置层的「收紧」测试 —— 每一条都对应一个被 review 抓到的真实漏洞。

与 ``test_config.py`` 分开是因为侧重不同：那边覆盖 §11 的四条验收标准，
这边专门守住那些**会静默通过**的缺口。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from pipeline.config import CONFIG_DIR, Config, ConfigError, check_columns_match, load_config
from pipeline.dim_expr import parse_dim_when
from pipeline.tests.test_config import REAL_METRICS_COLUMNS


def _raw(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def _write_all(tmp: Path, **overrides: dict[str, Any]) -> Path:
    for name in ("universe.yaml", "metrics.yaml", "strength.yaml", "app.yaml"):
        data = overrides.get(name.removesuffix(".yaml"), _raw(name))
        with (tmp / name).open("w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    return tmp


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


# ---------------------------------------------------------------------------
# params 的类型化 —— _Strict「拼错就失败」唯一的破口，也是语义最重的地方
# ---------------------------------------------------------------------------
class TestParamsAreTyped:
    """§3.4 把后果写得很清楚：``units`` 写成 ``percentage`` → 漏掉 /100 →
    得到 2.08%/日 的无风险利率和一个灾难性的负 alpha，
    **看起来像市场崩盘，实际是单位 bug**。这种东西必须在加载期死。
    """

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("annualization",), "compounded"),  # 正确值是 compound
            (("min_obz",), 120),  # 拼错的键
            (("risk_free", "units"), "percentage"),  # 正确值是 percent
            (("risk_free", "mode"), "fixed"),  # 正确值是 constant
        ],
    )
    def test_alpha_beta_param_typos_are_rejected(
        self, tmp_path: Path, path: tuple[str, ...], value: object
    ) -> None:
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["fn"] == "alpha_beta")
        target: Any = ab["params"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="params 不合法"):
            load_config(tmp_path)

    def test_british_spelling_does_not_silently_coexist(self, tmp_path: Path) -> None:
        """``annualisation`` 与 ``annualization`` 并存时真正的键保持旧值 ——
        编辑者以为改了，其实没改。``extra=forbid`` 让这种「改了个寂寞」立刻失败。
        """
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["fn"] == "alpha_beta")
        ab["params"]["annualisation"] = "compound"
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="params 不合法"):
            load_config(tmp_path)

    def test_min_obs_above_window_is_rejected(self, tmp_path: Path) -> None:
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["fn"] == "alpha_beta")
        ab["params"]["min_obs"] = 999
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="永远达不到"):
            load_config(tmp_path)

    def test_unregistered_fn_is_rejected(self, tmp_path: Path) -> None:
        """没登记 params 模型的 fn 等于给 §6.2 的承诺开后门。"""
        metrics = _raw("metrics.yaml")
        metrics["metrics"][0]["fn"] = "not_registered"
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="没有登记 params 模型"):
            load_config(tmp_path)


class TestBenchmarkAnchorSurvivesRename:
    """跨文件 benchmark 检查必须锚在 ``fn`` 上，不能锚在指标 id 上。

    初版写的是 ``by_id("alpha_beta_126")`` 加一个 ``if ab is not None``，
    于是把指标改名成 ``alpha_beta_252``（§12 把窗口变更当作纯 config 改动，
    这是个现实的编辑）就会让这条检查**静默失效** ——
    α 列悄悄变成对 SPY 的，而全站标签仍写着对 QQQ 的。
    """

    def test_mismatch_is_caught_even_after_rename(self, tmp_path: Path) -> None:
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["fn"] == "alpha_beta")
        ab["id"] = "alpha_beta_252"  # 改名
        ab["params"]["window"] = 252
        ab["params"]["benchmark"] = "SPY"  # 同时改错 benchmark
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="各说各话"):
            load_config(tmp_path)

    def test_missing_alpha_beta_is_loud(self, tmp_path: Path) -> None:
        """缺失必须是响亮的 —— 少了它 alpha/beta 整列都没有来源。"""
        metrics = _raw("metrics.yaml")
        metrics["metrics"] = [m for m in metrics["metrics"] if m["fn"] != "alpha_beta"]
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="恰好一个"):
            load_config(tmp_path)


class TestSoftDelete:
    """§6.1 的软删除：移出 config 的标的置 ``enabled: false``，历史数据保留。

    真实配置里每个标的都启用，所以这条路径只能靠构造来测 ——
    否则把 ``enabled_symbols`` 写成 ``return self.symbols``，全套测试照样绿。
    """

    def test_disabled_symbol_drops_out(self, tmp_path: Path) -> None:
        universe = _raw("universe.yaml")
        tsla = next(s for s in universe["symbols"] if s["symbol"] == "TSLA")
        tsla["enabled"] = False
        _write_all(tmp_path, universe=universe)
        cfg = load_config(tmp_path)
        assert len(cfg.universe.symbols) == 17  # 还在表里
        assert len(cfg.universe.enabled_symbols) == 16  # 但不再启用
        assert "TSLA" not in cfg.universe.pool("stocks")
        assert len(cfg.universe.pool("stocks")) == 15


class TestStructuralColumns:
    """``check_columns_match`` 可以直接吃整张表的列。

    让调用方自己维护一份「哪些列是结构列」，就是 §6.1.1 警告的
    「两个要对齐的地方」—— M3 的 SQL 解析器会原样传整张表。
    """

    def test_full_table_columns_accepted(self, cfg: Config) -> None:
        full = set(REAL_METRICS_COLUMNS) | {
            "symbol",
            "date",
            "extra",
            "provisional_metrics",
            "computed_at",
        }
        check_columns_match(cfg, full)


class TestCompositeScore:
    """§4.1 把 composite 列为可选的主排序分，它必须真的可选。"""

    def test_composite_can_be_selected(self, tmp_path: Path) -> None:
        strength = _raw("strength.yaml")
        strength["score_metric"] = "composite"
        strength["composite"]["enabled"] = True
        _write_all(tmp_path, strength=strength)
        assert load_config(tmp_path).strength.score_metric == "composite"

    def test_selected_but_disabled_is_rejected(self, tmp_path: Path) -> None:
        strength = _raw("strength.yaml")
        strength["score_metric"] = "composite"
        _write_all(tmp_path, strength=strength)
        with pytest.raises(ConfigError, match="关着的尺子"):
            load_config(tmp_path)

    def test_enabled_but_unused_is_rejected(self, tmp_path: Path) -> None:
        """一个开着但没人用的旋钮，迟早会被当成生效了。"""
        strength = _raw("strength.yaml")
        strength["composite"]["enabled"] = True
        _write_all(tmp_path, strength=strength)
        with pytest.raises(ConfigError, match="没被 score_metric 选用"):
            load_config(tmp_path)

    def test_negative_weight_rejected(self, tmp_path: Path) -> None:
        """公式是 ``w_mom*z(mom) - w_resid_vol*z(resid_vol)``，
        负权重会把风险惩罚反转成风险**奖励**。
        """
        strength = _raw("strength.yaml")
        strength["composite"]["w_resid_vol"] = -5.0
        _write_all(tmp_path, strength=strength)
        with pytest.raises(ConfigError):
            load_config(tmp_path)


class TestDisplayVocabularyIsClosed:
    """前端直接用 js-yaml 读这份 YAML，没有 codegen 步骤 ——
    ``widget: plian`` 只会在 Vercel 构建时变成缺失组件，或者一个静默的兜底渲染。
    """

    def test_unknown_widget_rejected(self, tmp_path: Path) -> None:
        metrics = _raw("metrics.yaml")
        rsi = next(m for m in metrics["metrics"] if m["id"] == "rsi_14")
        rsi["display"]["widget"] = "plian"
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError):
            load_config(tmp_path)

    def test_malformed_format_rejected(self, tmp_path: Path) -> None:
        metrics = _raw("metrics.yaml")
        rsi = next(m for m in metrics["metrics"] if m["id"] == "rsi_14")
        rsi["display"]["format"] = "numbr:1"
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError):
            load_config(tmp_path)


class TestRealConfigDimWhen:
    """把 fail-closed 这条测试**挂到真实配置上**。

    否则所有求值测试都写死 ``"abs(alpha_t_stat) < 2"`` 这个字面量，
    而把 YAML 里的 ``abs`` 删掉（一个现实的手滑）全套测试照样绿，
    同时每一行负 alpha 都不再打灰。
    """

    def test_benchmark_row_is_dimmed_by_the_real_expression(self, cfg: Config) -> None:
        ab = next(m for m in cfg.metrics.metrics if m.fn == "alpha_beta")
        assert isinstance(ab.display, dict)
        spec = ab.display["alpha_annual"]
        assert spec.dim_when is not None
        expr = parse_dim_when(spec.dim_when)

        # §3.4：QQQ 对自己 → alpha_t_stat 写 NULL → 必须打灰
        assert expr.evaluate({"alpha_t_stat": None, "alpha_annual": 0.0}) is True
        # 显著的负 alpha 不该打灰 —— 这一条能抓住「abs 被删掉」
        assert expr.evaluate({"alpha_t_stat": -3.0}) is False
        assert expr.evaluate({"alpha_t_stat": 3.0}) is False
        # 不显著的两侧都要打灰
        assert expr.evaluate({"alpha_t_stat": 1.0}) is True
        assert expr.evaluate({"alpha_t_stat": -1.0}) is True
