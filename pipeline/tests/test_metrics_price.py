"""价格派生指标的 golden-value 测试（§3.1 / §3.2 / §4.1）。

**期望值是手算的，不是跑一遍实现再抄回来的** —— 后者只能证明代码没变，
证明不了代码是对的。用 ``period=3`` 是为了让手算过程能写进注释接受复核。
"""

from __future__ import annotations

import math
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from pipeline.metrics.ema import close_vs_ema_pct, ema, ema_slope
from pipeline.metrics.momentum import momentum
from pipeline.metrics.rsi import rsi_wilder


def _s(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.RangeIndex(len(values)), dtype=float)


class TestRsiGoldenValues:
    """手算过程（period=3，便于逐步核对）：

    价格   10    11    10.5   12     11     11.5
    delta        +1.0  -0.5   +1.5   -1.0   +0.5
    gain         1.0   0.0    1.5    0.0    0.5
    loss         0.0   0.5    0.0    1.0    0.0

    播种（下标 3，即第 4 根收盘价 12）：
      AvgGain = (1.0 + 0.0 + 1.5) / 3 = 0.8333333
      AvgLoss = (0.0 + 0.5 + 0.0) / 3 = 0.1666667
      RS = 5.0 → RSI = 100 - 100/6 = 83.3333333
    下标 4（价 11，gain=0 loss=1）：
      AvgGain = (0.8333333*2 + 0.0)/3 = 0.5555556
      AvgLoss = (0.1666667*2 + 1.0)/3 = 0.4444444
      RS = 1.25 → RSI = 100 - 100/2.25 = 55.5555556
    下标 5（价 11.5，gain=0.5 loss=0）：
      AvgGain = (0.5555556*2 + 0.5)/3 = 0.5370370
      AvgLoss = (0.4444444*2 + 0.0)/3 = 0.2962963
      RS = 1.8125 → RSI = 100 - 100/2.8125 = 64.4444444
    """

    PRICES: ClassVar[list[float]] = [10.0, 11.0, 10.5, 12.0, 11.0, 11.5]

    def test_golden(self) -> None:
        got = rsi_wilder(_s(self.PRICES), period=3)
        assert got.iloc[:3].isna().all(), "播种前必须是 NaN"
        assert got.iloc[3] == pytest.approx(83.3333333, abs=1e-6)
        assert got.iloc[4] == pytest.approx(55.5555556, abs=1e-6)
        assert got.iloc[5] == pytest.approx(64.4444444, abs=1e-6)

    def test_first_value_lands_on_bar_period_plus_1(self) -> None:
        """``delta_0`` 不存在，所以第一个 RSI 落在第 ``period+1`` 根收盘价上。

        这是每次 RSI 重实现都会犯的经典 off-by-one；写成断言而不是注释。
        """
        for period in (3, 14):
            prices = _s([10.0 + i for i in range(period + 5)])
            got = rsi_wilder(prices, period=period)
            first = got.first_valid_index()
            assert first == period, f"period={period} 的第一个值应在下标 {period}"

    def test_is_not_ewm_span(self) -> None:
        """``ewm(span=14)`` 是 alpha=2/15，**不是** Wilder 的 1/14。

        这两者差得足够远，一条断言就能把「顺手用了 span」挡住。
        """
        rng = np.random.default_rng(0)
        prices = _s(list(100 + rng.standard_normal(300).cumsum()))
        ours = rsi_wilder(prices, period=14)

        delta = prices.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        span_rs = gain.ewm(span=14, adjust=False).mean() / loss.ewm(span=14, adjust=False).mean()
        span_rsi = 100 - 100 / (1 + span_rs)

        assert not np.isclose(ours.iloc[-1], span_rsi.iloc[-1], atol=1e-3)

    def test_converges_to_ewm_alpha_form_on_long_series(self) -> None:
        """与 ``ewm(alpha=1/14, adjust=False)`` 的差异按 ``(13/14)^k`` 衰减。

        两者**不是同一个函数**（播种不同），所以这条交叉验证只在长序列上成立 ——
        短夹具上对标它会得到一个看起来像公式错、实际是播种错的失败。
        """
        rng = np.random.default_rng(1)
        prices = _s(list(100 + rng.standard_normal(600).cumsum()))
        ours = rsi_wilder(prices, period=14)

        delta = prices.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        rs = (
            gain.ewm(alpha=1 / 14, adjust=False).mean()
            / loss.ewm(alpha=1 / 14, adjust=False).mean()
        )
        ewm_rsi = 100 - 100 / (1 + rs)

        assert ours.iloc[-1] == pytest.approx(ewm_rsi.iloc[-1], abs=1e-6)


