"""``pipeline/fetch.py`` 的测试（§3.0 规则 3/4、§7.2 闸门 4、§7.3.1）。

§7.2 闸门 4 明确要求：**M4 必须有一个跑通整窗 Stooq 降级的测试，
否则这条分支永远没被执行过**。那条在 :class:`TestWholeWindowDegradation` 里。
"""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from pipeline.fetch import (
    SanityIssue,
    check_sanity,
    cross_source_gap,
    fetch_window,
    restrict_to_sessions,
    stooq_frame,
    yfinance_frame,
)
from pipeline.sessions import Session
from pipeline.throttle import RequestBudget

START, END = date(2024, 6, 3), date(2024, 6, 7)
DAYS = [date(2024, 6, d) for d in (3, 4, 5, 6, 7)]


def _budget(**kw: Any) -> RequestBudget:
    opts: dict[str, Any] = {
        "max_requests": 50,
        "interval_seconds": 0,
        "retry_max_attempts": 1,
        "sleep": lambda _s: None,
        "monotonic": lambda: 0.0,
    }
    opts.update(kw)
    return RequestBudget(**opts)


def _yf_raw(prices: dict[str, list[float | None]]) -> pd.DataFrame:
    """造一个 yfinance 形状的两级列 DataFrame。"""
    cols, data = [], {}
    for sym, closes in prices.items():
        for field_name in ("Adj Close", "Close", "High", "Low", "Open", "Volume"):
            cols.append((field_name, sym))
            if field_name == "Volume":
                vals: list[float | None] = [1e6 if c is not None else None for c in closes]
            elif field_name == "High":
                vals = [c * 1.01 if c is not None else None for c in closes]
            elif field_name in ("Low", "Adj Close"):
                vals = [c * 0.99 if c is not None else None for c in closes]
            else:
                vals = list(closes)
            data[(field_name, sym)] = vals
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in DAYS])
    return pd.DataFrame(data, index=idx, columns=pd.MultiIndex.from_tuples(cols))


class TestYfinanceAdapter:
    def test_auto_adjust_false_is_always_passed(self) -> None:
        """§3.0 规则 4：不传它，``Close`` 就是复权价且没有 ``Adj Close`` 列 ——
        两个列会被写入同一个数字，「最新价」对任何分红股都对不上券商软件。"""
        seen: dict[str, Any] = {}

        def fake(symbols: list[str], **kw: Any) -> pd.DataFrame:
            seen.update(kw)
            return _yf_raw({"AAPL": [100.0] * 5})

        yfinance_frame(["AAPL"], START, END, download=fake)
        assert seen["auto_adjust"] is False
        assert seen["threads"] is False, "§7.3.1：串行，不并发"

    def test_end_is_treated_as_inclusive(self) -> None:
        """yfinance 的 ``end`` 是开区间。不 +1 天会静默丢掉**今天那一行**，
        而闸门 3 会把它报成 stale_vendor —— 一个看起来像供应商故障的自伤。"""
        seen: dict[str, Any] = {}

        def fake(symbols: list[str], **kw: Any) -> pd.DataFrame:
            seen.update(kw)
            return _yf_raw({"AAPL": [100.0] * 5})

        yfinance_frame(["AAPL"], START, END, download=fake)
        assert seen["end"] == (END + timedelta(days=1)).isoformat()

    def test_an_all_nan_block_is_not_data(self) -> None:
        """**实测：抓不到的 ticker 不抛异常，只返回一整块 NaN。**

        把它当数据写下去，``adj_close`` 的 NOT NULL 会让当天整批写入失败，
        而原因一点都不明显。调用方要知道的是「这个标的没拿到」，
        好去走整窗降级。
        """
        raw = _yf_raw({"AAPL": [100.0] * 5, "ZZZZ": [None] * 5})
        out = yfinance_frame(["AAPL", "ZZZZ"], START, END, download=lambda *a, **k: raw)
        assert set(out["symbol"]) == {"AAPL"}

    def test_columns_are_normalised(self) -> None:
        out = yfinance_frame(
            ["AAPL"], START, END, download=lambda *a, **k: _yf_raw({"AAPL": [100.0] * 5})
        )
        assert list(out.columns) == [
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "source",
        ]
        assert (out["source"] == "yfinance").all()
        assert out["close"].notna().all() and out["adj_close"].notna().all()

    def test_empty_symbol_list_short_circuits(self) -> None:
        called = False

        def fake(*a: Any, **k: Any) -> pd.DataFrame:
            nonlocal called
            called = True
            return pd.DataFrame()

        assert yfinance_frame([], START, END, download=fake).empty
        assert not called, "没有标的就不该发请求"


