"""半年 alpha / beta 的测试（§3.4）。

M2 的三条硬验收标准都在这里：
**QQQ 对自己 → β=1、α=0、R²=1**、**`r2 == corr²`**、以及样本不足写 NULL。
另外两条是设计里专门点名的静默错误路径：跨 session 缺口的收益、和 `ddof`。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pipeline.metrics.alpha_beta import (
    TRADING_DAYS_PER_YEAR,
    AlphaBetaResult,
    alpha_beta,
    session_returns,
)


def _series(values: list[float], ordinals: list[int]) -> tuple[pd.Series, pd.Series]:
    idx = pd.RangeIndex(len(values))
    return (
        pd.Series(values, index=idx, dtype=float),
        pd.Series(ordinals, index=idx, dtype="int64"),
    )


def _synthetic(
    n: int = 200, beta: float = 1.8, alpha_d: float = 0.0004, seed: int = 7
) -> tuple[pd.Series, pd.Series]:
    """造一条 ``y = alpha_d + beta*x + noise`` 的样本。"""
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0005, 0.012, n)
    noise = rng.normal(0.0, 0.008, n)
    y = alpha_d + beta * x + noise
    idx = pd.RangeIndex(n)
    return pd.Series(y, index=idx), pd.Series(x, index=idx)


class TestBenchmarkAgainstItself:
    """**M2 的硬验收标准。**

    QQQ 在 universe 里，所以这个退化回归每天都会在生产里算一次。
    残差恒为 0 → ``resid_vol = 0`` → ``alpha_t_stat = 0/0``。
    NaN 会朝**失败开放**的方向出错：``abs(NaN) < 2`` 为假 → 基准那一行不打灰
    → UI 宣称一个「统计显著」的 0.00% alpha，恰好是真相的反面。
    """

    def test_beta_1_alpha_0_r2_1(self) -> None:
        y, _ = _synthetic()
        got = alpha_beta(y, y, window=126, min_obs=120, is_benchmark=True)
        assert got.beta == 1.0
        assert got.alpha_annual == 0.0
        assert got.r2 == 1.0
        assert got.corr == 1.0
        assert got.resid_vol_annual == 0.0

    def test_regression_path_also_gives_beta_1_alpha_0_r2_1(self) -> None:
        """**不走 ``is_benchmark`` 捷径**，让回归自己算一遍。

        ``is_benchmark=True`` 是直接返回字面量 ``(0.0, 1.0, 1.0, …)``，
        所以只断言那条分支等于断言字面量回到自己。这一条才真正验证
        「把一条序列对它自己回归」确实给出 β=1、α=0、R²=1 ——
        任何符号或错位 bug 都会在这里露出来。
        """
        y, _ = _synthetic()
        got = alpha_beta(y, y, window=126, min_obs=120, is_benchmark=False)
        assert got.beta == pytest.approx(1.0, abs=1e-12)
        assert got.alpha_annual == pytest.approx(0.0, abs=1e-12)
        assert got.r2 == pytest.approx(1.0, abs=1e-12)
        assert got.alpha_t_stat is None  # 残差恒为零 → 0/0

    def test_benchmark_branch_still_honours_min_obs(self) -> None:
        """样本不足时，基准那一行也不能断言一个完整而自信的 α/β/R²。

        初版把 ``is_benchmark`` 的短路放在样本量检查**之前**，于是首次回填那天
        （或任何基准收益 join 不上的天）QQQ 会在 0 个观测值上给出 β=1、R²=1。
        """
        idx_a = pd.RangeIndex(0, 10)
        idx_b = pd.RangeIndex(100, 110)  # 完全不相交 → join 后 0 行
        a = pd.Series(np.zeros(10), index=idx_a)
        b = pd.Series(np.zeros(10), index=idx_b)
        got = alpha_beta(a, b, window=126, min_obs=120, is_benchmark=True)
        assert got == AlphaBetaResult.empty(0)

    def test_min_obs_below_3_is_rejected_with_a_useful_message(self) -> None:
        """``ssr/(n-2)`` 在 n=2 时除零。在这里报，错误信息才能指向 config 键。"""
        y, x = _synthetic(n=10)
        with pytest.raises(ValueError, match="min_obs"):
            alpha_beta(y, x, window=0, min_obs=2)

    def test_t_stat_is_none_not_nan_not_zero(self) -> None:
        """0/0 必须写 ``None``（→ 库里 NULL → 前端 fail closed 打灰）。

        写 0 会让 ``abs(0) < 2`` 为真 —— 碰巧也是打灰，但那是**蒙对的**；
        写 NaN 则会让它变成「不打灰」，即宣称显著。
        """
        y, _ = _synthetic()
        got = alpha_beta(y, y, is_benchmark=True)
        assert got.alpha_t_stat is None

    def test_perfect_linear_relation_also_yields_none_t(self) -> None:
        """不走 ``is_benchmark`` 分支、但残差恒为 0 的情形，也必须写 None。"""
        _, x = _synthetic()
        y = 2.0 * x + 0.001
        got = alpha_beta(y, x, window=0, min_obs=10)
        assert got.beta == pytest.approx(2.0)
        assert got.r2 == pytest.approx(1.0)
        # 浮点残差是 ~1e-17 而不是精确 0，所以守卫必须是**相对**判据。
        # 用 `== 0.0` 的话这里会得到 t ≈ 3.4e16 —— 一个会被读成
        # 「极度显著」的垃圾数。这条测试当时就是这么抳出来的。
        assert got.alpha_t_stat is None


class TestR2EqualsCorrSquared:
    """``r2 == corr²`` 在简单回归下精确成立 —— 一条免费的不变式。

    它能白捡一整类符号 / 索引错误：任何把 y 和 x 弄反、
    或者算 corr 时错位一行的 bug，都会让这个恒等式破掉。
    """

    @pytest.mark.parametrize("beta", [-2.0, -0.5, 0.5, 1.0, 3.0])
    def test_identity_holds(self, beta: float) -> None:
        """这条断言**必须是真的不变式，不能是同义反复**。

        实现里 ``r2`` 由 ``1 - SSR/SST`` 独立算出、``corr`` 由 ``np.corrcoef``
        算出，两条路径互不相干 —— 所以这个恒等式能抓住符号 / 错位错误。
        （初版把 ``r2 = corr*corr`` 直接导出，那样这条断言永远不会失败。）
        """
        y, x = _synthetic(beta=beta)
        got = alpha_beta(y, x, window=0, min_obs=10)
        assert got.r2 is not None
        assert got.corr is not None
        assert got.r2 == pytest.approx(got.corr**2, abs=1e-12)

    def test_swapping_y_and_x_breaks_the_identity_check_is_meaningful(self) -> None:
        """佐证上一条不是同义反复：y/x 互换后 beta 变了，而 r2 与 corr² 仍相等
        —— 说明两条路径都跟着变，而不是其中一条抄了另一条。
        """
        y, x = _synthetic(beta=3.0)
        fwd = alpha_beta(y, x, window=0, min_obs=10)
        rev = alpha_beta(x, y, window=0, min_obs=10)
        assert fwd.beta is not None and rev.beta is not None
        assert fwd.beta != pytest.approx(rev.beta)
        assert fwd.r2 == pytest.approx(rev.r2, abs=1e-12)  # R² 对互换不变

    def test_r2_matches_independent_ssr_sst_formula(self) -> None:
        """再用 ``1 - SSR/SST`` 独立算一遍 —— 实现是由 corr 导出的，
        所以这条交叉验证不是同义反复。
        """
        y, x = _synthetic()
        got = alpha_beta(y, x, window=0, min_obs=10)
        yv, xv = y.to_numpy(), x.to_numpy()
        b = np.cov(yv, xv, ddof=1)[0, 1] / np.var(xv, ddof=1)
        a = yv.mean() - b * xv.mean()
        ssr = float(np.sum((yv - (a + b * xv)) ** 2))
        sst = float(np.sum((yv - yv.mean()) ** 2))
        assert got.r2 == pytest.approx(1 - ssr / sst, abs=1e-12)

    def test_negative_beta_still_gives_positive_r2(self) -> None:
        y, x = _synthetic(beta=-1.5)
        got = alpha_beta(y, x, window=0, min_obs=10)
        assert got.beta is not None and got.beta < 0
        assert got.r2 is not None and got.r2 > 0


class TestCoefficientsRecoverTruth:
    def test_beta_and_alpha_are_recovered(self) -> None:
        true_beta, true_alpha_d = 1.8, 0.0004
        y, x = _synthetic(n=4000, beta=true_beta, alpha_d=true_alpha_d)
        got = alpha_beta(y, x, window=0, min_obs=100)
        assert got.beta == pytest.approx(true_beta, abs=0.02)
        assert got.alpha_annual is not None
        assert got.alpha_annual == pytest.approx(true_alpha_d * TRADING_DAYS_PER_YEAR, abs=0.05)

    def test_linear_is_the_default_not_compound(self) -> None:
        """复利式把算术均值当几何均值用，高估幅度随估计值单调放大。

        0.5%/日 → 线性 +126%、复利 +252%；1.0%/日 → +252%、+1130%。
        默认必须是线性。
        """
        _, x = _synthetic(n=300)
        alpha_d = 0.005
        y = alpha_d + 1.0 * x  # 无噪声，alpha_d 精确可知
        lin = alpha_beta(y, x, window=0, min_obs=10, annualization="linear")
        cmp_ = alpha_beta(y, x, window=0, min_obs=10, annualization="compound")
        assert lin.alpha_annual == pytest.approx(alpha_d * 252, abs=1e-9)
        assert cmp_.alpha_annual == pytest.approx((1 + alpha_d) ** 252 - 1, abs=1e-9)
        assert cmp_.alpha_annual is not None and lin.alpha_annual is not None
        # 实际比值约 1.996 —— 设计文档说「约 2 倍」，这里就断言「明显更大」，
        # 不把一个只是掉到 1.996 的数字写成硬阀值。
        assert cmp_.alpha_annual > 1.9 * lin.alpha_annual

    def test_rejects_unknown_annualization(self) -> None:
        y, x = _synthetic()
        with pytest.raises(ValueError, match="annualization"):
            alpha_beta(y, x, window=0, min_obs=10, annualization="geometric")


class TestExactInterceptStandardError:
    """精确式比简化式多乘一个 ``sqrt(1 + n*x̄²/((n-1)*s_x²))``。

    这个因子在本项目的取值范围内**很小**（约 +0.09%）—— 设计文档里一度写成
    「高估 10%–50%」，那个量级是错的，已更正。所以这条测试断言的是
    「精确式与简化式确实不同，且差异就是那个因子」，而不是「差异很大」。
    """

    def test_matches_the_closed_form(self) -> None:
        y, x = _synthetic(n=126)
        got = alpha_beta(y, x, window=0, min_obs=10)
        yv, xv = y.to_numpy(), x.to_numpy()
        n = len(yv)
        b = np.cov(yv, xv, ddof=1)[0, 1] / np.var(xv, ddof=1)
        a = yv.mean() - b * xv.mean()
        s = float(np.sqrt(np.sum((yv - (a + b * xv)) ** 2) / (n - 2)))
        se = s * np.sqrt(1 / n + xv.mean() ** 2 / ((n - 1) * np.var(xv, ddof=1)))
        assert got.alpha_t_stat == pytest.approx(a / se, abs=1e-9)

    def test_differs_from_the_naive_form(self) -> None:
        y, x = _synthetic(n=126)
        got = alpha_beta(y, x, window=0, min_obs=10)
        yv, xv = y.to_numpy(), x.to_numpy()
        n = len(yv)
        b = np.cov(yv, xv, ddof=1)[0, 1] / np.var(xv, ddof=1)
        a = yv.mean() - b * xv.mean()
        s = float(np.sqrt(np.sum((yv - (a + b * xv)) ** 2) / (n - 2)))
        naive_t = a / (s / np.sqrt(n))
        assert got.alpha_t_stat is not None
        assert got.alpha_t_stat != pytest.approx(naive_t, abs=1e-12)
        # 精确 SE 更大 → 精确 |t| 更小
        assert abs(got.alpha_t_stat) < abs(naive_t)

    def test_resid_vol_uses_ddof_n_minus_2(self) -> None:
        """估了截距与斜率两个参数，所以 ``ddof = n-2``。

        用 ``n-1`` 会把标准误低估 ``sqrt((n-2)/(n-1))``，
        方向上与多重比较的问题叠加 —— 两者都在放大 |t|。
        """
        y, x = _synthetic(n=126)
        got = alpha_beta(y, x, window=0, min_obs=10)
        yv, xv = y.to_numpy(), x.to_numpy()
        n = len(yv)
        b = np.cov(yv, xv, ddof=1)[0, 1] / np.var(xv, ddof=1)
        a = yv.mean() - b * xv.mean()
        ssr = float(np.sum((yv - (a + b * xv)) ** 2))
        expected = float(np.sqrt(ssr / (n - 2)) * np.sqrt(TRADING_DAYS_PER_YEAR))
        assert got.resid_vol_annual == pytest.approx(expected, abs=1e-12)
        wrong = float(np.sqrt(ssr / (n - 1)) * np.sqrt(TRADING_DAYS_PER_YEAR))
        assert got.resid_vol_annual != pytest.approx(wrong, abs=1e-12)


class TestDegenerateResidualGuard:
    """**这一组守的是一个会被读成「极度显著」的垃圾数。**

    完美线性关系下浮点残差是 ~1e-17 而不是精确 0。初版守卫写的是
    ``ssr <= 1e-20 * sst``，比 float64 在 R² 上能分辨的最小差（1.11e-16）
    还紧四个数量级 —— 于是噪声 σ=1e-10 时 R² 在 float64 里**就是 1.0**，
    守卫却不触发，``alpha_t_stat`` 输出 1.26e8。
    而 ``abs(1.26e8) < 2`` 为假 → 那一行**不打灰** → UI 宣称统计显著。
    """

    @pytest.mark.parametrize("noise", [0.0, 1e-14, 1e-12, 1e-10])
    def test_near_perfect_fits_null_the_t_stat(self, noise: float) -> None:
        rng = np.random.default_rng(5)
        n = 126
        x = pd.Series(rng.normal(0.0005, 0.012, n))
        y = 0.001 + 2.0 * x + pd.Series(rng.normal(0, noise, n)) if noise else 0.001 + 2.0 * x
        got = alpha_beta(y, x, window=0, min_obs=10)
        assert got.alpha_t_stat is None, f"noise={noise} 时仍输出了 t={got.alpha_t_stat}"

    def test_genuine_high_r2_is_not_suppressed(self) -> None:
        """阈值放宽不能误伤真实结果。

        没有任何两只美股的 126 日收益回归能达到 R² > 1-1e-12；
        即便是杠杆 ETF 对其标的也就 0.999 量级。这条测试守住那一侧。
        """
        rng = np.random.default_rng(6)
        n = 126
        x = pd.Series(rng.normal(0.0005, 0.012, n))
        y = 0.001 + 2.0 * x + pd.Series(rng.normal(0, 0.0005, n))  # R² ≈ 0.999
        got = alpha_beta(y, x, window=0, min_obs=10)
        assert got.r2 is not None and got.r2 > 0.99
        assert got.alpha_t_stat is not None, "真实的高 R² 结果不该被抹掉"

    def test_implausible_t_is_capped_on_a_non_degenerate_fit(self) -> None:
        """**必须落在退化分支之外**，否则测的不是天花板而是上一道守卫。

        构造一个 ``SSR/SST`` 恰好**高于** 1e-12 的拟合：
        它不退化（``r2 < 1 - 1e-12``、残差波动非零），
        但截距 t 值仍有约 1e5 量级 —— 这正是单靠 R² 阈值界不住 |t| 的证据。
        """
        rng = np.random.default_rng(7)
        n = 126
        xv = rng.normal(0.0005, 0.012, n)
        fitted = 0.001 + 2.0 * xv
        resid = rng.normal(0.0, 1.0, n)
        resid -= resid.mean()
        target_ratio = 2e-12  # 高于 1e-12 的退化阈值
        sst = float(np.sum((fitted - fitted.mean()) ** 2))
        scale = float(np.sqrt(target_ratio * sst / np.sum(resid**2)))

        x = pd.Series(xv)
        y = pd.Series(fitted + scale * resid)
        got = alpha_beta(y, x, window=0, min_obs=10)

        assert got.r2 is not None and got.r2 < 1.0 - 1e-12, "必须在退化分支之外"
        assert got.resid_vol_annual is not None and got.resid_vol_annual > 0.0
        assert got.alpha_t_stat is None, "超出合理性天花板的 t 值必须写 None"

    def test_degenerate_neighbours_are_consistent(self) -> None:
        """退化时 resid_vol / r2 也要归成干净值，与 ``is_benchmark`` 分支一致 ——
        否则记录会带着 resid_vol=5e-17 与 r2=0.9999999999999996 这种邻居值。
        """
        _, x = _synthetic()
        y = 2.0 * x + 0.001
        got = alpha_beta(y, x, window=0, min_obs=10)
        assert got.resid_vol_annual == 0.0
        assert got.r2 == 1.0


class TestInsufficientSample:
    """样本不足一律写 NULL，**不写近似值**。"""

    def test_below_min_obs_is_all_none(self) -> None:
        y, x = _synthetic(n=50)
        got = alpha_beta(y, x, window=126, min_obs=120)
        assert got == AlphaBetaResult.empty(50)
        assert got.n_obs == 50

    def test_exactly_min_obs_is_computed(self) -> None:
        y, x = _synthetic(n=120)
        got = alpha_beta(y, x, window=126, min_obs=120)
        assert got.beta is not None
        assert got.n_obs == 120

    def test_zero_variance_benchmark_is_none(self) -> None:
        """基准在整个窗口里没有波动：beta 无定义，不给兜底值。"""
        n = 200
        idx = pd.RangeIndex(n)
        x = pd.Series(np.zeros(n), index=idx)
        y = pd.Series(np.random.default_rng(3).normal(0, 0.01, n), index=idx)
        got = alpha_beta(y, x, window=0, min_obs=10)
        assert got.beta is None
        assert got.n_obs == n

    def test_window_takes_the_tail(self) -> None:
        y, x = _synthetic(n=400)
        got = alpha_beta(y, x, window=126, min_obs=120)
        assert got.n_obs == 126


class TestSessionReturns:
    """**这是设计里点名的那条静默错误路径。**

    某标的缺一天时，跨缺口的那一段会被当成单日收益，量级约 √2 倍偏大，
    推高 ``Var(r_s)`` 并拖动 beta；而 ``n_obs`` 仍会读到 125，
    安全地高于 ``min_obs: 120`` —— **没有任何东西会报警**。
    """

    def test_consecutive_sessions_are_kept(self) -> None:
        prices, ords = _series([100.0, 101.0, 102.0], [1, 2, 3])
        rets = session_returns(prices, ords)
        assert np.isnan(rets.iloc[0])  # 第一根没有前一根
        assert rets.iloc[1] == pytest.approx(0.01)
        assert rets.iloc[2] == pytest.approx(102 / 101 - 1)

    def test_gap_return_is_dropped(self) -> None:
        """ordinal 从 1 跳到 3：那一段跨了两个 session，必须丢掉。"""
        prices, ords = _series([100.0, 110.0, 111.0], [1, 3, 4])
        rets = session_returns(prices, ords)
        assert np.isnan(rets.iloc[1]), "跨缺口的收益必须被剔除，而不是当成单日收益"
        assert rets.iloc[2] == pytest.approx(111 / 110 - 1)

    def test_adjacency_is_by_ordinal_not_by_date(self) -> None:
        """判据必须是 ``ordinal`` 差 1，不是日期差 1。

        只有 date 的话没人知道感恩节；而用工作日差替代会把交易所假日
        误判为连续。这里用一个「日期差 4 天但 session 相邻」的长周末来证明。
        """
        idx = pd.to_datetime(["2026-01-02", "2026-01-06"])  # 中间隔了周末+假日
        prices = pd.Series([100.0, 102.0], index=idx)
        ords = pd.Series([10, 11], index=idx, dtype="int64")  # 但 session 是相邻的
        rets = session_returns(prices, ords)
        assert rets.iloc[1] == pytest.approx(0.02), "session 相邻就该保留，不看日历天数"

    def test_gap_inflates_beta_if_not_dropped(self) -> None:
        """把「不剔除」的后果量化出来，证明这条规则不是洁癖。"""
        rng = np.random.default_rng(11)
        n = 200
        x = rng.normal(0, 0.01, n)
        y = 1.0 * x + rng.normal(0, 0.002, n)
        idx = pd.RangeIndex(n)
        ords = pd.Series(range(1, n + 1), index=idx, dtype="int64")
        # 制造一个缺口：把第 100 根的 ordinal 推远，等价于那天之后缺了几个 session
        ords_gapped = ords.copy()
        ords_gapped.iloc[100:] = ords_gapped.iloc[100:] + 5

        prices = pd.Series(100 * np.exp(np.cumsum(y)), index=idx)
        clean = session_returns(prices, ords)
        gapped = session_returns(prices, ords_gapped)
        # 干净序列保留 n-1 个收益；有缺口的少一个（那个跨缺口的被剔除）
        assert clean.notna().sum() == n - 1
        assert gapped.notna().sum() == n - 2

    def test_index_mismatch_is_loud(self) -> None:
        prices = pd.Series([1.0, 2.0], index=[0, 1])
        ords = pd.Series([1, 2], index=[5, 6], dtype="int64")
        with pytest.raises(ValueError, match="同索引"):
            session_returns(prices, ords)

    def test_zero_previous_price_is_dropped(self) -> None:
        prices, ords = _series([0.0, 100.0, 101.0], [1, 2, 3])
        rets = session_returns(prices, ords)
        assert np.isnan(rets.iloc[1]), "除以 0 应剔除而不是产出 inf"

    def test_bad_current_price_is_dropped_too(self) -> None:
        """**分子也要查，不只是分母。**

        只查分母时 ``[100, 0, 101, -2, 102]`` 会产出 ``-1.0`` 与 ``-1.02``
        这种**看起来完全正常**的日收益，静悄悄进入 OLS。
        而它们是**有限值**，所以 §9.1.3 的 NaN sanitizer 也拦不住。
        """
        prices, ords = _series([100.0, 0.0, 101.0, -2.0, 102.0], [1, 2, 3, 4, 5])
        rets = session_returns(prices, ords)
        assert rets.isna().all(), f"坏价格两侧的收益都该剔除，实际 {rets.tolist()}"


class TestNeverEmitsNonFinite:
    """NaN / inf 绝不进入写库路径（§9.1.3）。

    首次回填每个标的约 126 个前导行全是空值；若它们以 NaN 形态进到 JSON，
    整批写入会失败，而失败原因一点都不明显。
    """

    def test_all_fields_are_none_or_finite(self) -> None:
        for n in (5, 50, 120, 300):
            y, x = _synthetic(n=n)
            got = alpha_beta(y, x, window=126, min_obs=120)
            for name, value in vars(got).items():
                if name == "n_obs" or value is None:
                    continue
                assert np.isfinite(value), f"{name} 不是有限值：{value}"