class TestRsiDegenerateCases:
    """三种退化情形（§3.1）。最后一种是设计里专门点名的。"""

    def test_all_gains_gives_100(self) -> None:
        got = rsi_wilder(_s([1.0, 2.0, 3.0, 4.0, 5.0]), period=3)
        assert got.iloc[3] == 100.0

    def test_all_losses_gives_0(self) -> None:
        got = rsi_wilder(_s([5.0, 4.0, 3.0, 2.0, 1.0]), period=3)
        assert got.iloc[3] == 0.0

    def test_flat_series_gives_nan_not_50(self) -> None:
        """**连续平盘 / 停牌 → NaN，绝不是 50。**

        一个合成的 50 是在「没有信息」的地方画一个中性且自信的数字 ——
        与 §10.4「绝不用 0 或上一日的值冒充」是同一类错误。
        """
        got = rsi_wilder(_s([7.0] * 8), period=3)
        assert got.iloc[3:].isna().all()

    def test_too_short_is_all_nan(self) -> None:
        got = rsi_wilder(_s([1.0, 2.0, 3.0]), period=3)
        assert got.isna().all()
        assert len(got) == 3  # 等长，索引不变

    def test_rejects_bad_period(self) -> None:
        with pytest.raises(ValueError, match="period"):
            rsi_wilder(_s([1.0, 2.0]), period=0)


class TestNaNPricesMustNotBecomeFlatSessions:
    """**一个缺失价格绝不能被伪造成「平盘日」。**

    ``NaN > 0`` 与 ``NaN < 0`` 都是 False，所以一个朴素的 ``np.where``
    会把缺失价格变成 ``gain=0, loss=0`` —— 恰好是 §3.1 明令禁止的「用 0 冒充」。
    而 Wilder 是 IIR 递归，**污染永不消散**。

    这不是假想：§7.3.1 的批量 ``yf.download`` 在某标的缺一天时返回的就是 NaN。
    """

    CLEAN: ClassVar[list[float]] = [10.0, 11.0, 10.5, 12.0, 11.0, 11.5, 12.0, 13.0]

    def test_nan_poisons_instead_of_fabricating(self) -> None:
        holed = list(self.CLEAN)
        holed[4] = float("nan")
        got = rsi_wilder(_s(holed), period=3)
        # 缺口之后全是 NaN —— 不是一串看起来正常的数字
        assert got.iloc[4:].isna().all(), "缺失价格必须把其后的 RSI 也置为 NaN"

    def test_the_fabricated_value_would_have_been_plausible(self) -> None:
        """把「如果不修会怎样」钉下来：伪造出的值毫不可疑，正因如此才危险。"""
        clean = rsi_wilder(_s(self.CLEAN), period=3)
        assert clean.iloc[4] == pytest.approx(55.5555556, abs=1e-6)
        # 若把 NaN 当平盘（gain=0,loss=0），那一格会读出 83.33 —— 一个完全正常的 RSI

    def test_ema_also_propagates_nan(self) -> None:
        holed = list(self.CLEAN)
        holed[3] = float("nan")
        got = ema(_s(holed), period=3)
        # 下标 2 是播种，由 bar 0–2 算出 —— 那三根都不含缺口，所以它本来就该有值。
        # 污染从下标 3（缺口本身）开始，而且因为是递归，往后永不恢复。
        assert got.iloc[2] == pytest.approx(10.5)
        assert got.iloc[3:].isna().all()

    def test_momentum_propagates_nan(self) -> None:
        holed = [1.0, 2.0, float("nan"), 4.0, 5.0]
        got = momentum(_s(holed), period=2)
        assert math.isnan(got.iloc[4])


class TestEmaGoldenValues:
    """手算过程（period=3 → alpha = 2/4 = 0.5）：

    价格   10    11    10.5    12      11
    播种（下标 2）= (10 + 11 + 10.5)/3 = 10.5
    下标 3 = 0.5*12 + 0.5*10.5  = 11.25
    下标 4 = 0.5*11 + 0.5*11.25 = 11.125
    """

    def test_golden(self) -> None:
        got = ema(_s([10.0, 11.0, 10.5, 12.0, 11.0]), period=3)
        assert got.iloc[:2].isna().all()
        assert got.iloc[2] == pytest.approx(10.5)
        assert got.iloc[3] == pytest.approx(11.25)
        assert got.iloc[4] == pytest.approx(11.125)

    def test_seed_lands_on_bar_period_minus_1(self) -> None:
        got = ema(_s([1.0] * 10), period=60)
        assert got.isna().all(), "不足 period 根时全是 NaN"
        got2 = ema(_s([float(i) for i in range(60)]), period=60)
        assert got2.first_valid_index() == 59

    def test_constant_series_equals_the_constant(self) -> None:
        """恒定序列的 EMA 恒等于那个常数（在浮点精度内）。

        递归会累积微小误差，所以这里用 approx 而不是精确相等 ——
        用精确相等只会得到一条反复碎掉的测试，而不是更强的保证。
        """
        got = ema(_s([42.0] * 100), period=60).dropna()
        assert got.to_numpy() == pytest.approx(42.0, abs=1e-9)


