"""``run_macro``：三个来源互相独立，失败的来源不动库里的旧数据（M16）。

假 I/O：``fetch`` 与 ``today`` 注入，store 的四个写函数换成可观测的假货，连接换成
只会数 ``commit`` / ``rollback`` 的桩。**每条测试都必须真的调到 ``run_macro``** ——
本仓库吃过「测试从头到尾没碰被测函数、把 bug 加回去它照样绿」的亏
（skills/review-gate/SKILL.md 的坑 2）。
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date
from typing import Any

import pytest

from pipeline import run_macro as rm
from pipeline.macro.config import load_macro_config
from pipeline.macro.fomc import FOMC_URL, coverage_start
from pipeline.macro.nyfed import effr_url
from pipeline.macro.treasury import treasury_url
from pipeline.run_macro import MacroReport, run_macro
from pipeline.throttle import RequestBudget

TODAY = date(2026, 11, 23)
CFG = load_macro_config()


# --- 夹具：与 test_macro.py 同形状，保持自成一体 ---------------------------------
def _effr() -> str:
    def row(d: str, lo: float, hi: float, r: float) -> dict[str, Any]:
        return {
            "effectiveDate": d,
            "type": "EFFR",
            "percentRate": r,
            "targetRateFrom": lo,
            "targetRateTo": hi,
        }

    return json.dumps(
        {
            "refRates": [
                # 首行是 history_start 当天的真实形状（零利率时期，EFFR 0.08）。它让
                # 「拿最旧一行的 EFFR 当锚」在测试里也会失败 —— 生产里 3 个月期收益率 4.32 对
                # 0.08 差 4.2，超过容差，treasury 这一路会永久失败。
                row("2022-01-03", 0.0, 0.25, 0.08),
                row("2026-09-16", 3.5, 3.75, 3.64),
                row("2026-09-17", 3.75, 4.0, 3.88),
                row("2026-11-19", 3.75, 4.0, 3.88),
            ]
        }
    )


_HEADER = '"Date","3 Mo","2 Yr","10 Yr"'


def _treasury(year: int) -> str:
    return f"{_HEADER}\n11/20/{year}0,4.32,4.63,4.92\n".replace(f"{year}0", str(year))


def _meeting(month: str, days: str, tail: str = "") -> str:
    return (
        '<div class="row fomc-meeting">'
        f'<div class="fomc-meeting__month"><strong>{month}</strong></div>'
        f'<div class="fomc-meeting__date">{days}</div><div>{tail}</div></div>'
    )


def _fomc_page(*, extra_2027: str = "") -> str:
    rows_2026 = "".join(
        _meeting(m, d)
        for m, d in [
            ("January", "27-28"),
            ("March", "17-18*"),
            ("April", "28-29"),
            ("June", "16-17*"),
            ("July", "28-29"),
            ("September", "15-16*"),
            ("October", "27-28"),
            ("December", "8-9*"),
        ]
    )
    strategy = _meeting("October", "26-27", "<p>Monetary Policy Strategy Meeting</p>")
    y27 = (
        "<h4>2027 FOMC Meetings</h4>"
        + _meeting("September", "14-15")
        + strategy
        + _meeting("November", "16-17")
        + extra_2027
    )
    return f"<h4>2026 FOMC Meetings</h4>{rows_2026}{y27}"


def _routes(**override: Any) -> dict[str, Any]:
    routes: dict[str, Any] = {
        effr_url(CFG.history_start, TODAY): _effr(),
        treasury_url(TODAY.year - 1): _treasury(TODAY.year - 1),
        treasury_url(TODAY.year): _treasury(TODAY.year),
        FOMC_URL: _fomc_page(),
    }
    routes.update(override)
    return routes


# --- 假 I/O ---------------------------------------------------------------------
class FakeConn:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class Harness:
    def __init__(self) -> None:
        self.conn = FakeConn()
        self.calls: dict[str, list[Any]] = {
            "rate": [],
            "yields": [],
            "fomc": [],
            "state": [],
            "fetched": [],
        }
        self.fail_write: set[str] = set()

    def fetcher(self, routes: dict[str, Any]) -> Any:
        def fetch(url: str) -> str:
            self.calls["fetched"].append(url)
            value = routes[url]
            if isinstance(value, Exception):
                raise value
            return str(value)

        return fetch

    def run(self, routes: dict[str, Any] | None = None) -> MacroReport:
        budget = RequestBudget(
            max_requests=CFG.max_requests_per_run,
            interval_seconds=0,
            retry_max_attempts=CFG.retry_max_attempts,
            sleep=lambda _s: None,
        )
        return run_macro(
            self.conn,  # type: ignore[arg-type]
            CFG,
            today=TODAY,
            fetch=self.fetcher(routes if routes is not None else _routes()),
            budget=budget,
        )


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> Harness:
    hh = Harness()

    def _guard(name: str) -> None:
        if name in hh.fail_write:
            raise RuntimeError(f"写 {name} 时数据库出错")

    def upsert_rate(conn: Any, rows: list[dict[str, Any]]) -> int:
        _guard("rate")
        hh.calls["rate"].append(rows)
        return len(rows)

    def upsert_yields(conn: Any, rows: list[dict[str, Any]]) -> int:
        _guard("yields")
        hh.calls["yields"].append(rows)
        return len(rows)

    def replace_fomc(conn: Any, start: date, rows: list[dict[str, Any]]) -> int:
        _guard("fomc")
        hh.calls["fomc"].append((start, rows))
        return len(rows)

    def record(conn: Any, source: str, error: str | None) -> None:
        hh.calls["state"].append((source, error))

    monkeypatch.setattr(rm, "upsert_rate_policy", upsert_rate)
    monkeypatch.setattr(rm, "upsert_treasury_yields", upsert_yields)
    monkeypatch.setattr(rm, "replace_fomc_meetings", replace_fomc)
    monkeypatch.setattr(rm, "record_macro_fetch", record)
    return hh


class TestHappyPath:
    def test_all_three_sources_are_written_and_recorded_ok(self, h: Harness) -> None:
        report = h.run()
        assert report.ok and report.exit_code == 0
        assert h.calls["state"] == [("nyfed", None), ("treasury", None), ("fomc", None)]
        assert report.rows == {"nyfed": 4, "treasury": 6, "fomc": 10}

    def test_four_requests_a_run(self, h: Harness) -> None:
        # 设计文档 §4.3 的承诺：4 次请求/跑（1 + 2 + 1），不是 17 个标的那种量级。
        h.run()
        assert len(h.calls["fetched"]) == 4

    def test_the_whole_history_is_rewritten_with_changes_recomputed(self, h: Harness) -> None:
        h.run()
        (rows,) = h.calls["rate"]
        # 零利率时期那行之后直接是 2026-09-16：夹具把 2022→2026 之间压缩成了一次 350bp 的跳变。
        assert [r["change_kind"] for r in rows] == [None, "up", "up", None]
        assert [r["change_bp"] for r in rows] == [None, 350, 25, None]

    def test_the_strategy_meeting_never_reaches_the_database(self, h: Harness) -> None:
        h.run()
        ((_, rows),) = h.calls["fomc"]
        assert date(2027, 10, 26) not in {r["start_date"] for r in rows}
        assert date(2027, 11, 16) in {r["start_date"] for r in rows}

    def test_the_fomc_window_starts_where_the_contract_says(self, h: Harness) -> None:
        h.run()
        ((start, _),) = h.calls["fomc"]
        assert start == coverage_start(TODAY)


class TestSourcesAreIndependent:
    def test_a_failing_fomc_page_does_not_stop_rates_and_yields(self, h: Harness) -> None:
        report = h.run(_routes(**{FOMC_URL: OSError("down")}))
        assert report.exit_code == 1 and list(report.failures) == ["fomc"]
        assert len(h.calls["rate"]) == 1 and len(h.calls["yields"]) == 1

    def test_a_failing_nyfed_does_not_stop_the_others(self, h: Harness) -> None:
        report = h.run(_routes(**{effr_url(CFG.history_start, TODAY): "<html>503</html>"}))
        assert list(report.failures) == ["nyfed"]
        assert len(h.calls["yields"]) == 1 and len(h.calls["fomc"]) == 1

    def test_a_failure_is_recorded_with_its_reason_and_ok_is_not_touched(self, h: Harness) -> None:
        h.run(_routes(**{FOMC_URL: OSError("down")}))
        failed = [(s, e) for s, e in h.calls["state"] if e is not None]
        assert len(failed) == 1 and failed[0][0] == "fomc" and "down" in (failed[0][1] or "")
        # 成功的来源记的是 error=None（record_macro_fetch 据此更新 last_ok_at）。
        assert ("nyfed", None) in h.calls["state"] and ("treasury", None) in h.calls["state"]


class TestYieldsAreCrossCheckedAgainstTheNyFedRate:
    def test_a_fraction_form_csv_is_rejected_and_not_written(self, h: Harness) -> None:
        # 财政部 CSV 悄悄改成小数形式：表头、日期都对，数也在 (0, 25) 里 ——
        # 只有拿同一跑里纽约联储的 EFFR 去对，才抓得到。
        scaled = _HEADER + "\n11/20/2026,0.0432,0.0463,0.0492\n"
        report = h.run(_routes(**{treasury_url(TODAY.year): scaled}))
        assert "treasury" in report.failures and "疑似单位错误" in report.failures["treasury"]
        assert h.calls["yields"] == []
        # 另外两个来源不受影响。
        assert len(h.calls["rate"]) == 1 and len(h.calls["fomc"]) == 1

    def test_when_nyfed_failed_the_check_is_skipped_not_failed(self, h: Harness) -> None:
        # 没有锚就没法核对；那一半交给 invariants.sql 的事后断言。不能因此让 treasury 也失败。
        scaled = _HEADER + "\n11/20/2026,0.0432,0.0463,0.0492\n"
        routes = _routes(
            **{
                effr_url(CFG.history_start, TODAY): "<html>503</html>",
                treasury_url(TODAY.year): scaled,
            }
        )
        report = h.run(routes)
        assert list(report.failures) == ["nyfed"]
        assert len(h.calls["yields"]) == 1


class TestAFailedSourceNeverTouchesOldData:
    def test_a_rejected_fomc_page_leaves_the_old_schedule_alone(self, h: Harness) -> None:
        # 页面里混进一个不认识的行：整次拒收，**既不删也不插** ——
        # 一次抓取失败不能清空库里那份仍然有效的日程。
        bad = _fomc_page(extra_2027=_meeting("May", "TBD"))
        report = h.run(_routes(**{FOMC_URL: bad}))
        assert "fomc" in report.failures and "MacroParseError" in report.failures["fomc"]
        assert h.calls["fomc"] == []

    def test_a_page_with_too_few_meetings_leaves_the_old_schedule_alone(self, h: Harness) -> None:
        report = h.run(
            _routes(**{FOMC_URL: "<h4>2026 FOMC Meetings</h4>" + _meeting("May", "5-6")})
        )
        assert "fomc" in report.failures and h.calls["fomc"] == []

    def test_a_unit_error_in_the_rates_leaves_the_old_rates_alone(self, h: Harness) -> None:
        bad = json.loads(_effr())
        bad["refRates"][2]["percentRate"] = 388
        report = h.run(_routes(**{effr_url(CFG.history_start, TODAY): json.dumps(bad)}))
        assert "nyfed" in report.failures and h.calls["rate"] == []

    def test_an_empty_treasury_year_leaves_the_old_yields_alone(self, h: Harness) -> None:
        report = h.run(_routes(**{treasury_url(TODAY.year): _HEADER + "\n"}))
        assert "treasury" in report.failures and h.calls["yields"] == []

    def test_a_database_error_rolls_back_and_is_recorded(self, h: Harness) -> None:
        h.fail_write.add("fomc")
        report = h.run()
        assert "fomc" in report.failures and "数据库出错" in report.failures["fomc"]
        assert h.conn.rollbacks >= 1
        # 另外两个来源照常落库。
        assert len(h.calls["rate"]) == 1 and len(h.calls["yields"]) == 1


class TestTreasuryWindow:
    def test_a_header_only_current_year_is_normal_in_early_january(self, h: Harness) -> None:
        jan = date(2027, 1, 5)
        routes = {
            effr_url(CFG.history_start, jan): _effr(),
            treasury_url(2026): _treasury(2026),
            treasury_url(2027): _HEADER + "\n",
            FOMC_URL: _fomc_page(),
        }
        budget = RequestBudget(
            max_requests=20, interval_seconds=0, retry_max_attempts=3, sleep=lambda _s: None
        )
        report = run_macro(
            h.conn,  # type: ignore[arg-type]
            CFG,
            today=jan,
            fetch=h.fetcher(routes),
            budget=budget,
        )
        # fomc 在 2027-01-05 当年场次不足 5 场（页面夹具只有 2 场）会失败 ——
        # 本条只关心 treasury 没有因为「新年头两周当年表头为空」而失败。
        assert "treasury" not in report.failures
        assert report.rows["treasury"] == 3

    def test_the_same_empty_file_in_june_is_a_failure(self, h: Harness) -> None:
        june = date(2026, 6, 10)
        routes = {
            effr_url(CFG.history_start, june): _effr(),
            treasury_url(2025): _treasury(2025),
            treasury_url(2026): _HEADER + "\n",
            FOMC_URL: _fomc_page(),
        }
        budget = RequestBudget(
            max_requests=20, interval_seconds=0, retry_max_attempts=3, sleep=lambda _s: None
        )
        report = run_macro(
            h.conn,  # type: ignore[arg-type]
            CFG,
            today=june,
            fetch=h.fetcher(routes),
            budget=budget,
        )
        assert "treasury" in report.failures


class TestBudgetAndExit:
    def test_exit_code_is_one_if_any_source_failed(self) -> None:
        r = MacroReport(failures={"fomc": "x"}, rows={"nyfed": 1})
        assert r.exit_code == 1 and not r.ok

    def test_an_empty_report_is_ok_and_describes_itself(self) -> None:
        assert MacroReport().exit_code == 0
        assert MacroReport().describe() == "没有任何来源被执行"

    def test_failures_are_reported_in_the_summary(self) -> None:
        r = MacroReport(failures={"fomc": "boom"}, rows={"nyfed": 3})
        assert "nyfed: 3 行" in r.describe() and "fomc: 失败（boom）" in r.describe()

    def test_retries_are_bounded_by_the_budget(self, h: Harness) -> None:
        # 一个永远失败的来源：重试 3 次就放弃，不会无限敲门。
        report = h.run(_routes(**{FOMC_URL: OSError("down")}))
        assert "fomc" in report.failures
        fomc_hits = [u for u in h.calls["fetched"] if u == FOMC_URL]
        assert len(fomc_hits) == CFG.retry_max_attempts


class TestHttpGet:
    def test_sends_an_honest_user_agent_and_rejects_non_200(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        class Resp:
            def __init__(self, status: int) -> None:
                self.status = status

            def read(self) -> bytes:
                return b"\xef\xbb\xbfhello"

            def __enter__(self) -> Resp:
                return self

            def __exit__(self, *a: Any) -> None:
                return None

        def fake_urlopen(req: Any, timeout: float) -> Resp:
            seen["ua"] = req.get_header("User-agent")
            seen["timeout"] = timeout
            return Resp(seen.get("status", 200))

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert rm.http_get("https://example.test/x", 12.0) == "hello"  # BOM 被吃掉
        assert "markme" in seen["ua"] and "github.com/sudoghut/markme" in seen["ua"]
        assert seen["timeout"] == 12.0
        seen["status"] = 204
        with pytest.raises(RuntimeError, match="204"):
            rm.http_get("https://example.test/x", 12.0)


# --- store 里的三个新函数（用记录 SQL 的假游标，不连库）-------------------------------
class _Cur:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, stmt: Any, params: Any = None) -> None:
        self.log.append(
            ("execute", (str(getattr(stmt, "as_string", lambda _c: stmt)(None)), params))
        )

    def executemany(self, stmt: Any, rows: Any) -> None:
        self.log.append(("executemany", list(rows)))


class _RecConn:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> _Cur:
        return _Cur(self.log)

    def commit(self) -> None:
        self.commits += 1


class TestStoreWrites:
    def test_a_failure_never_moves_last_ok_at(self) -> None:
        from pipeline.store import record_macro_fetch

        conn = _RecConn()
        record_macro_fetch(conn, "fomc", "boom")  # type: ignore[arg-type]
        ((_, (stmt, params)),) = conn.log
        # 「数据最后一次是什么时候对的」不能被一次失败改掉。
        assert "last_ok_at = excluded.last_ok_at" not in stmt
        assert "last_error = excluded.last_error" in stmt
        assert params == ("fomc", "boom") and conn.commits == 1

    def test_a_success_clears_the_error_and_moves_last_ok_at(self) -> None:
        from pipeline.store import record_macro_fetch

        conn = _RecConn()
        record_macro_fetch(conn, "nyfed", None)  # type: ignore[arg-type]
        ((_, (stmt, _)),) = conn.log
        assert "last_ok_at = excluded.last_ok_at" in stmt and "last_error = null" in stmt

    def test_the_fomc_delete_runs_even_when_nothing_is_inserted(self) -> None:
        # 「会议被取消、本次一场都不返回」的路径：已作废的未来行必须消失
        # （与 symbol_events 的整窗删+插同一条规则）。
        from pipeline.store import replace_fomc_meetings

        conn = _RecConn()
        n = replace_fomc_meetings(conn, date(2025, 1, 1), [])  # type: ignore[arg-type]
        assert n == 0
        assert [k for k, _ in conn.log] == ["execute"]
        assert "delete from fomc_meetings" in conn.log[0][1][0]
        assert conn.log[0][1][1] == (date(2025, 1, 1),)

    def test_the_fomc_delete_precedes_the_insert(self) -> None:
        from pipeline.store import replace_fomc_meetings

        conn = _RecConn()
        rows = [{"start_date": date(2026, 12, 8), "end_date": date(2026, 12, 9)}]
        assert replace_fomc_meetings(conn, date(2025, 1, 1), rows) == 1  # type: ignore[arg-type]
        assert [k for k, _ in conn.log] == ["execute", "executemany"]
