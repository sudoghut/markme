"""M13：收盘后先出临时结果，次日定稿（``docs/provisional-close.md``）。

原则：**收盘后尽快出一个标明临时的结果；前一天及更早的历史必须是定稿数字。**
这里的每一组测试对应那份计划 §9 验收清单里的一条，外加实现时踩出来的几处：

- 临时 → 定稿时价格一分不差，``diff_prices`` 若只比价格就不会写，标记永远翻不回去；
- 宽限（``vendor_grace``）只对「session 就是今天」成立，定稿跑上午 9 点不该被它静音；
- 分钟线不能给降级到 Stooq 的窗口补行（规则 1），也不能绕过闸门 4。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
import pytest

from pipeline.calendar_gate import ET
from pipeline.config import load_config
from pipeline.fetch import FetchOutcome, fetch_window, intraday_frame
from pipeline.sessions import Session
from pipeline.store import diff_prices
from pipeline.tests.test_run_once_behavior import (  # 复用同一套假数据；夹具在 conftest.py
    FakeConn,
    _frame,
    _install_plan,
    _no_revision,
    _sessions,
)
from pipeline.throttle import RequestBudget


# ---------------------------------------------------------------------------
# intraday_frame：分钟线 → 一行日线
# ---------------------------------------------------------------------------
def _minute_bars(
    symbols: list[str],
    day: date,
    *,
    last: time = time(15, 59),
    extra_after_close: bool = True,
) -> pd.DataFrame:
    """yfinance `group_by="column"` 的形状：两级列 (field, ticker)，UTC 索引。"""
    start = datetime.combine(day, time(9, 30), tzinfo=ET)
    end = datetime.combine(day, last, tzinfo=ET)
    stamps = list(pd.date_range(start, end, freq="1min").to_pydatetime())
    if extra_after_close:  # 收盘之后、另一天的行都必须被排除
        stamps.append(datetime.combine(day, time(16, 0), tzinfo=ET))
        stamps.append(datetime.combine(day, time(16, 5), tzinfo=ET))
        stamps.append(datetime.combine(day - timedelta(days=1), time(15, 0), tzinfo=ET))
    idx = pd.DatetimeIndex(stamps).tz_convert("UTC")
    cols: dict[tuple[str, str], list[float]] = {}
    n = len(idx)
    for s in symbols:
        base = 100.0
        close = [base + i * 0.01 for i in range(n)]
        if extra_after_close:
            close[-3] = 777.0  # 16:00 那一根：收盘那一刻开始的 bar，已不属于常规时段
            close[-2] = 999.0  # 16:05 那一根：盘后，不该是收盘价
            close[-1] = 1.0  # 前一天那一根
        cols[("Open", s)] = [c - 0.5 for c in close]
        cols[("High", s)] = [c + 1.0 for c in close]
        cols[("Low", s)] = [c - 1.0 for c in close]
        cols[("Close", s)] = close
        cols[("Volume", s)] = [10.0] * n
    df = pd.DataFrame(list(zip(*cols.values(), strict=True)), index=idx)
    df.columns = pd.MultiIndex.from_tuples(list(cols))
    return df


def _session(d: date, *, half: bool = False) -> Session:
    return Session(date=d, ordinal=1, close_et=time(13 if half else 16, 0), is_half_day=half)


DAY = date(2026, 9, 30)


class TestIntradayFrame:
    def test_it_takes_the_last_regular_bar_as_the_close(self) -> None:
        raw = _minute_bars(["AAA"], DAY)
        out = intraday_frame(["AAA"], _session(DAY), download=lambda *a, **k: raw)
        assert len(out) == 1
        row = out.iloc[0]
        n_regular = 390  # 09:30 … 15:59
        assert row["close"] == pytest.approx(100.0 + (n_regular - 1) * 0.01)
        assert row["adj_close"] == row["close"], "同日复权价就是原价"
        assert row["open"] == pytest.approx(99.5), "第一根的 Open"
        assert row["volume"] == pytest.approx(10.0 * n_regular), "只加常规时段"
        assert row["date"] == DAY
        assert row["source"] == "yfinance", "同源同基准，不是新的数据源"

    def test_after_hours_and_other_days_are_excluded(self) -> None:
        raw = _minute_bars(["AAA"], DAY)
        out = intraday_frame(["AAA"], _session(DAY), download=lambda *a, **k: raw)
        assert out.iloc[0]["close"] != 999.0, "16:05 是盘后"
        assert out.iloc[0]["close"] != 777.0, "16:00 那根不是 15:59 那根"
        assert out.iloc[0]["high"] < 777.0
        assert out.iloc[0]["high"] < 999.0
        assert out.iloc[0]["low"] > 1.0, "前一天那根混进来了"

    def test_a_stale_last_bar_is_not_a_close(self) -> None:
        """最后一根停在 15:00 —— 停牌或分钟线只给了半天。那不是收盘价。"""
        raw = _minute_bars(["AAA"], DAY, last=time(15, 0), extra_after_close=False)
        out = intraday_frame(["AAA"], _session(DAY), download=lambda *a, **k: raw)
        assert out.empty

    def test_half_day_closes_at_one(self) -> None:
        raw = _minute_bars(["AAA"], DAY, last=time(12, 59), extra_after_close=False)
        out = intraday_frame(["AAA"], _session(DAY, half=True), download=lambda *a, **k: raw)
        assert len(out) == 1

    def test_single_level_columns_for_several_symbols_give_nothing(self) -> None:
        """单级列只可能属于一个 ticker。拿它给 17 只都拼一行，就是把一个价格写成 17 只的收盘价。"""
        raw = _minute_bars(["AAA"], DAY).xs("AAA", axis=1, level=1)
        out = intraday_frame(["AAA", "BBB"], _session(DAY), download=lambda *a, **k: raw)
        assert out.empty
        one = intraday_frame(["AAA"], _session(DAY), download=lambda *a, **k: raw)
        assert len(one) == 1, "只有一个标的时单级列就是它的"

    def test_an_all_nan_symbol_is_dropped_not_written(self) -> None:
        raw = _minute_bars(["AAA", "BBB"], DAY)
        for f in ("Open", "High", "Low", "Close", "Volume"):
            raw[(f, "BBB")] = float("nan")
        out = intraday_frame(["AAA", "BBB"], _session(DAY), download=lambda *a, **k: raw)
        assert list(out["symbol"]) == ["AAA"]

    def test_it_asks_for_regular_hours_only_and_unadjusted(self) -> None:
        seen: dict[str, Any] = {}

        def _dl(*a: Any, **k: Any) -> pd.DataFrame:
            seen.update(k)
            return _minute_bars(["AAA"], DAY)

        intraday_frame(["AAA"], _session(DAY), download=_dl)
        assert seen["interval"] == "1m"
        assert seen["prepost"] is False
        assert seen["auto_adjust"] is False
        assert seen["start"] == DAY.isoformat()
        assert seen["end"] == (DAY + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# diff_prices：临时 → 定稿，价格一样也要写
# ---------------------------------------------------------------------------
class TestFlippingThePreliminaryFlagIsAChange:
    def test_same_price_but_now_final_is_written(self) -> None:
        row = {"symbol": "AAA", "date": DAY, "close": 10.0, "adj_close": 10.0, "source": "yfinance"}
        existing = {("AAA", DAY): {**row, "preliminary": True}}
        d = diff_prices([{**row, "preliminary": False}], existing)
        assert d.n_changed == 1, "不写的话 preliminary 永远翻不回 false"

    def test_same_price_same_flag_is_skipped(self) -> None:
        row = {"symbol": "AAA", "date": DAY, "close": 10.0, "adj_close": 10.0, "source": "yfinance"}
        d = diff_prices(
            [{**row, "preliminary": False}], {("AAA", DAY): {**row, "preliminary": False}}
        )
        assert d.n_changed == 0


class _RecCursor:
    """记下 SQL、按脚本吐回行 —— 让三个新查询的谓词与解析可被断言。"""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows, self.sql = rows, ""
        self.many: list[Any] = []

    def execute(self, sql: Any, params: Any = None) -> None:
        self.sql = str(sql)

    def executemany(self, sql: Any, seq: Any) -> None:
        self.sql, self.many = str(sql), list(seq)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def __enter__(self) -> _RecCursor:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class _RecConn:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.cur = _RecCursor(rows or [])

    def cursor(self) -> _RecCursor:
        return self.cur


class TestTheNewQueries:
    def test_final_rows_counts_only_final_rows(self) -> None:
        from pipeline.run_daily import _final_rows

        conn = _RecConn([(16,)])
        assert _final_rows(conn, ["A"], DAY) == 16  # type: ignore[arg-type]
        assert "not preliminary" in conn.cur.sql

    def test_preliminary_rows_reads_only_preliminary_rows(self) -> None:
        from pipeline.run_daily import _preliminary_rows

        row = ("A", DAY, 1.0, 2.0, 0.5, 1.5, 1.5, 10, "yfinance")
        conn = _RecConn([row])
        out = _preliminary_rows(conn, ["A"], DAY, DAY)  # type: ignore[arg-type]
        assert "where preliminary" in conn.cur.sql
        assert out == [
            {
                "symbol": "A",
                "date": DAY,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "adj_close": 1.5,
                "volume": 10,
                "source": "yfinance",
            }
        ]

    def test_existing_prices_carries_the_flag(self) -> None:
        """没有它，`_same_price` 比的是 None 对 None —— 临时 → 定稿同价时永远不写。"""
        from pipeline.run_daily import _existing_prices

        conn = _RecConn([("A", DAY, 1.5, 1.5, "yfinance", True)])
        out = _existing_prices(conn, ["A"], DAY, DAY)  # type: ignore[arg-type]
        assert out[("A", DAY)]["preliminary"] is True

    def test_upsert_prices_defaults_to_final_and_keeps_true(self) -> None:
        from pipeline.store import PRICE_WRITE_COLUMNS, upsert_prices

        base = {"symbol": "A", "date": DAY, "close": 1.0, "adj_close": 1.0, "source": "yfinance"}
        flagged = {**base, "date": DAY - timedelta(days=1), "preliminary": True}
        conn = _RecConn()
        upsert_prices(conn, [base, flagged])  # type: ignore[arg-type]
        i = PRICE_WRITE_COLUMNS.index("preliminary")
        assert [t[i] for t in conn.cur.many] == [False, True]


# ---------------------------------------------------------------------------
# fetch_window(require=...)：定稿兜底走 Stooq，**整窗**换源
# ---------------------------------------------------------------------------
class TestRequireFallsBackToStooqForTheWholeWindow:
    D1, D2 = date(2026, 9, 29), date(2026, 9, 30)

    def _budget(self) -> RequestBudget:
        return RequestBudget(max_requests=50, interval_seconds=0, retry_max_attempts=1)

    def test_a_missing_required_day_switches_the_whole_window(self) -> None:
        yf = _frame(["AAA", "BBB"], [self.D2])  # AAA 缺 D1
        yf = pd.concat([yf, _frame(["BBB"], [self.D1])], ignore_index=True)
        stooq = _frame(["AAA"], [self.D1, self.D2]).assign(source="stooq", close=None)
        out = fetch_window(
            ["AAA", "BBB"],
            self.D1,
            self.D2,
            self._budget(),
            yf_frame=lambda *a: yf,
            stooq=lambda s, *a: stooq if s == "AAA" else pd.DataFrame(),
            require={"AAA": {self.D1}},
        )
        assert out.per_symbol_source["AAA"] == "stooq"
        assert "AAA" in out.degraded
        assert out.sources_are_unique_per_symbol(), "整窗替换，不是单行拼接（规则 1）"
        assert set(out.frame.loc[out.frame["symbol"] == "AAA", "date"]) == {self.D1, self.D2}

    def test_stooq_without_that_day_keeps_yfinance(self) -> None:
        yf = _frame(["AAA"], [self.D2])
        stooq = _frame(["AAA"], [self.D2]).assign(source="stooq", close=None)
        out = fetch_window(
            ["AAA"],
            self.D1,
            self.D2,
            self._budget(),
            yf_frame=lambda *a: yf,
            stooq=lambda *a: stooq,
            require={"AAA": {self.D1}},
        )
        assert out.per_symbol_source["AAA"] == "yfinance"
        assert not out.degraded

    def test_nothing_required_means_no_extra_request(self) -> None:
        calls: list[str] = []

        def _stq(s: str, *a: Any) -> pd.DataFrame:
            calls.append(s)
            return pd.DataFrame()

        out = fetch_window(
            ["AAA"],
            self.D1,
            self.D2,
            self._budget(),
            yf_frame=lambda *a: _frame(["AAA"], [self.D1, self.D2]),
            stooq=_stq,
            require={"AAA": {self.D1}},
        )
        assert calls == [] and out.per_symbol_source["AAA"] == "yfinance"


# ---------------------------------------------------------------------------
# run_once：两段式管道
# ---------------------------------------------------------------------------
def _cfg_symbols() -> tuple[Any, list[str], str]:
    cfg = load_config()
    symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
    return cfg, symbols, cfg.universe.benchmark


def _wire(
    rd: Any,
    monkeypatch: Any,
    sessions: list[Session],
    frame: pd.DataFrame,
    *,
    symbols: list[str],
    source: dict[str, Any] | None = None,
    intraday: pd.DataFrame | None = None,
    seen: dict[str, Any] | None = None,
) -> None:
    _install_plan(rd, monkeypatch, sessions, _no_revision())

    def _fw(*a: Any, **k: Any) -> FetchOutcome:
        if seen is not None:
            seen["require"] = k.get("require")
            seen["end"] = a[2]
        return FetchOutcome(
            frame=frame, per_symbol_source=source or dict.fromkeys(symbols, "yfinance")
        )

    monkeypatch.setattr(rd, "fetch_window", _fw)
    if intraday is not None:
        monkeypatch.setattr(rd, "intraday_frame", lambda syms, session, **k: intraday)


def _intraday_rows(symbols: list[str], day: date, price: float = 106.0) -> pd.DataFrame:
    return _frame(symbols, [day], price=price)


def _prices_written(calls: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for batch in calls["upsert_prices"] for r in batch]


class TestTheProvisionalRun:
    """收盘 +60 分钟起：日线还是半根 bar，当天收盘价取自分钟线，标临时。"""

    def test_one_hour_after_the_close_everyone_gets_a_preliminary_row(self, harness: Any) -> None:
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        days = [s.date for s in sessions]
        # 日线：今天那根是半根 bar，被 fetch.py 丢掉了 → 全员停在 D-1。
        _wire(
            rd,
            monkeypatch,
            sessions,
            _frame(symbols, days[:-1]),
            symbols=symbols,
            intraday=_intraday_rows(symbols, today),
        )
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(17, 5), tzinfo=ET))
        assert report.status == "ok_preliminary"
        assert report.exit_code == 0, "临时结果是正常产出，不告警"
        today_rows = [r for r in _prices_written(calls) if r["date"] == today]
        assert len(today_rows) == len(symbols)
        assert all(r["preliminary"] for r in today_rows)
        assert calls["replace_strength"], "当天的榜单照样出（这就是要的「大致结果」）"
        assert calls["revalidate"], "页面要更新"

    def test_a_daily_bar_before_final_settle_is_still_preliminary(self, harness: Any) -> None:
        """日线 `Close` 不是 NaN 也不等于定稿：M12 实测它的 Open 在 21:05→22:09 还在变。"""
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        _wire(
            rd, monkeypatch, sessions, _frame(symbols, [s.date for s in sessions]), symbols=symbols
        )
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(20, 0), tzinfo=ET))
        assert report.status == "ok_preliminary"
        assert all(r["preliminary"] for r in _prices_written(calls) if r["date"] == today)
        assert calls["intraday"] == [], "日线都有了，不用再问分钟线"

    def test_after_final_settle_the_daily_bar_is_final(self, harness: Any) -> None:
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        _wire(
            rd, monkeypatch, sessions, _frame(symbols, [s.date for s in sessions]), symbols=symbols
        )
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert report.status == "ok"
        assert not any(r["preliminary"] for r in _prices_written(calls))

    def test_the_final_settle_moment_itself_is_final(self, harness: Any) -> None:
        """边界归定稿那一侧：21:30 ET 整点那一跑拿到的日线就是定稿。"""
        _, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        _wire(
            rd, monkeypatch, sessions, _frame(symbols, [s.date for s in sessions]), symbols=symbols
        )
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(21, 30), tzinfo=ET))
        assert report.status == "ok"

    def test_a_preliminary_row_at_the_same_price_is_rewritten_as_final(self, harness: Any) -> None:
        """端到端版的 `_same_price`：库里是临时值、价格一分不差，这一跑必须照样写。"""
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        frame = _frame(symbols, [s.date for s in sessions])
        existing = {
            (r["symbol"], r["date"]): {
                "close": r["close"],
                "adj_close": r["adj_close"],
                "source": "yfinance",
                "preliminary": r["date"] == today,
            }
            for r in frame.to_dict("records")
        }
        _wire(rd, monkeypatch, sessions, frame, symbols=symbols)
        monkeypatch.setattr(rd, "_existing_prices", lambda *a, **k: existing)
        rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        written = _prices_written(calls)
        assert {r["date"] for r in written} == {today}, "只有今天那 17 行变了（标记翻了）"
        assert len(written) == len(symbols)
        assert all(r["preliminary"] is False for r in written)

    def test_a_real_big_move_earlier_in_the_window_does_not_block_the_minute_row(
        self, harness: Any
    ) -> None:
        """闸门 4 只比「前一根 + 这一根」：窗口里早先一次真实的大波动不该挡住它。"""
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        days = [s.date for s in sessions]
        frame = _frame(symbols, days[:-1])
        jumper = symbols[3]
        early = frame["date"] < days[10]
        frame.loc[(frame["symbol"] == jumper) & early, ["close", "adj_close"]] = 40.0
        _wire(
            rd,
            monkeypatch,
            sessions,
            frame,
            symbols=symbols,
            intraday=_intraday_rows(symbols, today, price=105.9),
        )
        rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(17, 5), tzinfo=ET))
        assert jumper in {r["symbol"] for r in _prices_written(calls) if r["date"] == today}

    def test_after_final_settle_a_missing_daily_bar_is_filled_from_minutes(
        self, harness: Any
    ) -> None:
        calls, monkeypatch, rd = harness
        cfg, symbols, bench = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        days = [s.date for s in sessions]
        late = next(s for s in symbols if s != bench)
        frame = pd.concat(
            [_frame([s for s in symbols if s != late], days), _frame([late], days[:-1])],
            ignore_index=True,
        )
        _wire(
            rd,
            monkeypatch,
            sessions,
            frame,
            symbols=symbols,
            intraday=_intraday_rows([late], today),
        )
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert report.status == "ok_preliminary"
        pre = {
            r["symbol"] for r in _prices_written(calls) if r["date"] == today and r["preliminary"]
        }
        assert pre == {late}, "只有没拿到日线的那只是临时的"

    def test_minutes_are_not_spliced_into_a_stooq_window(self, harness: Any) -> None:
        """规则 1：降级到 Stooq 的窗口复权基准不同，拼一行 Yahoo 进去就是接缝。"""
        _calls, monkeypatch, rd = harness
        cfg, symbols, bench = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        days = [s.date for s in sessions]
        stq = next(s for s in symbols if s != bench)
        frame = pd.concat(
            [_frame([s for s in symbols if s != stq], days), _frame([stq], days[:-1])],
            ignore_index=True,
        )
        asked: list[list[str]] = []

        def _intra(syms: Any, session: Any, **k: Any) -> pd.DataFrame:
            asked.append(list(syms))
            return pd.DataFrame()

        _wire(
            rd,
            monkeypatch,
            sessions,
            frame,
            symbols=symbols,
            source={**dict.fromkeys(symbols, "yfinance"), stq: "stooq"},
        )
        monkeypatch.setattr(rd, "intraday_frame", _intra)
        rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert all(stq not in a for a in asked)

    def test_a_poisoned_minute_close_is_not_used(self, harness: Any) -> None:
        """闸门 4 同样适用：0.01 的分钟线收盘价和 0.01 的日线一样是毒数据。"""
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        days = [s.date for s in sessions]
        bad = _intraday_rows(symbols, today).copy()
        victim = symbols[1]
        bad.loc[bad["symbol"] == victim, ["close", "adj_close"]] = 0.01
        _wire(rd, monkeypatch, sessions, _frame(symbols, days[:-1]), symbols=symbols, intraday=bad)
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(17, 5), tzinfo=ET))
        written = {r["symbol"] for r in _prices_written(calls) if r["date"] == today}
        assert victim not in written
        assert "未过合理性闸门" in report.message


class TestTheFinalizeRun:
    """闸门没放行、但上一个 session 在库里还不是全员定稿 → 钳到那一天去定稿。"""

    def test_saturday_morning_finalizes_friday(self, harness: Any) -> None:
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        friday = sessions[-1].date
        assert friday.weekday() == 4
        seen: dict[str, Any] = {}
        _wire(
            rd,
            monkeypatch,
            sessions,
            _frame(symbols, [s.date for s in sessions]),
            symbols=symbols,
            seen=seen,
        )
        monkeypatch.setattr(rd, "_final_rows", lambda conn, syms, day: 0)
        saturday = friday + timedelta(days=1)
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(saturday, time(9, 0), tzinfo=ET))
        assert report.session_date == friday
        assert seen["end"] == friday
        assert report.status == "ok"
        assert not any(r["preliminary"] for r in _prices_written(calls)), "隔天的日线就是定稿"

    def test_nothing_to_finalize_means_skip(self, harness: Any) -> None:
        _, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        saturday = sessions[-1].date + timedelta(days=1)
        _wire(
            rd, monkeypatch, sessions, _frame(symbols, [s.date for s in sessions]), symbols=symbols
        )
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(saturday, time(9, 0), tzinfo=ET))
        assert report.status == "skipped_holiday"

    def test_weekday_morning_finalizes_yesterday_not_today(self, harness: Any) -> None:
        _, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today, yesterday = sessions[-1].date, sessions[-2].date
        seen: dict[str, Any] = {}
        _wire(
            rd,
            monkeypatch,
            sessions,
            _frame(symbols, [s.date for s in sessions[:-1]]),
            symbols=symbols,
            seen=seen,
        )
        monkeypatch.setattr(rd, "_final_rows", lambda conn, syms, day: 0)
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(9, 0), tzinfo=ET))
        assert report.session_date == yesterday
        assert seen["end"] == yesterday, "上午 9 点今天还没开盘，绝不能抓今天"

    def test_a_finalize_run_is_not_graced(self, harness: Any) -> None:
        """宽限只对「session 就是今天」成立。周六上午 9 点「还没到 22:30」不是理由。"""
        _, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        friday = sessions[-1].date
        # 周五的日线还是没有，分钟线也没有 → 全员落后。
        _wire(
            rd,
            monkeypatch,
            sessions,
            _frame(symbols, [s.date for s in sessions[:-1]]),
            symbols=symbols,
        )
        monkeypatch.setattr(rd, "_final_rows", lambda conn, syms, day: 0)
        report = rd.run_once(
            FakeConn(), cfg, now=datetime.combine(friday + timedelta(days=1), time(9, 0), tzinfo=ET)
        )
        assert report.status == "stale_vendor"
        assert report.exit_code == 1

    def test_a_finalize_run_that_gets_nothing_keeps_last_nights_value(self, harness: Any) -> None:
        """周六早上：x 的周五日线还没来、分钟线也没有。沿用库里那行临时值，**不要**把它写成
        NULL、把它从周五的榜单里剔掉 —— 那比前一晚的临时结果还差。"""
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        friday = sessions[-1].date
        days = [s.date for s in sessions]
        x = symbols[4]
        frame = pd.concat(
            [_frame([s for s in symbols if s != x], days), _frame([x], days[:-1])],
            ignore_index=True,
        )
        _wire(rd, monkeypatch, sessions, frame, symbols=symbols)
        monkeypatch.setattr(rd, "_final_rows", lambda conn, syms, day: len(syms) - 1)
        row = {
            "symbol": x,
            "date": friday,
            "close": 105.9,
            "adj_close": 105.9,
            "source": "yfinance",
        }
        monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [row])
        report = rd.run_once(
            FakeConn(), cfg, now=datetime.combine(friday + timedelta(days=1), time(9, 0), tzinfo=ET)
        )
        assert report.status == "ok_preliminary", "还在等，不是故障；告警留给下一个交易日"
        assert report.exit_code == 0
        assert "沿用库里的临时值" in report.message
        assert "bar 落后" not in report.message
        mine = [r for r in _prices_written(calls) if r["symbol"] == x and r["date"] == friday]
        assert mine and all(r["preliminary"] is True for r in mine)

    @staticmethod
    def _reuse_case(
        harness: Any, *, now: datetime, x_source: str = "yfinance", minutes: bool = False
    ) -> tuple[Any, ...]:
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        friday = sessions[-1].date
        days = [s.date for s in sessions]
        x = symbols[4]
        frame = pd.concat(
            [_frame([s for s in symbols if s != x], days), _frame([x], days[:-1])],
            ignore_index=True,
        )
        if x_source == "stooq":
            frame.loc[frame["symbol"] == x, "source"] = "stooq"
        _wire(
            rd,
            monkeypatch,
            sessions,
            frame,
            symbols=symbols,
            source={**dict.fromkeys(symbols, "yfinance"), x: x_source},
            intraday=_intraday_rows([x], friday) if minutes else None,
        )
        monkeypatch.setattr(rd, "_final_rows", lambda conn, syms, day: len(syms) - 1)
        row = {
            "symbol": x,
            "date": friday,
            "close": 105.9,
            "adj_close": 105.9,
            "source": "yfinance",
        }
        monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [row])
        report = rd.run_once(FakeConn(), cfg, now=now)
        return report, calls, x, friday

    def test_the_same_day_cannot_reuse_its_way_out_of_an_alert(self, harness: Any) -> None:
        """当天 23:30（截止之后）x 还是什么都没有：那是真故障，沿用不能把它静音。"""
        sessions = _sessions(60)
        report, _, _, _ = self._reuse_case(
            harness, now=datetime.combine(sessions[-1].date, time(23, 30), tzinfo=ET)
        )
        assert "沿用库里的临时值" not in report.message
        assert report.exit_code == 1

    def test_a_stooq_window_does_not_reuse_a_yahoo_row(self, harness: Any) -> None:
        """规则 1：x 的窗口是 Stooq，就不能把库里那行 Yahoo 临时值拼进去。"""
        sessions = _sessions(60)
        report, calls, x, friday = self._reuse_case(
            harness,
            now=datetime.combine(sessions[-1].date + timedelta(days=1), time(9, 0), tzinfo=ET),
            x_source="stooq",
        )
        assert "沿用库里的临时值" not in report.message
        assert not [r for r in _prices_written(calls) if r["symbol"] == x and r["date"] == friday]

    def test_a_reused_row_is_written_once(self, harness: Any) -> None:
        """同一个 (symbol, date) 在一条 upsert 里出现两次，Postgres 会让整个 T2 失败。

        要让它真的可能重复：分钟线已经给出了那一行，库里又挂着同一天的临时值。
        """
        sessions = _sessions(60)
        report, calls, _, _ = self._reuse_case(
            harness,
            now=datetime.combine(sessions[-1].date + timedelta(days=1), time(9, 0), tzinfo=ET),
            minutes=True,
        )
        assert "沿用库里的临时值" not in report.message, "分钟线有了就用新的，不沿用旧的"
        keys = [(r["symbol"], r["date"]) for r in _prices_written(calls)]
        assert len(keys) == len(set(keys))

    def test_past_days_are_not_reused_they_alert(self, harness: Any) -> None:
        """沿用只认 session 那一天。更早的临时值必须走「保留 + partial」，不能被沿用静音。"""
        _, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        friday, thursday = sessions[-1].date, sessions[-2].date
        days = [s.date for s in sessions]
        x = symbols[4]
        frame = pd.concat(
            [
                _frame([s for s in symbols if s != x], days),
                _frame([x], [d for d in days if d != thursday]),
            ],
            ignore_index=True,
        )
        _wire(rd, monkeypatch, sessions, frame, symbols=symbols)
        monkeypatch.setattr(rd, "_final_rows", lambda conn, syms, day: 0)
        row = {"symbol": x, "date": thursday, "close": 1.0, "adj_close": 1.0, "source": "yfinance"}
        monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [row])
        report = rd.run_once(
            FakeConn(), cfg, now=datetime.combine(friday + timedelta(days=1), time(9, 0), tzinfo=ET)
        )
        assert "沿用库里的临时值" not in report.message
        assert report.status == "partial" and report.exit_code == 1

    def test_force_before_the_open_targets_yesterday(self, harness: Any) -> None:
        """M12 记进 BACKLOG 的那条：早上手动补昨天，曾经挑到的是今天。"""
        _, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today, yesterday = sessions[-1].date, sessions[-2].date
        seen: dict[str, Any] = {}
        _wire(
            rd,
            monkeypatch,
            sessions,
            _frame(symbols, [s.date for s in sessions[:-1]]),
            symbols=symbols,
            seen=seen,
        )
        report = rd.run_once(
            FakeConn(), cfg, now=datetime.combine(today, time(9, 0), tzinfo=ET), force=True
        )
        assert report.session_date == yesterday
        assert seen["end"] == yesterday
        assert report.status == "ok"


class TestYesterdayStillPreliminary:
    """下一个交易日收盘 +60 分钟，前一天仍拿不到正式价 → 保留临时值并告警。"""

    def test_it_keeps_the_preliminary_row_and_alerts(self, harness: Any) -> None:
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today, yesterday = sessions[-1].date, sessions[-2].date
        days = [s.date for s in sessions]
        x = symbols[2]
        frame = pd.concat(
            [
                _frame([s for s in symbols if s != x], days),
                _frame([x], [d for d in days if d != yesterday]),
            ],
            ignore_index=True,
        )
        pending_row = {
            "symbol": x,
            "date": yesterday,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 105.8,
            "adj_close": 105.8,
            "volume": 1.0,
            "source": "yfinance",
        }
        seen: dict[str, Any] = {}
        _wire(rd, monkeypatch, sessions, frame, symbols=symbols, seen=seen)
        monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [pending_row])
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert seen["require"] == {x: {yesterday}}, "要让 fetch_window 去问 Stooq"
        assert report.status == "partial"
        assert report.exit_code == 1
        assert f"{x}@{yesterday}" in report.message
        assert "窗口内有空洞" not in report.message, "保留下来的临时值填住了那一天"
        kept = [r for r in _prices_written(calls) if r["symbol"] == x and r["date"] == yesterday]
        assert kept, "那一天必须原样写回（它在库里，diff 之后才会跳过）"
        assert all(r["preliminary"] is True for r in kept), (
            "**这是整条原则的底线**：保留下来的是临时值，绝不能被当成定稿写回去"
        )

    def test_a_stooq_window_does_not_get_a_yahoo_row_spliced_in(self, harness: Any) -> None:
        """规则 1：x 的窗口已经整窗换成 Stooq，就不能再把库里那行 Yahoo 临时值拼进去。"""
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today, yesterday = sessions[-1].date, sessions[-2].date
        days = [s.date for s in sessions]
        x = symbols[2]
        frame = pd.concat(
            [
                _frame([s for s in symbols if s != x], days),
                _frame([x], [d for d in days if d != yesterday]).assign(source="stooq"),
            ],
            ignore_index=True,
        )
        _wire(
            rd,
            monkeypatch,
            sessions,
            frame,
            symbols=symbols,
            source={**dict.fromkeys(symbols, "yfinance"), x: "stooq"},
        )
        row = {"symbol": x, "date": yesterday, "close": 1.0, "adj_close": 1.0, "source": "yfinance"}
        monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [row])
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert "保留临时值" not in report.message
        assert not [
            r for r in _prices_written(calls) if r["symbol"] == x and r["date"] == yesterday
        ]

    def test_a_day_the_calendar_no_longer_has_is_not_kept(self, harness: Any) -> None:
        """历史修订删掉的那一天：带回来就违反 §9.1.4 第 4 条，而且会让它永远 partial。"""
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        gone = sessions[-1].date - timedelta(days=6)  # 上周六：不在日历里
        assert gone not in {s.date for s in sessions}
        _wire(
            rd, monkeypatch, sessions, _frame(symbols, [s.date for s in sessions]), symbols=symbols
        )
        row = {
            "symbol": symbols[0],
            "date": gone,
            "close": 1.0,
            "adj_close": 1.0,
            "source": "yfinance",
        }
        monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [row])
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert report.status == "ok"
        assert not [r for r in _prices_written(calls) if r["date"] == gone]

    def test_todays_own_preliminary_row_is_not_a_past_day_to_finalize(self, harness: Any) -> None:
        """`pending` 只收 **session 之前**的日子。今天那行是这一跑自己要写的，不是兜底对象。"""
        _, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today = sessions[-1].date
        seen: dict[str, Any] = {}
        _wire(
            rd,
            monkeypatch,
            sessions,
            _frame(symbols, [s.date for s in sessions]),
            symbols=symbols,
            seen=seen,
        )
        row = {
            "symbol": symbols[0],
            "date": today,
            "close": 1.0,
            "adj_close": 1.0,
            "source": "yfinance",
        }
        monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [row])
        rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert seen["require"] == {}

    def test_once_yahoo_has_it_the_row_is_finalized(self, harness: Any) -> None:
        calls, monkeypatch, rd = harness
        cfg, symbols, _ = _cfg_symbols()
        sessions = _sessions(60)
        today, yesterday = sessions[-1].date, sessions[-2].date
        x = symbols[2]
        pending_row = {
            "symbol": x,
            "date": yesterday,
            "close": 1.0,
            "adj_close": 1.0,
            "source": "yfinance",
        }
        _wire(
            rd, monkeypatch, sessions, _frame(symbols, [s.date for s in sessions]), symbols=symbols
        )
        monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [pending_row])
        report = rd.run_once(FakeConn(), cfg, now=datetime.combine(today, time(22, 0), tzinfo=ET))
        assert report.status == "ok"
        row = next(r for r in _prices_written(calls) if r["symbol"] == x and r["date"] == yesterday)
        assert row["preliminary"] is False


class TestStatusRank:
    def test_preliminary_outranks_events_stale(self) -> None:
        """两者同时成立时不能被 `_already_done` 当成做完了 —— 否则当晚的定稿跑被跳过。"""
        from pipeline.run_daily import RunReport

        r = RunReport(status="ok")
        r.escalate("ok_events_stale")
        r.escalate("ok_preliminary")
        assert r.status == "ok_preliminary"
        r2 = RunReport(status="ok")
        r2.escalate("ok_preliminary")
        r2.escalate("ok_events_stale")
        assert r2.status == "ok_preliminary"

    def test_partial_still_wins(self) -> None:
        from pipeline.run_daily import RunReport

        r = RunReport(status="ok")
        r.escalate("ok_preliminary")
        r.escalate("partial")
        assert r.status == "partial" and r.exit_code == 1

    def test_preliminary_exits_zero(self) -> None:
        from pipeline.run_daily import RunReport

        assert RunReport(status="ok_preliminary").exit_code == 0

    def test_already_done_does_not_count_preliminary(self) -> None:
        import inspect

        from pipeline.run_daily import _already_done

        src = inspect.getsource(_already_done)
        code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines() if '"""' not in ln)
        assert "'ok', 'ok_events_stale'" in code
        assert "ok_preliminary" not in code


