"""配置层（§6）。

``config/`` 下的四个 YAML 是 pipeline、数据库写入、前端表头与方法论页的
**单一事实来源**。前端在构建期用 js-yaml 直接读同样的文件 —— 不生成中间产物，
因为「生成 + CI 检查是否同步」拦不住 Vercel 部署（它从 git 部署，与 CI 状态无关）。

这里做的事：把 YAML 读成经过校验的模型，并把**会静默出错的东西挡在加载期**。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pipeline.dim_expr import DimExprError, parse_dim_when
from pipeline.errors import ConfigError

__all__ = [
    "AppConfig",
    "Config",
    "ConfigError",
    "MetricSpec",
    "StrengthConfig",
    "Symbol",
    "UniverseConfig",
    "load_config",
]

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# 常数平移量：对同一天的所有标的减去同一个数，排名恒等于不减。
# 它们可以当展示列，**绝不能当排序尺子** —— 那是空操作，
# 而切换 score_metric 会按 §4.2 清空所有 days_in_top_n，
# 于是一个数学上的空操作造成一次用户可见的撒谎（§12 #3）。
PosInt = Annotated[int, Field(ge=1)]

CONSTANT_SHIFT_METRICS = frozenset({"delta_to_median", "rs_vs_qqq_20d"})

# metrics_daily 里**不是指标**的列（§9.1）。§6.2 的双向校验只管指标列，
# 所以这份名单必须和 core_columns 住在同一个文件里 ——
# 放到调用方去维护，就是 §6.1.1 警告的「两个要对齐的地方」。
STRUCTURAL_COLUMNS = frozenset({"symbol", "date", "extra", "provisional_metrics", "computed_at"})


class _Strict(BaseModel):
    """所有配置模型的基类。

    - ``extra="forbid"``：拼错一个键应该立刻失败，而不是被静默忽略。
    - ``allow_inf_nan=False``：``Field(ge=0)`` **是接受 ``inf`` 的**，
      于是 YAML 里写 ``annual: .inf`` 能顺利通过校验并毒化整列 alpha。
      这条把所有浮点字段的 inf/nan 一次性堵死。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


# ---------------------------------------------------------------------------
# universe.yaml
# ---------------------------------------------------------------------------
class Symbol(_Strict):
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]*$")
    name: str = Field(min_length=1)
    type: Literal["stock", "etf"]
    enabled: bool = True


class UniverseConfig(_Strict):
    benchmark: str
    symbols: list[Symbol] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        seen = [s.symbol for s in self.symbols]
        dupes = {s for s in seen if seen.count(s) > 1}
        if dupes:
            raise ConfigError(f"universe.yaml 有重复标的：{sorted(dupes)}")
        if self.benchmark not in seen:
            raise ConfigError(
                f"benchmark {self.benchmark!r} 不在 symbols 里。"
                "它既是基准又是 universe 成员，两个角色都要声明（§2）。"
            )
        bench = next(s for s in self.symbols if s.symbol == self.benchmark)
        if not bench.enabled:
            raise ConfigError(
                f"benchmark {self.benchmark!r} 被 enabled: false 了 —— "
                "基准关掉的话 alpha/beta 整列都算不出来。"
            )
        return self

    @property
    def enabled_symbols(self) -> list[Symbol]:
        return [s for s in self.symbols if s.enabled]

    def pool(self, rank_pool: Literal["stocks", "all"]) -> list[str]:
        """排名池（§4.1）。``stocks`` 排除 ETF。"""
        return [s.symbol for s in self.enabled_symbols if rank_pool == "all" or s.type == "stock"]


# ---------------------------------------------------------------------------
# metrics.yaml
# ---------------------------------------------------------------------------
# 前端直接用 js-yaml 读这份 YAML，中间没有 codegen 步骤 ——
# 于是 `widget: plian` 这种笔误只会在 Vercel 构建时变成缺失组件，
# 或者更糟：一个静默的兜底渲染。闭集合在这里拦下来最便宜。
Widget = Literal[
    "plain", "signed", "rsi_bar", "beta_bar", "diverging_bar", "countdown_chip", "hidden"
]
# price / int / date / bool / days / number:N / pct:N
FORMAT_RE = r"^(price|int|date|bool|days|(number|pct):[0-9])$"


