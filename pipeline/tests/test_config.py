"""配置层测试（§6）。

M1 的验收标准是「非法 config 被明确报错；universe 17 个标的解析正确；
``min_bars <= provisional_below`` 不变式生效；§6.2 三条 CI 校验生效」。
这份文件逐条对着那四项写。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from pipeline.config import (
    CONFIG_DIR,
    Config,
    ConfigError,
    MetricsConfig,
    StrengthConfig,
    UniverseConfig,
    check_columns_match,
    load_config,
)

# 真实 metrics_daily 列集合（§9.1）。M3 会从 0001_init.sql 解析出同一份，
# 届时这个常量与 SQL 的一致性由 M3 的测试负责。
REAL_METRICS_COLUMNS = {
    "rsi_14",
    "ema_60",
    "close_vs_ema60_pct",
    "ema60_slope_20d",
    "alpha_annual",
    "beta",
    "r2",
    "corr",
    "resid_vol_annual",
    "alpha_t_stat",
    "n_obs",
    "mom_20",
    "days_to_next_earnings",
    "days_since_last_earnings",
    "days_to_next_dividend",
    "days_since_last_dividend",
    "next_earnings_date",
    "next_dividend_date",
    "next_earnings_is_estimated",
    "next_dividend_is_estimated",
}


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


def _raw(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def _write_all(tmp: Path, **overrides: dict[str, Any]) -> Path:
    """把四个真实配置落到 tmp 目录，按需覆盖其中一两个。"""
    for name in ("universe.yaml", "metrics.yaml", "strength.yaml", "app.yaml"):
        key = name.removesuffix(".yaml")
        data = overrides.get(key, _raw(name))
        with (tmp / name).open("w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    return tmp


# ---------------------------------------------------------------------------
# 验收标准 1：真实配置解析正确
# ---------------------------------------------------------------------------
class TestRealConfigParses:
    def test_universe_has_17_symbols(self, cfg: Config) -> None:
        """QQQ + 16 只个股 = 17（§2；SPY 不入池，§12 #2）。"""
        assert len(cfg.universe.symbols) == 17
        assert len(cfg.universe.enabled_symbols) == 17
        assert "SPY" not in {s.symbol for s in cfg.universe.symbols}

    def test_universe_matches_low_buy_snapshot(self, cfg: Config) -> None:
        """这 16 只是 low-buy ``core/data.py`` 的事实快照（拷贝，非依赖）。"""
        expected = {
            "AAPL",
            "AMD",
            "AMZN",
            "ASML",
            "AVGO",
            "COST",
            "GOOGL",
            "INTC",
            "META",
            "MSFT",
            "MU",
            "NFLX",
            "NVDA",
            "ORCL",
            "TSLA",
            "TSM",
        }
        stocks = {s.symbol for s in cfg.universe.symbols if s.type == "stock"}
        assert stocks == expected

    def test_benchmark_is_qqq_and_in_universe(self, cfg: Config) -> None:
        assert cfg.universe.benchmark == "QQQ"
        assert "QQQ" in {s.symbol for s in cfg.universe.symbols}

    def test_rank_pool_excludes_etf(self, cfg: Config) -> None:
        """排名池默认只含个股：ETF 与单票的动量方差不可比（§4.1）。"""
        assert len(cfg.universe.pool("stocks")) == 16
        assert "QQQ" not in cfg.universe.pool("stocks")
        assert len(cfg.universe.pool("all")) == 17

    def test_app_params(self, cfg: Config) -> None:
        assert cfg.app.lookback_bars == 400  # EMA60 的 6×N=360 取整留裕量
        assert cfg.app.settle_minutes == 60
        assert cfg.app.sessions_start_date.isoformat() == "2024-01-02"

    def test_strength_defaults(self, cfg: Config) -> None:
        assert cfg.strength.score_metric == "mom_20"
        assert cfg.strength.rank_pool == "stocks"
        assert cfg.strength.top_n == 3
        assert cfg.strength.composite.enabled is False