class TestTheMigration:
    def test_every_migration_has_a_rollback(self) -> None:
        from pipeline.schema import MIGRATIONS_DIR

        for p in sorted(MIGRATIONS_DIR.glob("0*.sql")):
            if p.name.endswith("_rollback.sql"):
                continue
            rb = MIGRATIONS_DIR / f"{p.name[:4]}_rollback.sql"
            assert rb.exists(), f"{p.name} 没有回滚脚本（§9.4：不允许执行）"

    def test_0003_guards_final_rows_in_the_database(self) -> None:
        from pipeline.schema import MIGRATIONS_DIR

        raw = (MIGRATIONS_DIR / "0003_preliminary.sql").read_text(encoding="utf-8")
        assert raw.lstrip().splitlines()[0].startswith("--")
        # **去掉注释再断言。** 文件头的说明里就写着 `set search_path = ''` 和触发器的样子，
        # 对原文断言会在散文上命中（变异测试实测：删掉真正那一行，测试照样绿）。
        sql = "\n".join(ln.split("--", 1)[0] for ln in raw.splitlines())
        assert "before update on prices_daily" in sql
        assert "if not old.preliminary and new.preliminary then" in sql
        assert "'ok_preliminary'" in sql
        assert "begin;" in sql and "commit;" in sql
        assert "set search_path = ''" in sql

    def test_the_invariant_only_tolerates_the_latest_day(self) -> None:
        from pipeline.schema import MIGRATIONS_DIR

        inv = (MIGRATIONS_DIR.parent / "invariants.sql").read_text(encoding="utf-8")
        assert "where preliminary\n  and date < (select max(date) from prices_daily)" in inv
        assert "t_prices_keep_final" in inv
        assert "tgname < 't_prices_touch'" in inv
        assert "and date in (select date from trading_sessions)" in inv, "日历过滤"
        assert "and t.tgenabled in ('O', 'A')" in inv, "禁用 / replica-only 不算挂上"
        assert "and (t.tgtype & 2) = 2" in inv, "必须是 BEFORE（AFTER 跳不掉那一行）"
        assert "symbol in (select symbol from symbols where enabled)" in inv


class TestTheConfigOrdering:
    def test_final_settle_before_settle_is_rejected(self) -> None:
        import yaml
        from pydantic import ValidationError

        from pipeline.config import AppConfig, ConfigError
        from pipeline.schema import MIGRATIONS_DIR

        raw = yaml.safe_load(
            (MIGRATIONS_DIR.parent.parent / "config" / "app.yaml").read_text(encoding="utf-8")
        )
        AppConfig.model_validate(raw)  # 现状是合法的
        raw["final_settle_minutes"] = raw["settle_minutes"] - 1
        with pytest.raises((ConfigError, ValidationError), match="定稿"):
            AppConfig.model_validate(raw)