class DisplaySpec(_Strict):
    label: str = Field(min_length=1)
    format: str = Field(pattern=FORMAT_RE)
    widget: Widget
    dim_when: str | None = None
    estimated_flag: str | None = None


# ---------------------------------------------------------------------------
# params 的类型化（§6.2 校验之外的一条）
#
# `params: dict[str, Any]` 是 _Strict「拼错一个键就立刻失败」这条承诺
# 唯一的破口，而它恰好是语义最重的那些值住的地方。
# §3.4 把后果写得很清楚：`units` 写成 `percentage` → 漏掉 /100 →
# 得到 2.08%/日 的无风险利率和一个灾难性的负 alpha，
# **看起来像市场崩盘，实际是单位 bug**。这种东西必须在加载期死。
# ---------------------------------------------------------------------------
class RiskFreeSpec(_Strict):
    mode: Literal["constant", "series"]
    annual: float = Field(ge=0)
    symbol: str = Field(min_length=1)
    # ^IRX 报的是百分数（5.25 = 5.25%），必须先 /100 再 /252。
    units: Literal["percent", "fraction"]


class PeriodParams(_Strict):
    period: PosInt


class AlphaBetaParams(_Strict):
    window: PosInt
    benchmark: str = Field(min_length=1)
    min_obs: PosInt
    # 线性，不是复利（§3.4）：复利式会把强势半年的 α 夸大 2–4.5 倍。
    annualization: Literal["linear", "compound"] = "linear"
    risk_free: RiskFreeSpec

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min_obs > self.window:
            raise ConfigError(
                f"min_obs({self.min_obs}) > window({self.window})：门槛高于窗口本身，永远达不到"
            )
        return self


class EventDistanceParams(_Strict):
    unit: Literal["calendar_days"]
    # 事件列只写最新一行，历史行一律 NULL（§3.5(3)）。
    latest_row_only: bool


# 注册函数名 → 它的 params 模型。新增指标时**必须**在这里登记，
# 否则 `_check_params` 会直接报错 —— 这是故意的：
# 一个没人校验 params 的指标，等于把 §6.2 的承诺开了个后门。
PARAM_MODELS: dict[str, type[_Strict]] = {
    "rsi_wilder": PeriodParams,
    "ema": PeriodParams,
    "momentum": PeriodParams,
    "alpha_beta": AlphaBetaParams,
    "event_distances": EventDistanceParams,
}


class DerivedSpec(_Strict):
    id: str = Field(min_length=1)
    expr: str = Field(min_length=1)
    display: DisplaySpec


