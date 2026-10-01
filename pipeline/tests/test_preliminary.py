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
        stamps.append(datetime.combine(day, time(16, 5), tzinfo=ET))
        stamps.append(datetime.combine(day - timedelta(days=1), time(15, 0), tzinfo=ET))
    idx = pd.DatetimeIndex(stamps).tz_convert("UTC")
    cols: dict[tuple[str, str], list[float]] = {}
    n = len(idx)
    for s in symbols:
        base = 100.0
        close = [base + i * 0.01 for i in range(n)]
        if extra_after_close:
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
        _calls, monkeypatch, rd = harness
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

        sql = (MIGRATIONS_DIR / "0003_preliminary.sql").read_text(encoding="utf-8")
        assert "before update on prices_daily" in sql
        assert "if not old.preliminary and new.preliminary then" in sql
        assert "'ok_preliminary'" in sql
        assert sql.lstrip().splitlines()[0].startswith("--")
        assert "begin;" in sql and "commit;" in sql
