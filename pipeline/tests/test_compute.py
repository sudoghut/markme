"""``pipeline/compute.py`` 的测试（§3.0 规则 2、§3.3 双闸门、§3.5(3)）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pytest

from pipeline.compute import EVENT_COLUMNS, build_windows, compute_metrics, compute_strength
from pipeline.config import load_config
from pipeline.metrics.events import EventDistances

CFG = load_config()
DAYS = [date(2024, 1, 1) + timedelta(days=i) for i in range(400)]
ORD = {d: i + 1 for i, d in enumerate(DAYS)}


def _prices(symbols: dict[str, list[float]]) -> pd.DataFrame:
    frames = []
    for sym, closes in symbols.items():
        frames.append(
            pd.DataFrame(
                {
                    "symbol": sym,
                    "date": DAYS[: len(closes)],
                    "adj_close": closes,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _walk(n: int, seed: int = 0) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(100 * np.cumprod(1 + rng.normal(0.0005, 0.01, n)))


class TestWindows:
    def test_only_session_dates_survive(self) -> None:
        """§9.1.4 第 4 条：即便有陈旧行残留在价格表里，它也进不了计算窗口。"""
        df = _prices({"A": _walk(10)})
        partial = {DAYS[i]: i + 1 for i in (0, 1, 2)}
        w = build_windows(df, partial)
        assert w["A"].n_bars == 3
        assert w["A"].ordinals == (1, 2, 3)

    def test_an_empty_frame_yields_nothing(self) -> None:
        assert build_windows(pd.DataFrame(), ORD) == {}


class TestTheTwoGates:
    """§3.3：``min_bars`` 说「出不出值」，``provisional_below`` 说「信不信得过」。

    §3.3 写这两条就是为了防止它们被混用。
    """

    def test_below_min_bars_is_null_not_a_value(self) -> None:
        w = build_windows(_prices({"A": _walk(30)}), ORD)
        rows = compute_metrics(CFG, w)
        # RSI(14) 的 min_bars 是 15 → 前 14 行必须是 None
        rsi = [r["rsi_14"] for r in rows]
        assert all(v is None for v in rsi[:14])
        assert rsi[14] is not None

    def test_ema60_is_null_until_its_own_min_bars(self) -> None:
        w = build_windows(_prices({"A": _walk(80)}), ORD)
        rows = compute_metrics(CFG, w)
        ema = [r["ema_60"] for r in rows]
        assert all(v is None for v in ema[:59])
        assert ema[59] is not None

    def test_a_short_history_is_flagged_provisional_but_still_produces_values(self) -> None:
        w = build_windows(_prices({"A": _walk(100)}), ORD)
        rows = compute_metrics(CFG, w)
        assert rows[-1]["rsi_14"] is not None, "软闸门下仍然出值"
        assert "rsi_14" in (rows[-1]["provisional_metrics"] or []), "但要标灰"

    def test_a_long_history_is_not_provisional(self) -> None:
        w = build_windows(_prices({"A": _walk(400)}), ORD)
        rows = compute_metrics(CFG, w)
        assert rows[-1]["provisional_metrics"] == []

    def test_not_null_defaulted_columns_are_never_written_as_null(self) -> None:
        """``extra`` 与 ``provisional_metrics`` 是 ``not null default '{}'``。

        **「有默认值」只在*不提供该列*时生效** —— 提供了 NULL 就是 NULL，
        照样撞 NOT NULL。第一次真实回填就栽在这里，而纯逻辑测试看不出来，
        所以把它钉成一条断言。
        """
        w = build_windows(_prices({"A": _walk(30)}), ORD)
        for r in compute_metrics(CFG, w):
            assert r["extra"] is not None
            assert r["provisional_metrics"] is not None


class TestEventColumns:
    """§3.5(3)：只写最新一行，其余行**显式** NULL。"""

    def test_only_the_latest_row_carries_event_columns(self) -> None:
        w = build_windows(_prices({"A": _walk(30)}), ORD)
        ed = EventDistances(days_to_next_earnings=12, next_earnings_date=DAYS[41])
        rows = compute_metrics(CFG, w, event_distances={"A": ed}, latest_date=DAYS[29])
        assert rows[-1]["days_to_next_earnings"] == 12
        assert all(r["days_to_next_earnings"] is None for r in rows[:-1])

    def test_history_rows_are_explicitly_null_not_absent(self) -> None:
        """「不管它」和「写 NULL」不是一回事：upsert 时前者会保留残值。

        §7.3.1 说得很清楚：非最新行的八个事件列要被**显式写成 NULL**。
        """
        w = build_windows(_prices({"A": _walk(5)}), ORD)
        rows = compute_metrics(CFG, w)
        for r in rows:
            for col in EVENT_COLUMNS:
                assert col in r, f"{col} 必须出现在行里（值为 None），而不是缺席"
                assert r[col] is None

    def test_no_event_data_means_all_null_even_on_the_latest_row(self) -> None:
        w = build_windows(_prices({"A": _walk(5)}), ORD)
        rows = compute_metrics(CFG, w, event_distances={}, latest_date=DAYS[4])
        assert all(rows[-1][c] is None for c in EVENT_COLUMNS)


class TestAlphaBeta:
    def test_the_benchmark_against_itself_is_beta_one(self) -> None:
        """§11 M2 的验收标准之一，这里确认接线没接反。"""
        w = build_windows(_prices({"QQQ": _walk(200, seed=1)}), ORD)
        rows = compute_metrics(CFG, w)
        last = rows[-1]
        assert last["beta"] == pytest.approx(1.0, abs=1e-9)
        assert last["alpha_annual"] == pytest.approx(0.0, abs=1e-9)
        assert last["r2"] == pytest.approx(1.0, abs=1e-9)

    def test_alpha_beta_only_lands_on_the_last_row(self) -> None:
        """§3.4：它是窗口末端的一个标量，不是逐行序列。"""
        w = build_windows(_prices({"QQQ": _walk(200, seed=1)}), ORD)
        rows = compute_metrics(CFG, w)
        assert all(r["beta"] is None for r in rows[:-1])
        assert rows[-1]["beta"] is not None

    def test_without_a_benchmark_alpha_beta_is_null_not_an_error(self) -> None:
        w = build_windows(_prices({"A": _walk(200)}), ORD)
        rows = compute_metrics(CFG, w)
        assert rows[-1]["beta"] is None


class TestStrength:
    def _rows(self, scores: dict[str, float | None]) -> list[dict[str, Any]]:
        return [
            {"symbol": s, "date": DAYS[5], "mom_20": v, "rsi_14": 50.0} for s, v in scores.items()
        ]

    POOL: ClassVar[dict[str, str]] = {"QQQ": "etf", "A": "stock", "B": "stock", "C": "stock"}

    def test_the_etf_is_excluded_from_a_stocks_pool(self) -> None:
        """§12 #2 / §4.1：排名池是 16 只个股，QQQ 是基准不是候选。"""
        rows = compute_strength(
            CFG,
            self._rows({"QQQ": 0.9, "A": 0.3, "B": 0.2, "C": 0.1}),
            day=DAYS[5],
            pool=self.POOL,
        )
        assert {r["symbol"] for r in rows} == {"A", "B", "C"}

    def test_all_ranks_are_stored_not_just_the_top_three(self) -> None:
        """§4.4：每天存**全部**名次，前端只显示前 3。"""
        rows = compute_strength(
            CFG, self._rows({"A": 0.3, "B": 0.2, "C": 0.1}), day=DAYS[5], pool=self.POOL
        )
        assert sorted(r["rank"] for r in rows) == [1, 2, 3]
        assert sum(r["in_top_n"] for r in rows) == min(CFG.strength.top_n, 3)

    def test_a_null_score_gets_no_row_at_all(self) -> None:
        """§4.1 规则 ③：有限值不足 N 个时，**只有这些有限的能上榜**。

        M2 的 ``rank_pool`` 把非有限分的标的整行丢掉，而不是给一个空名次。
        这条钉住那个契约 —— §9.3.2 的「榜单行数 == 排名池大小」那条不变式
        必须据此减去它们，否则**加标的那一天**会对一个健康的库报警。
        """
        rows = compute_strength(
            CFG, self._rows({"A": None, "B": 0.2, "C": 0.1}), day=DAYS[5], pool=self.POOL
        )
        assert {r["symbol"] for r in rows} == {"B", "C"}
        assert sorted(r["rank"] for r in rows) == [1, 2]

    def test_the_stored_metadata_comes_from_config(self) -> None:
        rows = compute_strength(CFG, self._rows({"A": 0.3}), day=DAYS[5], pool=self.POOL)
        assert rows[0]["score_metric"] == CFG.strength.score_metric
        assert rows[0]["rank_pool"] == CFG.strength.rank_pool
        assert rows[0]["benchmark"] == CFG.universe.benchmark

    def test_another_day_produces_nothing(self) -> None:
        assert compute_strength(CFG, self._rows({"A": 0.3}), day=DAYS[6], pool=self.POOL) == []