class MetricSpec(_Strict):
    id: str = Field(min_length=1)
    fn: str = Field(min_length=1)
    core: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    min_bars: int | None = Field(default=None, ge=1)
    provisional_below: int | None = Field(default=None, ge=1)
    outputs: list[str] | None = None
    # 单输出指标写 display 对象；多输出指标写 {output_id: display}。
    display: DisplaySpec | dict[str, DisplaySpec]
    derived: list[DerivedSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        # §3.3 的统一语义。初稿把 RSI 写成 min_bars=250 / provisional_below=150，
        # 两者反了：250 的硬闸门会让 150 的软闸门永远不可达，
        # 且 150–250 这一段没有任何规定。
        if (
            self.min_bars is not None
            and self.provisional_below is not None
            and self.min_bars > self.provisional_below
        ):
            raise ConfigError(
                f"指标 {self.id!r}：min_bars({self.min_bars}) > "
                f"provisional_below({self.provisional_below})。"
                "硬闸门高于软闸门会让后者永远不可达（§3.3）。"
            )

        outs = self.output_ids
        # §6.2 校验 3：core 指标的每个 output 都必须有 display 条目，
        # 否则前端要显示的字段没有配置来源，「前后端永不漂移」第一天就破了。
        if isinstance(self.display, dict):
            missing = set(outs) - set(self.display)
            if missing:
                raise ConfigError(
                    f"指标 {self.id!r} 的 output {sorted(missing)} 没有 display 条目（§6.2 校验 3）"
                )
            extra = set(self.display) - set(outs)
            if extra:
                raise ConfigError(
                    f"指标 {self.id!r} 的 display 多出了非 output 的键：{sorted(extra)}"
                )
        elif len(outs) != 1:
            raise ConfigError(
                f"指标 {self.id!r} 有 {len(outs)} 个 output，display 必须写成 "
                "{{output_id: {{...}}}} 的映射形式"
            )

        for spec in self._display_specs():
            self._check_display(spec, outs)
        self._check_params()
        return self

    def _check_params(self) -> None:
        model = PARAM_MODELS.get(self.fn)
        if model is None:
            raise ConfigError(
                f"指标 {self.id!r} 的 fn {self.fn!r} 没有登记 params 模型"
                f"（pipeline/config.py 的 PARAM_MODELS）—— "
                "没有模型就意味着它的 params 无人校验，那是 §6.2 承诺的后门。"
            )
        try:
            model.model_validate(self.params)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"指标 {self.id!r} 的 params 不合法：{exc}") from exc

    def _display_specs(self) -> list[DisplaySpec]:
        if isinstance(self.display, dict):
            return list(self.display.values())
        return [self.display]

    def _check_display(self, spec: DisplaySpec, outs: list[str]) -> None:
        known = set(outs)
        if spec.dim_when is not None:
            try:
                expr = parse_dim_when(spec.dim_when)
            except DimExprError as exc:
                raise ConfigError(f"指标 {self.id!r}：{exc}") from exc
            unknown = expr.fields - known
            if unknown:
                raise ConfigError(
                    f"指标 {self.id!r} 的 dim_when {spec.dim_when!r} 引用了不存在的字段 "
                    f"{sorted(unknown)} —— 它永远求不出值"
                )
        if spec.estimated_flag is not None and spec.estimated_flag not in known:
            raise ConfigError(
                f"指标 {self.id!r} 的 estimated_flag {spec.estimated_flag!r} 不是本指标的 output"
            )

    @property
    def output_ids(self) -> list[str]:
        """本指标产出的列名。没写 ``outputs`` 就是单输出，列名即 ``id``。"""
        return list(self.outputs) if self.outputs is not None else [self.id]

    @property
    def column_ids(self) -> list[str]:
        """本指标占用的 ``metrics_daily`` 列，含 derived。"""
        return [*self.output_ids, *(d.id for d in self.derived)]


