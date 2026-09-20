"""半年 alpha / beta（基准 QQQ，§3.4）。

这个指标有四处容易静默出错的地方，每一处都在下面写明了为什么。

1. **日期对齐：先各自算收益率，再对收益率做 inner join。**
   绝不能先 join 价格再差分 —— 某标的缺一天时，跨缺口的那一段会被当成单日收益，
   量级约 √2 倍偏大，推高 ``Var(r_s)`` 并拖动 beta 与残差波动；
   而 ``n_obs`` 仍会读到 125，安全地高于 ``min_obs: 120``，**没有任何东西会报警**。
2. **连续性校验要打在 join 之前的各自序列上。**
   设某股票有周一和周三、缺周二：它「周三的日收益」实际是两个 session 的收益。
   join 之后剩下的日期可能是周三、周四 —— 在交易所日历上**相邻**，
   于是一条「join 后日期相邻」的断言会放行，而那个两日收益照样进了 OLS。
3. **年化用线性，不用复利。** ``alpha_d`` 是回归残差的均值，不是复利收益路径。
   复利式把算术均值当几何均值用，且高估幅度随估计值单调放大
   （0.5%/日 → 线性 +126% / 复利 +252%；1.0%/日 → +252% / +1130%）。
4. **基准对自己必须特判。** QQQ 在 universe 里，所以这个退化回归每天都会算一次：
   残差恒为 0 → ``resid_vol = 0`` → ``alpha_t_stat = 0/0``。NaN 会朝
   **失败开放**的方向出错：``abs(NaN) < 2`` 为假 → 基准那一行**不打灰** →
   UI 宣称一个「统计显著」的 0.00% alpha，恰好是真相的反面。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from pipeline.metrics.registry import register

__all__ = ["AlphaBetaResult", "alpha_beta", "session_returns"]

TRADING_DAYS_PER_YEAR = 252

# 「残差实质为零」的相对判据（SSR/SST，即 1 - R²）。
#
# 初版用的是 1e-20，**太紧了四个数量级**：float64 里严格小于 1.0 的最大数
# 其 1-R² 是 1.11e-16，所以 1e-20 的切点落在 float64 根本分辨不出的区域里。
# 实测：噪声 σ=1e-10 时 SSR/SST = 1.77e-17，守卫**不触发**，
# 而 alpha_t_stat = 1.26e8 —— `abs(1.26e8) < 2` 为假 → 那一行**不打灰**
# → UI 宣称一个统计显著的 alpha。正是这个守卫要防的事。
#
# 放宽到 1e-12 没有误伤风险：没有任何两只美股的 126 日收益回归能达到
# R² > 1 - 1e-12；即便是杠杆 ETF 对其标的也就 0.999 量级。
_DEGENERATE_SSR_RATIO = 1e-12

# |t| 的合理上限。n=126 时 |t| > 50 对应 p < 1e-80。
#
# **这是一条产品策略，不是数值定理** —— 一个噪声极低但数学上完全合法的过程
# 确实可能有真实的 |t| > 50，而这里会把它抹成 NULL。
# 之所以接受这个代价：在「17 只大型科技股的 126 日收益对 QQQ 回归」这个
# 具体场景里，|t| > 50 现实中只会来自数值退化；而单靠 R² 阈值**界不住 |t|**
# （实测：SSR/SST = 1.01e-12 恰好在退化阈值之外，t 仍高达 4.9e5）。
# 超限写 None → 前端 fail closed 打灰，是保守的那一侧。
_MAX_PLAUSIBLE_T = 50.0


@dataclass(frozen=True)
class AlphaBetaResult:
    """一次回归的全部产出。``None`` 表示「无可用数值」，写库时落 NULL。"""

    alpha_annual: float | None
    beta: float | None
    r2: float | None
    corr: float | None
    resid_vol_annual: float | None
    alpha_t_stat: float | None
    n_obs: int

    @classmethod
    def empty(cls, n_obs: int = 0) -> AlphaBetaResult:
        return cls(None, None, None, None, None, None, n_obs)


def session_returns(adj_close: pd.Series, ordinals: pd.Series) -> pd.Series:
    """日简单收益，**只保留前一根 bar 是紧邻上一个 session 的那些**。

    ``ordinals`` 是 ``trading_sessions.ordinal``，与 ``adj_close`` 同索引。
    相邻判据是 ``ordinal - prev_ordinal == 1`` —— **不是日期相减**。
    只有 date 的话没人知道感恩节，而用工作日差替代会把交易所假日误判为连续。
    """
    if not adj_close.index.equals(ordinals.index):
        raise ValueError("adj_close 与 ordinals 必须同索引")

    prices = adj_close.astype(float)
    prev_price = prices.shift(1)
    prev_ord = ordinals.shift(1)

    gap_ok = (ordinals - prev_ord) == 1
    # **分子分母都要查，且都用 `> 0`。**
    # 只查分母是不够的：`[100, 0, 101]` 会产出一个 **-1.0 的日收益** ——
    # 一个看起来完全正常、会静悄悄进入 OLS 的数；负价格同理
    # （`-2` 接 `101` 算出 -1.02）。价格为 0 或负只能来自数据错误，
    # 而这类错误产出的是**有限值**，所以 §9.1.3 的 NaN sanitizer 拦不住它。
    price_ok = prices.notna() & (prices > 0)
    prev_ok = prev_price.notna() & (prev_price > 0)
    usable = gap_ok & price_ok & prev_ok

    rets = prices / prev_price - 1.0
    return rets.where(usable).rename("ret")


@register("alpha_beta", kind="pairwise")
def alpha_beta(
    symbol_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    window: int = 126,
    min_obs: int = 120,
    annualization: str = "linear",
    risk_free_daily: float = 0.0,
    is_benchmark: bool = False,
) -> AlphaBetaResult:
    """对**收益率序列**做 OLS。调用方先用 :func:`session_returns` 算好两条序列。"""
    if min_obs < 3:
        # 残差自由度是 n-2；n=2 会在 ssr/(n-2) 处除零。
        # 在这里报而不是等着被除零，是为了让错误信息能指向 config 键。
        raise ValueError(f"min_obs 必须 >= 3（残差自由度是 n-2），得到 {min_obs}")

    n_joined = _count_joined(symbol_returns, benchmark_returns, window)
    if n_joined < min_obs:
        # **先查样本量，再看是不是基准。**
        # 反过来的话，首次回填那天（或任何基准收益 join 不上的天）
        # QQQ 那一行会在 **0 个观测值**上断言一个完整而自信的 α/β/R²。
        return AlphaBetaResult.empty(n_joined)

    if is_benchmark:
        # 基准对自己：直接给出解析解，不跑回归（见模块 docstring 第 4 条）。
        # resid_vol 是 0，而 t 值是 0/0 —— 所以 t 值写 None，不写 0 也不写 NaN。
        return AlphaBetaResult(
            alpha_annual=0.0,
            beta=1.0,
            r2=1.0,
            corr=1.0,
            resid_vol_annual=0.0,
            alpha_t_stat=None,
            n_obs=n_joined,
        )

    joined = pd.concat({"y": symbol_returns, "x": benchmark_returns}, axis=1, join="inner").dropna()
    if window > 0:
        joined = joined.tail(window)

    n = len(joined)

    y = joined["y"].to_numpy(dtype=float) - risk_free_daily
    x = joined["x"].to_numpy(dtype=float) - risk_free_daily

    x_var = float(np.var(x, ddof=1))
    if x_var == 0.0 or not np.isfinite(x_var):
        # 基准在整个窗口里没有波动：beta 无定义。
        return AlphaBetaResult.empty(n)

    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    beta = float(np.cov(y, x, ddof=1)[0, 1] / x_var)
    alpha_d = y_mean - beta * x_mean

    resid = y - (alpha_d + beta * x)
    ssr = float(np.sum(resid**2))
    sst = float(np.sum((y - y_mean) ** 2))
    # 「残差实质为零」必须用**相对**判据，不能用 `== 0.0`：
    # 完美线性关系下浮点残差是 ~1e-17 而不是精确 0。阈值的由来见模块顶部。
    degenerate = sst <= 0.0 or ssr <= _DEGENERATE_SSR_RATIO * sst
    # ddof 必须是 n-2（估了截距与斜率两个参数）。用 n-1 会把标准误低估
    # sqrt((n-2)/(n-1))，方向上与多重比较的问题叠加，两者都在放大 |t|。
    resid_var = ssr / (n - 2)
    resid_vol_daily = float(np.sqrt(resid_var))

    # 精确的 OLS 截距标准误。常见的简化式 alpha_d / (s/sqrt(n)) 漏掉了
    # sqrt(1 + n*x̄²/((n-1)*s_x²)) 这个因子。在本项目的取值范围内它其实很小
    # （n=126, x̄≈0.0005, s_x≈0.012 → ≈1.0009，即 +0.09%），
    # 所以结论不是「近似式会毁掉显著性判定」，而是「精确式不花钱，没理由用近似式」。
    se_alpha = _intercept_se(resid_vol_daily, n, x_mean, x_var)
    alpha_t = None if se_alpha is None or se_alpha == 0.0 else alpha_d / se_alpha

    corr_mat = np.corrcoef(y, x)
    corr = float(corr_mat[0, 1]) if np.isfinite(corr_mat[0, 1]) else None
    # r2 由 1 - SSR/SST **独立**算出，不从 corr 导出。
    # 两者在简单回归下精确相等 —— 只有分开算，那条恒等式才是一条**真的不变式**，
    # 能白捡一整类符号 / 索引错误；若写成 `r2 = corr*corr`，
    # 对应的测试就只是一句同义反复，永远不会失败。
    r2 = None if sst <= 0.0 else 1.0 - ssr / sst

    if annualization == "linear":
        alpha_annual = alpha_d * TRADING_DAYS_PER_YEAR
    elif annualization == "compound":
        alpha_annual = (1.0 + alpha_d) ** TRADING_DAYS_PER_YEAR - 1.0
    else:
        raise ValueError(f"annualization 只能是 linear / compound，得到 {annualization!r}")

    result = AlphaBetaResult(
        alpha_annual=_finite(alpha_annual),
        beta=_finite(beta),
        r2=_finite(r2),
        corr=_finite(corr),
        resid_vol_annual=_finite(resid_vol_daily * np.sqrt(TRADING_DAYS_PER_YEAR)),
        alpha_t_stat=_finite(alpha_t),
        n_obs=n,
    )
    if degenerate:
        # 残差实质为零（标的与基准完全线性相关）：t 值是 0/0。
        # 连带把 resid_vol / r2 归成干净值 —— 否则这条记录会带着
        # resid_vol=5e-17 与 r2=0.9999999999999996 这种与基准特判分支不一致的邻居值。
        result = replace(result, alpha_t_stat=None, resid_vol_annual=0.0, r2=1.0)
    elif result.alpha_t_stat is not None and abs(result.alpha_t_stat) > _MAX_PLAUSIBLE_T:
        # 单靠 R² 阈值界不住 |t|，再加一道合理性天花板。
        result = replace(result, alpha_t_stat=None)
    return result


def _intercept_se(resid_vol_daily: float, n: int, x_mean: float, x_var: float) -> float | None:
    """OLS 截距的精确标准误 ``s * sqrt(1/n + x̄²/((n-1)*s_x²))``。"""
    if x_var <= 0 or n < 3:
        return None
    se = resid_vol_daily * np.sqrt(1.0 / n + (x_mean * x_mean) / ((n - 1) * x_var))
    return float(se) if np.isfinite(se) else None


def _count_joined(a: pd.Series, b: pd.Series, window: int) -> int:
    joined = pd.concat({"y": a, "x": b}, axis=1, join="inner").dropna()
    return len(joined.tail(window) if window > 0 else joined)


def _finite(value: float | None) -> float | None:
    """非有限值一律收敛成 ``None`` —— NaN/inf 绝不进入写库路径（§9.1.3）。"""
    if value is None:
        return None
    f = float(value)
    return f if np.isfinite(f) else None