# ---------------------------------------------------------------------------
# 验收标准 2：min_bars <= provisional_below 不变式
# ---------------------------------------------------------------------------
class TestWarmupInvariant:
    def test_real_config_satisfies_it(self, cfg: Config) -> None:
        for m in cfg.metrics.metrics:
            if m.min_bars is not None and m.provisional_below is not None:
                assert m.min_bars <= m.provisional_below, m.id

    def test_rsi_thresholds_are_not_inverted(self, cfg: Config) -> None:
        """初稿把 RSI 写成 250/150，两者反了 —— 硬闸门高于软闸门会让后者永不可达。"""
        rsi = cfg.metrics.by_id("rsi_14")
        assert rsi is not None
        assert rsi.min_bars == 15  # delta_0 不存在，第一个 RSI 在第 15 根
        assert rsi.provisional_below == 250

    def test_inverted_is_rejected(self, tmp_path: Path) -> None:
        metrics = _raw("metrics.yaml")
        rsi = next(m for m in metrics["metrics"] if m["id"] == "rsi_14")
        rsi["min_bars"], rsi["provisional_below"] = 250, 150
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="永远不可达"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# 验收标准 3：§6.2 的三条 CI 校验
# ---------------------------------------------------------------------------
class TestSchemaChecks:
    def test_real_config_matches_real_columns(self, cfg: Config) -> None:
        """双向一致：既不多也不少。"""
        check_columns_match(cfg, set(REAL_METRICS_COLUMNS))

    def test_check_1_config_declares_column_db_lacks(self, cfg: Config) -> None:
        cols = set(REAL_METRICS_COLUMNS) - {"ema60_slope_20d"}
        with pytest.raises(ConfigError, match="忘了迁移"):
            check_columns_match(cfg, cols)

    def test_check_2_db_has_column_config_lacks(self, cfg: Config) -> None:
        """反向检查 —— 初稿只做了正向，``ema60_slope_20d`` 正是这样漏掉的。"""
        cols = set(REAL_METRICS_COLUMNS) | {"orphan_column"}
        with pytest.raises(ConfigError, match="空洞"):
            check_columns_match(cfg, cols)

    def test_check_3_missing_display_entry(self, tmp_path: Path) -> None:
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["id"] == "alpha_beta_126")
        del ab["display"]["n_obs"]
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="没有 display 条目"):
            load_config(tmp_path)

    def test_display_key_not_an_output(self, tmp_path: Path) -> None:
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["id"] == "alpha_beta_126")
        ab["display"]["not_an_output"] = {"label": "x", "format": "int", "widget": "plain"}
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="非 output"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# 验收标准 4：非法 config 被明确报错
# ---------------------------------------------------------------------------
class TestRejectsInvalid:
    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        """拼错一个键应该立刻失败，而不是被静默忽略。"""
        app = _raw("app.yaml")
        app["lookback_barz"] = 400
        _write_all(tmp_path, app=app)
        with pytest.raises(ConfigError):
            load_config(tmp_path)

    def test_benchmark_not_in_symbols(self) -> None:
        with pytest.raises(ConfigError, match="不在 symbols"):
            UniverseConfig.model_validate(
                {
                    "benchmark": "SPY",
                    "symbols": [{"symbol": "QQQ", "name": "Q", "type": "etf"}],
                }
            )

    def test_benchmark_disabled(self) -> None:
        """基准关掉的话 alpha/beta 整列都算不出来。"""
        with pytest.raises(ConfigError, match="alpha/beta"):
            UniverseConfig.model_validate(
                {
                    "benchmark": "QQQ",
                    "symbols": [{"symbol": "QQQ", "name": "Q", "type": "etf", "enabled": False}],
                }
            )

    def test_duplicate_symbol(self) -> None:
        with pytest.raises(ConfigError, match="重复标的"):
            UniverseConfig.model_validate(
                {
                    "benchmark": "QQQ",
                    "symbols": [
                        {"symbol": "QQQ", "name": "Q", "type": "etf"},
                        {"symbol": "QQQ", "name": "Q2", "type": "etf"},
                    ],
                }
            )

    def test_constant_shift_as_score_metric_is_rejected(self) -> None:
        """常数平移量当排序尺子是空操作，而切换它会清空所有 days_in_top_n。

        一个数学上的空操作造成一次用户可见的撒谎（§12 #3）——
        所以它必须在加载期就被拦住，而不是等某天有人真的改了 config。
        """
        with pytest.raises(ConfigError, match="常数平移量"):
            StrengthConfig(
                score_metric="rs_vs_qqq_20d",
                rank_pool="stocks",
                top_n=3,
                tie_break="symbol",
                squeak_k=0.1,
            )

    def test_score_metric_not_declared(self, tmp_path: Path) -> None:
        strength = _raw("strength.yaml")
        strength["score_metric"] = "not_a_metric"
        _write_all(tmp_path, strength=strength)
        with pytest.raises(ConfigError, match="没有声明"):
            load_config(tmp_path)

    def test_benchmark_mismatch_across_files(self, tmp_path: Path) -> None:
        """「α 是相对谁的」不能在两个文件里各说各话。"""
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["id"] == "alpha_beta_126")
        ab["params"]["benchmark"] = "SPY"
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="不一致"):
            load_config(tmp_path)

    def test_top_n_exceeds_pool(self, tmp_path: Path) -> None:
        strength = _raw("strength.yaml")
        strength["top_n"] = 99
        _write_all(tmp_path, strength=strength)
        with pytest.raises(ConfigError, match="超过了排名池"):
            load_config(tmp_path)

    def test_sparkline_longer_than_lookback(self, tmp_path: Path) -> None:
        app = _raw("app.yaml")
        app["sparkline_bars"] = 9999
        _write_all(tmp_path, app=app)
        with pytest.raises(ConfigError, match="走势线"):
            load_config(tmp_path)

    def test_backfill_faster_than_daily_is_rejected(self, tmp_path: Path) -> None:
        """回填是一次性大动作，应当比日常运行**更慢**（§7.3.1）。"""
        app = _raw("app.yaml")
        app["backfill_interval_seconds"] = 0.1
        _write_all(tmp_path, app=app)
        with pytest.raises(ConfigError, match="更慢"):
            load_config(tmp_path)

    def test_bad_dim_when_is_rejected(self, tmp_path: Path) -> None:
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["id"] == "alpha_beta_126")
        ab["display"]["beta"]["dim_when"] = "__import__('os')"
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError):
            load_config(tmp_path)

    def test_dim_when_referencing_unknown_field(self, tmp_path: Path) -> None:
        """引用了不存在的字段 → 这条 dim_when 永远求不出值，等于没写。"""
        metrics = _raw("metrics.yaml")
        ab = next(m for m in metrics["metrics"] if m["id"] == "alpha_beta_126")
        ab["display"]["beta"]["dim_when"] = "no_such_field < 1"
        _write_all(tmp_path, metrics=metrics)
        with pytest.raises(ConfigError, match="不存在的字段"):
            load_config(tmp_path)

    def test_duplicate_column_across_metrics(self) -> None:
        with pytest.raises(ConfigError, match="争用同一个列名"):
            MetricsConfig.model_validate(
                {
                    "metrics": [
                        {
                            "id": "a",
                            "fn": "momentum",
                            "params": {"period": 20},
                            "core": True,
                            "display": {"label": "A", "format": "int", "widget": "plain"},
                        },
                        {
                            "id": "b",
                            "fn": "ema",
                            "params": {"period": 60},
                            "core": True,
                            "outputs": ["a"],
                            "display": {"a": {"label": "A", "format": "int", "widget": "plain"}},
                        },
                    ]
                }
            )

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="缺少配置文件"):
            load_config(tmp_path)