class TestStooqAdapter:
    CSV = (
        "Date,Open,High,Low,Close,Volume\n"
        "2024-06-03,99,101,98,100,1000\n"
        "2024-06-04,100,102,99,101,1100\n"
    )

    def test_close_is_always_null(self) -> None:
        """§3.0 规则 3：Stooq 的价格本身已复权，且没有独立的复权列。

        同时写进两列会**一次坏三件事**：「最新价」对每只分红股悄悄变成复权价；
        ``adj_factor`` 恒等于 1.0 让复权因子探测器失明；
        §3.0 规则 4 那条 CI 不变式变成「测哪个源跑了」。
        """
        out = stooq_frame("AAPL", START, END, fetch_csv=lambda *a: self.CSV)
        assert out["close"].isna().all()
        assert out["adj_close"].tolist() == [100.0, 101.0]
        assert (out["source"] == "stooq").all()

    def test_it_clips_to_the_requested_window(self) -> None:
        csv = self.CSV + "2024-07-01,1,1,1,1,1\n"
        out = stooq_frame("AAPL", START, END, fetch_csv=lambda *a: csv)
        assert out["date"].max() == date(2024, 6, 4)

    def test_empty_response_is_an_empty_frame(self) -> None:
        assert stooq_frame("AAPL", START, END, fetch_csv=lambda *a: "").empty
        assert stooq_frame("AAPL", START, END, fetch_csv=lambda *a: "no,such,cols\n1,2,3").empty