class MetricsConfig(_Strict):
    metrics: list[MetricSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [m.id for m in self.metrics]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ConfigError(f"metrics.yaml 有重复的指标 id：{sorted(dupes)}")
        cols = self.core_columns
        all_cols = [c for m in self.metrics if m.core for c in m.column_ids]
        col_dupes = {c for c in all_cols if all_cols.count(c) > 1}
        if col_dupes:
            raise ConfigError(f"多个指标争用同一个列名：{sorted(col_dupes)}")
        if not cols:
            raise ConfigError("metrics.yaml 里没有任何 core 指标")
        return self

    @property
    def core_columns(self) -> frozenset[str]:
        """所有 ``core: true`` 指标占用的列名（含 derived）。

        §6.2 校验 1/2 就是拿它与 ``metrics_daily`` 的实际列做**双向**比对。
        """
        return frozenset(c for m in self.metrics if m.core for c in m.column_ids)

    def by_id(self, metric_id: str) -> MetricSpec | None:
        return next((m for m in self.metrics if m.id == metric_id), None)


# ---------------------------------------------------------------------------
# strength.yaml
# ---------------------------------------------------------------------------
# §4.3 的复合分。公式是 `w_mom * z(mom_20) - w_resid_vol * z(resid_vol)`，
# 所以**负权重会把风险惩罚反转成风险奖励** —— 下界必须是 0。
class CompositeConfig(_Strict):
    enabled: bool = False
    w_mom: float = Field(default=1.0, ge=0)
    w_resid_vol: float = Field(default=0.0, ge=0)


# §4.1 允许主排序分切成 composite。它是 strength 层的构造，
# 不是 metrics.yaml 里的一个指标 id，所以跨文件检查要放行这个保留值。
COMPOSITE_SCORE = "composite"


class StrengthConfig(_Strict):
    score_metric: str
    rank_pool: Literal["stocks", "all"]
    top_n: int = Field(ge=1)
    tie_break: Literal["symbol"]
    squeak_k: float = Field(gt=0)
    composite: CompositeConfig = Field(default_factory=CompositeConfig)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.score_metric == COMPOSITE_SCORE and not self.composite.enabled:
            raise ConfigError(
                "score_metric 选了 composite，但 composite.enabled 是 false —— 选了一个关着的尺子。"
            )
        if self.score_metric != COMPOSITE_SCORE and self.composite.enabled:
            raise ConfigError(
                "composite.enabled 是 true 却没被 score_metric 选用 —— "
                "一个开着但没人用的旋钮，迟早会被当成生效了。"
            )
        if self.score_metric in CONSTANT_SHIFT_METRICS:
            raise ConfigError(
                f"score_metric 不能是 {self.score_metric!r}：它是常数平移量，"
                "对同一天的所有标的减去同一个数，排名恒等于不减。"
                "作为尺子是空操作，而切换 score_metric 会清空所有 days_in_top_n —— "
                "一个数学上的空操作造成一次用户可见的撒谎（§12 #3）。"
            )
        return self


# ---------------------------------------------------------------------------
# app.yaml
# ---------------------------------------------------------------------------
class SiteConfig(_Strict):
    title: str = Field(min_length=1)
    subtitle: str = Field(min_length=1)
    disclaimer: str = Field(min_length=1)
    # 只有前端用得上，但**必须声明**：``_Strict`` 是 ``extra="forbid"``，
    # 漏掉它会让整个管道在加载 config 时炸掉 —— 一个纯前端的改动
    # 把收盘后的抓取打死，而两边看不出任何关联。实测过：漏掉这一行时
    # ``load_config()`` 第一句就抛，``run_daily`` / ``backfill`` 全停。
    #
    # ``^https://`` 是一道 **CI 期**的护栏：pytest 会加载真实的 ``config/``，
    # 所以一个 ``javascript:`` 值过不了 PR。它**不是**前端的运行时校验 ——
    # ``web/lib/config.ts`` 是裸的 ``yaml.load``，Vercel 构建也不跑 pytest。
    repo_url: str = Field(pattern=r"^https://")


class AppConfig(_Strict):
    site: SiteConfig
    settle_minutes: PosInt
    lookback_bars: PosInt
    sparkline_bars: PosInt
    sessions_start_date: date
    sessions_horizon: PosInt
    events_refresh_weekday: int = Field(ge=1, le=7)
    events_refresh_within_days: PosInt
    request_interval_seconds: float = Field(ge=0)
    backfill_interval_seconds: float = Field(ge=0)
    max_requests_per_run: PosInt
    retry_max_attempts: PosInt
    revalidate_seconds: PosInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.sparkline_bars > self.lookback_bars:
            raise ConfigError(
                f"sparkline_bars({self.sparkline_bars}) > lookback_bars({self.lookback_bars})："
                "画不出比回看窗口还长的走势线。"
            )
        if self.backfill_interval_seconds < self.request_interval_seconds:
            raise ConfigError(
                "backfill_interval_seconds 不应小于 request_interval_seconds —— "
                "回填是一次性大动作，应当比日常运行**更慢**（§7.3.1）。"
            )
        return self


# ---------------------------------------------------------------------------
# 组合
# ---------------------------------------------------------------------------
class Config(_Strict):
    universe: UniverseConfig
    metrics: MetricsConfig
    strength: StrengthConfig
    app: AppConfig

    @model_validator(mode="after")
    def _check_cross_file(self) -> Self:
        """跨文件的一致性 —— 单看一个文件发现不了的那些。"""
        metric_ids = {m.id for m in self.metrics.metrics}
        if (
            self.strength.score_metric != COMPOSITE_SCORE
            and self.strength.score_metric not in metric_ids
        ):
            raise ConfigError(
                f"strength.yaml 的 score_metric {self.strength.score_metric!r} "
                f"在 metrics.yaml 里没有声明"
            )

        # alpha/beta 的 benchmark 必须与 universe 的 benchmark 一致，
        # 否则「α 是相对谁的」在两个文件里会各说各话（§3.4）。
        #
        # **锚在 fn 上而不是指标 id 上。** 初版写的是 by_id("alpha_beta_126")
        # 加一个 `if ab is not None`，于是把指标改名成 alpha_beta_252
        # （§12 把窗口变更当作纯 config 改动，这是个现实的编辑）
        # 就会让这条检查**静默失效** —— α 列悄悄变成对 SPY 的，
        # 而全站标签仍写着对 QQQ 的。缺失必须是响亮的。
        ab_metrics = [m for m in self.metrics.metrics if m.fn == "alpha_beta"]
        if len(ab_metrics) != 1:
            raise ConfigError(
                f"期望恰好一个 fn: alpha_beta 的指标，得到 {len(ab_metrics)} 个。"
                "少了它 alpha/beta 整列都没有来源；多了则不知道该信哪个。"
            )
        bench = ab_metrics[0].params.get("benchmark")
        if bench != self.universe.benchmark:
            raise ConfigError(
                f"{ab_metrics[0].id!r} 的 benchmark({bench!r}) 与 universe.yaml 的 "
                f"benchmark({self.universe.benchmark!r}) 不一致 —— "
                "「α 是相对谁的」不能在两个文件里各说各话"
            )

        pool = self.universe.pool(self.strength.rank_pool)
        if self.strength.top_n > len(pool):
            raise ConfigError(
                f"top_n({self.strength.top_n}) 超过了排名池大小({len(pool)}，"
                f"rank_pool={self.strength.rank_pool})"
            )
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"缺少配置文件：{path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)  # 必须 safe_load：yaml.load 能构造任意对象
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 的顶层必须是映射，得到 {type(data).__name__}")
    return data


def load_config(config_dir: Path | None = None) -> Config:
    """读取并校验 ``config/`` 下的四个 YAML。

    任何不合法之处都在这里抛 :class:`ConfigError`，附带足以定位的说明 ——
    「非法 config 被明确报错」是 M1 的验收标准。
    """
    d = config_dir or CONFIG_DIR
    try:
        return Config(
            universe=UniverseConfig(**_read_yaml(d / "universe.yaml")),
            metrics=MetricsConfig(**_read_yaml(d / "metrics.yaml")),
            strength=StrengthConfig(**_read_yaml(d / "strength.yaml")),
            app=AppConfig(**_read_yaml(d / "app.yaml")),
        )
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError 等
        raise ConfigError(f"配置校验失败：{exc}") from exc


# ---------------------------------------------------------------------------
# §6.2 的三条 CI 校验
# ---------------------------------------------------------------------------
def check_columns_match(cfg: Config, db_columns: set[str]) -> None:
    """§6.2 校验 1 与 2：config 的 core 列与 ``metrics_daily`` 的实际列**双向**一致。

    初稿只做了「声明 → 列」这一半，于是「§3 定义了但 config 没声明」那一半漏着
    —— ``ema60_slope_20d`` 正是这样漏掉的。

    ``db_columns`` 由调用方提供（M3 会从 ``0001_init.sql`` 解析出来），
    所以这个函数在没有数据库的情况下也能被测试。
    **可以直接传整张表的列**：:data:`STRUCTURAL_COLUMNS`（``symbol``/``date``/
    ``extra``/``provisional_metrics``/``computed_at``）会在这里被剔除 ——
    让调用方自己维护一份「哪些列是结构列」只会制造第二份需要对齐的名单。
    """
    declared = set(cfg.metrics.core_columns)
    db_columns = set(db_columns) - STRUCTURAL_COLUMNS
    missing_in_db = declared - db_columns
    if missing_in_db:
        raise ConfigError(
            f"metrics.yaml 声明了 {sorted(missing_in_db)}，但 metrics_daily 没有这些列 —— "
            "改了 config 忘了迁移（§6.2 校验 1）"
        )
    missing_in_config = db_columns - declared
    if missing_in_config:
        raise ConfigError(
            f"metrics_daily 有列 {sorted(missing_in_config)}，但 metrics.yaml 没有声明 —— "
            "schema 建了却没人算的空洞（§6.2 校验 2）"
        )