class TestEmaDerived:
    def test_close_vs_ema_pct(self) -> None:
        prices = _s([10.0, 11.0, 10.5, 12.0, 11.0])
        e = ema(prices, period=3)
        pct = close_vs_ema_pct(prices, e)
        # 下标 3：12 / 11.25 - 1 = 0.0666667
        assert pct.iloc[3] == pytest.approx(12 / 11.25 - 1)

    def test_zero_ema_gives_nan_not_inf(self) -> None:
        """无穷大的百分比没有可读含义，而且 ``dim_when`` 会把它判成打灰。"""
        pct = close_vs_ema_pct(_s([1.0, 2.0]), _s([0.0, 0.0]))
        assert pct.isna().all()

    def test_ema_slope(self) -> None:
        e = _s([10.0, 11.0, 12.0, 13.0])
        slope = ema_slope(e, lag=2)
        assert math.isnan(slope.iloc[1])
        assert slope.iloc[2] == pytest.approx(12 / 10 - 1)


class TestAdjustedCloseIsTheOnlyBasis:
    """§3.0 规则 1：派生列的分子分母必须同源，都用 ``adj_close``。

    M0 已实测确认 yfinance 的原始 ``Close`` **已做拆股调整**，
    所以两列真正的差是**累计分红调整** —— 量级小、不扎眼，
    因此比 10× 断崖更危险：它足以把「距 EMA60 +0.8%」变成「-1.5%」
    而不会让任何人起疑。这两条测试分别守住这两种情形。
    """

    def test_split_series_stays_continuous(self) -> None:
        """合成一条**真的有 2:1 拆股**的序列，断言派生列跨拆股日连续。

        初版这里写的是一条 ``+1/天`` 的线性斜坡 —— 里面根本没有拆股，
        断言的是「平滑的斜坡是平滑的」，§11 那条验收标准等于没有测试。
        这一版：第 80 根之后价格砍半（拆股），``adj`` 是回溯复权后的连续序列，
        ``raw`` 是未复权的原始序列（拆股日有断崖）。
        """
        pre = [100.0 + i for i in range(80)]  # 100 … 179
        post_raw = [(179.0 + 1 + i) / 2 for i in range(20)]  # 拆股后：90, 90.5 …
        raw = _s(pre + post_raw)
        # 回溯复权：把拆股前的价格也除以 2，于是整条序列连续
        adj = _s([p / 2 for p in pre] + post_raw)

        # 先证明这个夹具**确实含有一次拆股** —— 否则测试又会退化成测斜坡
        raw_jump = raw.iloc[80] / raw.iloc[79] - 1
        assert raw_jump < -0.45, f"夹具里必须真的有拆股断崖，实际 {raw_jump:.3f}"

        e = ema(adj, period=60)
        correct = close_vs_ema_pct(adj, e).dropna()
        assert correct.diff().abs().max() < 0.01, "复权序列上派生列必须跨拆股日连续"

        # 而用未复权的 raw 当分子会读出一道断崖 —— 这正是 §3.0 规则 1 要防的
        wrong = close_vs_ema_pct(raw, e).dropna()
        assert wrong.diff().abs().max() > 0.5, "未复权分子必须暴露出断崖"

    def test_mixing_raw_close_with_adjusted_ema_is_visibly_wrong(self) -> None:
        """把未复权的 ``close`` 当分子会读出错误的百分比。

        这条测试存在的意义是**把那个错误固定下来**：如果哪天有人「顺手」
        把分子换成 ``close``，它会立刻变红，而不是产出一个看起来正常的数字。
        """
        n = 100
        raw_close = _s([100.0] * n)
        # 分红调整让 adj_close 系统性低于 close 约 2%（真实量级更小，这里放大以便断言）
        adj_close = _s([98.0] * n)

        e = ema(adj_close, period=60)
        correct = close_vs_ema_pct(adj_close, e).dropna()
        wrong = close_vs_ema_pct(raw_close, e).dropna()

        assert correct.abs().max() == pytest.approx(0.0, abs=1e-12)
        assert wrong.iloc[-1] == pytest.approx(100 / 98 - 1)
        assert abs(wrong.iloc[-1] - correct.iloc[-1]) > 0.02


class TestMomentum:
    def test_golden(self) -> None:
        prices = _s([10.0, 11.0, 12.0, 15.0])
        got = momentum(prices, period=2)
        assert got.iloc[:2].isna().all()
        assert got.iloc[2] == pytest.approx(12 / 10 - 1)
        assert got.iloc[3] == pytest.approx(15 / 11 - 1)

    def test_needs_period_plus_1_bars(self) -> None:
        got = momentum(_s([1.0] * 20), period=20)
        assert got.isna().all()
        got2 = momentum(_s([1.0] * 21), period=20)
        assert got2.notna().sum() == 1

    def test_zero_denominator_gives_nan(self) -> None:
        got = momentum(_s([0.0, 0.0, 5.0]), period=2)
        assert math.isnan(got.iloc[2])