class TestWholeWindowDegradation:
    """§7.2 闸门 4 点名要求的那条测试。

    没有它，这条**设计好的**降级路径永远没被执行过 —— 而它恰好是
    「真出事那一天」才会跑的代码。
    """

    def test_a_missing_symbol_is_refetched_whole_window_from_stooq(self) -> None:
        raw = _yf_raw({"AAPL": [100.0] * 5, "MSFT": [None] * 5})
        csv = "Date,Open,High,Low,Close,Volume\n" + "".join(
            f"{d},99,101,98,{100 + i},1000\n" for i, d in enumerate(DAYS)
        )
        out = fetch_window(
            ["AAPL", "MSFT"],
            START,
            END,
            _budget(),
            yf_frame=lambda s, a, b: yfinance_frame(s, a, b, download=lambda *x, **k: raw),
            stooq=lambda sym, a, b: stooq_frame(sym, a, b, fetch_csv=lambda *x: csv),
        )
        assert out.degraded == ("MSFT",)
        assert out.missing == ()
        assert out.per_symbol_source == {"AAPL": "yfinance", "MSFT": "stooq"}
        # **整窗**，不是补一行
        assert len(out.frame[out.frame["symbol"] == "MSFT"]) == len(DAYS)

    def test_each_symbol_has_exactly_one_source_in_the_window(self) -> None:
        """§3.0 规则 3 的运行时断言。

        单行拼接会让接缝两侧的日收益各自错一整个累计复权差，
        并污染其后 126 个交易日的 beta / resid_vol / alpha t 值。
        这里断言的是**结构上不可能**发生那件事。
        """
        raw = _yf_raw({"AAPL": [100.0] * 5, "MSFT": [None] * 5})
        csv = "Date,Open,High,Low,Close,Volume\n" + "".join(
            f"{d},99,101,98,100,1000\n" for d in DAYS
        )
        out = fetch_window(
            ["AAPL", "MSFT"],
            START,
            END,
            _budget(),
            yf_frame=lambda s, a, b: yfinance_frame(s, a, b, download=lambda *x, **k: raw),
            stooq=lambda sym, a, b: stooq_frame(sym, a, b, fetch_csv=lambda *x: csv),
        )
        assert out.sources_are_unique_per_symbol()

    def test_a_symbol_missing_from_both_sources_is_reported_not_faked(self) -> None:
        raw = _yf_raw({"AAPL": [100.0] * 5, "MSFT": [None] * 5})
        out = fetch_window(
            ["AAPL", "MSFT"],
            START,
            END,
            _budget(),
            yf_frame=lambda s, a, b: yfinance_frame(s, a, b, download=lambda *x, **k: raw),
            stooq=lambda sym, a, b: pd.DataFrame(),
        )
        assert out.missing == ("MSFT",)
        assert "MSFT" not in out.per_symbol_source

    def test_a_stooq_exception_is_a_missing_symbol_not_a_crash(self) -> None:
        raw = _yf_raw({"AAPL": [100.0] * 5, "MSFT": [None] * 5})

        def boom(sym: str, a: date, b: date) -> pd.DataFrame:
            raise RuntimeError("Stooq HTTP 503")

        out = fetch_window(
            ["AAPL", "MSFT"],
            START,
            END,
            _budget(),
            yf_frame=lambda s, a, b: yfinance_frame(s, a, b, download=lambda *x, **k: raw),
            stooq=boom,
        )
        assert out.missing == ("MSFT",)
        assert set(out.frame["symbol"]) == {"AAPL"}

    def test_the_batch_is_one_request(self) -> None:
        """§7.3.1：17 个标的**一次请求**拿完。减少请求数是最有效的礼貌。"""
        b = _budget()
        raw = _yf_raw({s: [100.0] * 5 for s in ("AAPL", "MSFT", "NVDA")})
        fetch_window(
            ["AAPL", "MSFT", "NVDA"],
            START,
            END,
            b,
            yf_frame=lambda s, a, bb: yfinance_frame(s, a, bb, download=lambda *x, **k: raw),
            stooq=lambda *a: pd.DataFrame(),
        )
        assert b.used == 1


class TestSanityGate:
    def _frame(self, source: str, closes: list[float], **over: Any) -> pd.DataFrame:
        n = len(closes)
        df = pd.DataFrame(
            {
                "symbol": ["X"] * n,
                "date": DAYS[:n],
                "open": closes,
                "high": [c * 1.01 for c in closes],
                "low": [c * 0.99 for c in closes],
                "close": closes if source == "yfinance" else [None] * n,
                "adj_close": closes,
                "volume": [1e6] * n,
                "source": [source] * n,
            }
        )
        for k, v in over.items():
            df[k] = v
        return df

    def test_a_clean_yfinance_window_has_no_issues(self) -> None:
        assert check_sanity(self._frame("yfinance", [100, 101, 102, 103, 104])) == []

    def test_a_clean_stooq_window_has_no_issues(self) -> None:
        """**这是「按源分支」那条规则的要害。**

        Stooq 行的 ``close`` 是 NULL。拿 NULL 去比大小或做除法，
        会让这条**设计好的降级路径变成永久 partial** ——
        也就是说，备源一启用就再也没有「正常」状态。
        """
        assert check_sanity(self._frame("stooq", [100, 101, 102, 103, 104])) == []

    def test_a_bad_tick_is_caught(self) -> None:
        """一个 MU 的 0.01 坏收盘会给出 ``mom_20 ≈ -99.9%``、RSI 钉在 0，
        并且会进入 126 日窗口，**在上游修正之后仍继续污染统计半年**。"""
        issues = check_sanity(self._frame("yfinance", [100, 101, 0.01, 103, 104]))
        assert any(i.kind == "日间跳变超限" for i in issues)

    def test_non_positive_price_is_caught(self) -> None:
        df = self._frame("yfinance", [100, 101, 102, 103, 104])
        df.loc[2, "adj_close"] = 0.0
        assert any(i.kind == "adj_close<=0" for i in check_sanity(df))

    def test_negative_volume_is_caught(self) -> None:
        df = self._frame("yfinance", [100, 101, 102, 103, 104])
        df.loc[2, "volume"] = -5
        assert any(i.kind == "volume<0" for i in check_sanity(df))

    def test_ohlc_inconsistency_is_caught(self) -> None:
        df = self._frame("yfinance", [100, 101, 102, 103, 104])
        df.loc[2, "low"] = 200.0  # low > high
        assert any(i.kind == "ohlc 不一致" for i in check_sanity(df))

    def test_a_split_day_is_let_through_when_a_corporate_action_is_known(self) -> None:
        """拆股日的**原始**价必然跳变 —— 那不是脏数据。"""
        df = self._frame("yfinance", [100, 101, 10.2, 10.3, 10.4])
        assert any(i.kind == "日间跳变超限" for i in check_sanity(df))
        assert check_sanity(df, corporate_action_dates={"X": {DAYS[2]}}) == []

    def test_an_empty_frame_is_not_an_error(self) -> None:
        assert check_sanity(pd.DataFrame()) == []

    def test_issue_renders_readably(self) -> None:
        assert str(SanityIssue("X", "k", "d")) == "X: k — d"


class TestCrossSourceComparison:
    def test_it_reports_relative_difference(self) -> None:
        a = pd.DataFrame({"symbol": ["X"], "date": [DAYS[0]], "adj_close": [100.0]})
        b = pd.DataFrame({"symbol": ["X"], "date": [DAYS[0]], "adj_close": [101.0]})
        out = cross_source_gap(a, b)
        assert out["rel_diff"].iloc[0] == pytest.approx(1 / 101)

    def test_no_overlap_is_an_empty_report(self) -> None:
        a = pd.DataFrame({"symbol": ["X"], "date": [DAYS[0]], "adj_close": [100.0]})
        b = pd.DataFrame({"symbol": ["X"], "date": [DAYS[1]], "adj_close": [100.0]})
        assert cross_source_gap(a, b).empty
        assert cross_source_gap(pd.DataFrame(), b).empty


class TestRestrictToSessions:
    def test_stale_rows_cannot_enter_the_window(self) -> None:
        """§9.1.4 第 4 条：「``prices_daily`` 里有什么就用什么」是不够的。

        日历修订之后价格表里可能残留多余日期。显式按 sessions 过滤，
        让「日历是唯一事实来源」成为**结构性保证**，而不是靠对账脚本跑对。
        """
        df = pd.DataFrame({"symbol": ["X"] * 3, "date": DAYS[:3], "adj_close": [1.0, 2.0, 3.0]})
        sessions = [
            Session(date=DAYS[0], ordinal=1, close_et=time(16), is_half_day=False),
            Session(date=DAYS[2], ordinal=2, close_et=time(16), is_half_day=False),
        ]
        out = restrict_to_sessions(df, sessions)
        assert out["date"].tolist() == [DAYS[0], DAYS[2]]

    def test_empty_input_is_fine(self) -> None:
        assert restrict_to_sessions(pd.DataFrame(), []).empty


class TestBudgetInteraction:
    def test_running_out_of_budget_propagates(self) -> None:
        """预算用尽必须**中止**，不是悄悄少抓几个标的 ——
        后者会产出一个「看起来完整」的部分结果。"""
        from pipeline.throttle import BudgetExceeded

        raw = _yf_raw({"AAPL": [np.nan] * 5, "MSFT": [np.nan] * 5})
        with pytest.raises(BudgetExceeded):
            fetch_window(
                ["AAPL", "MSFT"],
                START,
                END,
                _budget(max_requests=1),
                yf_frame=lambda s, a, b: yfinance_frame(s, a, b, download=lambda *x, **k: raw),
                stooq=lambda *a: pd.DataFrame(),
            )
