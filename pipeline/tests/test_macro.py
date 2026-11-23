"""利率政策板块（M16，docs/add-rate-policy.md）。

夹具的**形状**取自 2026-11-23 的真实响应（纽约联储 JSON、财政部 CSV、美联储 HTML），
数值裁短。HTML 夹具特意带上真实页面里的三个毛病：

- ``<div class="row fomc-meeting" ">`` 里那个多余的引号；
- ``22 (notation vote)`` 与 ``Monetary Policy Strategy Meeting`` 两种非决议行；
- 跨月会议 ``Jan/Feb  31-1``。
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from pipeline.config import CONFIG_DIR
from pipeline.errors import ConfigError
from pipeline.invariants import parse_invariants
from pipeline.macro import MacroParseError
from pipeline.macro.config import TenorSpec, load_macro_config
from pipeline.macro.fomc import Meeting, coverage_start, parse_fomc
from pipeline.macro.nyfed import RatePoint, effr_url, parse_effr, with_changes
from pipeline.macro.treasury import (
    ANCHOR_TENOR,
    ANCHOR_TOLERANCE,
    YieldPoint,
    check_against_policy_rate,
    parse_par_yields,
    treasury_url,
)
from pipeline.schema import MIGRATIONS_DIR
from pipeline.sqltext import mask

TODAY = date(2026, 11, 23)
TENORS = [
    TenorSpec(column="3 Mo", key="3m", label="3M"),
    TenorSpec(column="2 Yr", key="2y", label="2Y"),
    TenorSpec(column="10 Yr", key="10y", label="10Y"),
]


# ---------------------------------------------------------------------------
# 纽约联储
# ---------------------------------------------------------------------------
def _effr_row(day: str, lo: Any, hi: Any, rate: Any = 3.88, **over: Any) -> dict[str, Any]:
    row = {
        "effectiveDate": day,
        "type": "EFFR",
        "percentRate": rate,
        "targetRateFrom": lo,
        "targetRateTo": hi,
        "revisionIndicator": "",
    }
    row.update(over)
    return row


def _effr_payload(*rows: dict[str, Any]) -> str:
    return json.dumps({"refRates": list(rows)})


class TestParseEffr:
    def test_sorted_ascending_and_exact(self) -> None:
        pts = parse_effr(
            _effr_payload(
                _effr_row("2026-11-19", 3.75, 4.0),
                _effr_row("2026-09-16", 3.5, 3.75, 3.64),
            )
        )
        assert [p.date for p in pts] == [date(2026, 9, 16), date(2026, 11, 19)]
        assert pts[1] == RatePoint(
            date(2026, 11, 19), Decimal("3.75"), Decimal("4.0"), Decimal("3.88")
        )

    @pytest.mark.parametrize("bad", [388, 0, -1, 25, 100.0])
    def test_an_out_of_range_rate_rejects_the_whole_payload(self, bad: float) -> None:
        # 差 100 倍的数字看起来「像个利率」，放行就会被页面当成事实展示。
        good = _effr_row("2026-11-18", 3.75, 4.0)
        with pytest.raises(MacroParseError):
            parse_effr(_effr_payload(good, _effr_row("2026-11-19", 3.75, 4.0, bad)))  # EFFR
        with pytest.raises(MacroParseError):
            parse_effr(_effr_payload(good, _effr_row("2026-11-19", 3.75, bad)))  # 上沿

    @pytest.mark.parametrize("bad", [-1, 25, 388])
    def test_an_out_of_range_lower_edge_rejects(self, bad: float) -> None:
        with pytest.raises(MacroParseError):
            parse_effr(_effr_payload(_effr_row("2026-11-19", bad, 400.0)))

    def test_a_fraction_form_is_caught_by_row_consistency_not_by_range(self) -> None:
        # 0.0388 在 (0, 25) 里 —— 零利率时期的真实 EFFR 就是 0.08，范围卡不住。
        # 拦它的是「EFFR 离目标区间 3.75–4.00 差了 3.8 个百分点」。
        with pytest.raises(MacroParseError, match=r"离目标区间.*太远"):
            parse_effr(_effr_payload(_effr_row("2026-11-19", 3.75, 4.0, 0.0388)))

    def test_effr_may_leave_the_range_a_little(self) -> None:
        # 2019-09 回购市场紧张时 EFFR 真的越过了上沿；容差是为了不误杀这种真实情形。
        assert parse_effr(_effr_payload(_effr_row("2026-11-19", 3.75, 4.0, 4.05)))

    def test_the_zero_lower_bound_era_is_valid_data(self) -> None:
        # 2022-01-03…2022-03-15 的真实形状：区间 0.00–0.25，EFFR 0.08。
        # history_start=2022-01-01 会抓到它；把 0 当错数，整份响应都会被拒收。
        pts = parse_effr(
            _effr_payload(
                _effr_row("2022-01-03", 0.0, 0.25, 0.08),
                _effr_row("2022-03-17", 0.25, 0.5, 0.33),
            )
        )
        assert pts[0].target_lower == Decimal("0.0")
        rows = with_changes(pts)
        assert (rows[1]["change_kind"], rows[1]["change_bp"]) == ("up", 25)

    @pytest.mark.parametrize(
        "row",
        [
            _effr_row("2026-11-19", 4.0, 3.75),  # 下沿高于上沿
            _effr_row("2026-11-19", 3.75, 3.75),  # 退化区间
            _effr_row("2026-11-19", 3.75, 4.0, type="SOFR"),
            _effr_row("2026-11-19", "3.75", 4.0),  # 字符串不是数字
            _effr_row("2026-11-19", True, 4.0),  # bool 是 int 的子类，必须单独拦
            _effr_row("2026-11-19", None, 4.0),
            _effr_row("not-a-date", 3.75, 4.0),
        ],
    )
    def test_one_bad_row_rejects_everything(self, row: dict[str, Any]) -> None:
        with pytest.raises(MacroParseError):
            parse_effr(_effr_payload(_effr_row("2026-11-18", 3.75, 4.0), row))

    def test_the_same_day_twice_is_rejected(self) -> None:
        r = _effr_row("2026-11-19", 3.75, 4.0)
        with pytest.raises(MacroParseError, match="不止一行"):
            parse_effr(_effr_payload(r, dict(r)))

    @pytest.mark.parametrize("payload", ["", "not json", "[]", "{}", '{"refRates": []}', "null"])
    def test_wrong_shape_is_rejected(self, payload: str) -> None:
        with pytest.raises(MacroParseError):
            parse_effr(payload)

    def test_url_carries_the_window(self) -> None:
        assert effr_url(date(2022, 1, 1), TODAY).endswith("startDate=2022-01-01&endDate=2026-11-23")


def _pts(*triples: tuple[str, str, str]) -> list[RatePoint]:
    return [
        RatePoint(date.fromisoformat(d), Decimal(lo), Decimal(hi), Decimal("3.88"))
        for d, lo, hi in triples
    ]


class TestWithChanges:
    def test_a_hike(self) -> None:
        rows = with_changes(_pts(("2026-09-16", "3.50", "3.75"), ("2026-09-17", "3.75", "4.00")))
        assert (rows[1]["change_kind"], rows[1]["change_bp"]) == ("up", 25)

    def test_a_cut_is_negative(self) -> None:
        rows = with_changes(_pts(("2024-09-18", "5.25", "5.50"), ("2024-09-19", "4.75", "5.00")))
        assert (rows[1]["change_kind"], rows[1]["change_bp"]) == ("down", -50)

    def test_the_first_row_has_no_predecessor_so_no_change(self) -> None:
        rows = with_changes(_pts(("2026-09-17", "3.75", "4.00")))
        assert (rows[0]["change_kind"], rows[0]["change_bp"]) == (None, None)

    def test_a_hold_marks_nothing(self) -> None:
        rows = with_changes(_pts(("2026-10-01", "3.75", "4.00"), ("2026-10-02", "3.75", "4.00")))
        assert all(r["change_kind"] is None and r["change_bp"] is None for r in rows)

    def test_only_one_edge_moving_is_mixed_and_guesses_nothing(self) -> None:
        rows = with_changes(_pts(("2026-10-01", "3.75", "4.00"), ("2026-10-02", "3.75", "4.25")))
        assert (rows[1]["change_kind"], rows[1]["change_bp"]) == ("mixed", None)

    def test_edges_moving_in_opposite_directions_is_mixed(self) -> None:
        rows = with_changes(_pts(("2026-10-01", "3.75", "4.00"), ("2026-10-02", "3.50", "4.25")))
        assert (rows[1]["change_kind"], rows[1]["change_bp"]) == ("mixed", None)

    def test_the_difference_is_exact_not_floating(self) -> None:
        # 这对数是挑过的：float 里 0.32 - 0.07 == 0.25，而 0.57 - 0.32 == 0.24999999999999994。
        # 若 with_changes 改回 float 差分，上下沿的差不相等 → 被判成 mixed（变异测试实测）。
        assert 0.32 - 0.07 != 0.57 - 0.32  # 夹具自检：这对数确实能区分 float 与 Decimal
        rows = with_changes(_pts(("2026-10-01", "0.07", "0.32"), ("2026-10-02", "0.32", "0.57")))
        assert (rows[1]["change_kind"], rows[1]["change_bp"]) == ("up", 25)

    def test_rows_carry_the_source_and_the_edges(self) -> None:
        row = with_changes(_pts(("2026-10-01", "3.75", "4.00")))[0]
        assert row["source"] == "nyfed"
        assert (row["target_lower"], row["target_upper"]) == (Decimal("3.75"), Decimal("4.00"))


# ---------------------------------------------------------------------------
# 财政部
# ---------------------------------------------------------------------------
HEADER = (
    '"Date","1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr",'
    '"5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"'
)


def _csv(*rows: str, header: str = HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


ROW_OK = "11/20/2026,4.19,4.26,4.28,4.32,4.32,4.38,4.51,4.63,4.65,4.71,4.80,4.92,5.34,5.31"
ROW_PREV = "11/19/2026,4.20,4.27,4.29,4.34,4.35,4.42,4.62,4.76,4.81,4.86,4.94,5.04,5.43,5.40"


class TestParseYields:
    def test_only_the_configured_tenors_in_percent(self) -> None:
        pts = parse_par_yields(_csv(ROW_OK, ROW_PREV), TENORS)
        got = {(p.date.isoformat(), p.tenor): p.yield_pct for p in pts}
        assert len(pts) == 6
        assert got[("2026-11-20", "2y")] == Decimal("4.63")
        assert got[("2026-11-20", "10y")] == Decimal("4.92")
        assert got[("2026-11-19", "3m")] == Decimal("4.34")

    def test_sorted_by_date_then_tenor(self) -> None:
        pts = parse_par_yields(_csv(ROW_OK, ROW_PREV), TENORS)
        assert [(p.date, p.tenor) for p in pts] == sorted((p.date, p.tenor) for p in pts)

    def test_a_missing_header_column_rejects(self) -> None:
        with pytest.raises(MacroParseError, match="缺少期限列"):
            parse_par_yields(_csv(ROW_OK, header=HEADER.replace('"2 Yr"', '"2 Year"')), TENORS)

    def test_blank_and_na_cells_are_skipped_not_zero(self) -> None:
        row = "11/20/2026,4.19,,4.28,N/A,4.32,4.38,4.51,,4.65,4.71,4.80,4.92,5.34,5.31"
        pts = parse_par_yields(_csv(row), TENORS)
        assert {p.tenor for p in pts} == {"10y"}  # 3m=N/A、2y=空 都不是 0

    def test_garbage_in_a_cell_rejects_instead_of_being_treated_as_missing(self) -> None:
        row = ROW_OK.replace("4.63", "4,63")  # 逗号小数把列数也弄乱了
        with pytest.raises(MacroParseError):
            parse_par_yields(_csv(row), TENORS)
        with pytest.raises(MacroParseError, match="不是数字"):
            parse_par_yields(_csv(ROW_OK.replace("4.63", "abc")), TENORS)

    @pytest.mark.parametrize("bad", ["463", "0", "-4.63", "25"])
    def test_a_wrong_unit_rejects(self, bad: str) -> None:
        with pytest.raises(MacroParseError, match="单位"):
            parse_par_yields(_csv(ROW_OK.replace("4.63", bad)), TENORS)

    def test_a_bad_date_rejects(self) -> None:
        with pytest.raises(MacroParseError, match="MM/DD/YYYY"):
            parse_par_yields(_csv(ROW_OK.replace("11/20/2026", "2026-11-20")), TENORS)

    def test_the_same_day_twice_rejects(self) -> None:
        with pytest.raises(MacroParseError, match="不止一次"):
            parse_par_yields(_csv(ROW_OK, ROW_OK), TENORS)

    def test_header_only_rejects_unless_explicitly_allowed(self) -> None:
        with pytest.raises(MacroParseError, match="没有数据行"):
            parse_par_yields(_csv(), TENORS)
        assert parse_par_yields(_csv(), TENORS, allow_empty=True) == []

    def test_empty_text_and_a_bom_prefixed_header(self) -> None:
        with pytest.raises(MacroParseError):
            parse_par_yields("", TENORS)
        assert len(parse_par_yields("﻿" + _csv(ROW_OK), TENORS)) == 3

    def test_url_names_the_year_twice(self) -> None:
        url = treasury_url(2026)
        assert "/2026/all" in url and "field_tdr_date_value=2026" in url


def _yp(day: str, tenor: str, v: str) -> YieldPoint:
    return YieldPoint(date.fromisoformat(day), tenor, Decimal(v))


class TestYieldsAgainstThePolicyRate:
    """``(0, 25)`` 拦不住小数形式，所以拿 EFFR 当锚核对 3 个月期（A 闸门 A1-S2）。"""

    def test_a_normal_level_passes(self) -> None:
        check_against_policy_rate([_yp("2026-11-20", "3m", "4.32")], Decimal("3.88"))

    def test_a_fraction_form_is_rejected_even_though_it_is_inside_the_range(self) -> None:
        # 「10Y 0.04、变动 0bp、2s10s≈0」看起来完全正常 —— 这正是它危险的地方。
        pts = parse_par_yields(_csv(ROW_OK.replace("4.32", "0.0432")), TENORS)
        assert pts  # 范围检查确实放行了它
        with pytest.raises(MacroParseError, match="疑似单位错误"):
            check_against_policy_rate(pts, Decimal("3.88"))

    def test_a_hundredfold_error_the_other_way_is_rejected_too(self) -> None:
        with pytest.raises(MacroParseError):
            check_against_policy_rate([_yp("2026-11-20", "3m", "432")], Decimal("3.88"))

    def test_only_the_newest_day_is_compared(self) -> None:
        # 去年零利率时期的老点不该让今天的核对失败。
        pts = [_yp("2021-06-01", "3m", "0.05"), _yp("2026-11-20", "3m", "4.32")]
        check_against_policy_rate(pts, Decimal("3.88"))

    def test_a_missing_anchor_tenor_skips_instead_of_failing(self) -> None:
        check_against_policy_rate([_yp("2026-11-20", "2y", "4.63")], Decimal("3.88"))

    def test_the_boundary_is_inclusive_at_the_tolerance(self) -> None:
        edge = Decimal("3.88") + ANCHOR_TOLERANCE
        check_against_policy_rate([_yp("2026-11-20", ANCHOR_TENOR, str(edge))], Decimal("3.88"))
        with pytest.raises(MacroParseError):
            check_against_policy_rate(
                [_yp("2026-11-20", ANCHOR_TENOR, str(edge + Decimal("0.01")))], Decimal("3.88")
            )


# ---------------------------------------------------------------------------
# 美联储日程
# ---------------------------------------------------------------------------
def _row(
    month: str, days: str, *, shaded: bool = False, stray_quote: bool = False, tail: str = ""
) -> str:
    cls = "fomc-meeting--shaded row fomc-meeting" if shaded else "row fomc-meeting"
    quote = ' "' if stray_quote else ""  # 真实页面里确有 `class="row fomc-meeting" ">`
    return (
        f'<div class="{cls}"{quote}>\n'
        f'  <div class="fomc-meeting__month col-xs-5"><strong>{month}</strong></div>\n'
        f'  <div class="fomc-meeting__date col-xs-4 col-lg-1">{days}</div>\n'
        f'  <div class="col-xs-12 col-md-4">{tail}</div>\n'
        f"</div>\n"
    )


def _year(year: int, *rows: str) -> str:
    return f'<div class="panel"><h4><a href="#">{year} FOMC Meetings</a></h4></div>\n' + "".join(
        rows
    )


def _full_2026() -> str:
    return _year(
        2026,
        _row("January", "27-28", stray_quote=True),
        _row("March", "17-18*", shaded=True, stray_quote=True),
        _row("April", "28-29"),
        _row("June", "16-17*", shaded=True),
        _row("July", "28-29"),
        _row("September", "15-16*", shaded=True),
        _row("October", "27-28"),
        _row("December", "8-9*", shaded=True),
    )


def _page(*sections: str) -> str:
    return "<html><body>" + "".join(sections) + "<footer>Back to Top</footer></body></html>"


class TestParseFomc:
    def test_a_normal_year(self) -> None:
        ms = parse_fomc(_page(_full_2026()), TODAY)
        assert len(ms) == 8
        assert ms[0] == Meeting(date(2026, 1, 27), date(2026, 1, 28))
        assert ms[-1] == Meeting(date(2026, 12, 8), date(2026, 12, 9))

    def test_the_asterisk_is_not_part_of_the_date(self) -> None:
        ms = parse_fomc(_page(_full_2026()), TODAY)
        assert Meeting(date(2026, 3, 17), date(2026, 3, 18)) in ms

    def test_a_notation_vote_is_not_a_meeting(self) -> None:
        # 2025-08-22：书面表决，倒计时指向它就是指向一个不会宣布利率的日子。
        page = _page(_full_2026(), _year(2025, _row("August", "22 (notation vote)")))
        assert all(m.start_date != date(2025, 8, 22) for m in parse_fomc(page, TODAY))

    def test_a_strategy_meeting_is_not_a_rate_meeting(self) -> None:
        # 2027-10-26/27：美联储在 2026-11-12 把它改成了没有利率决定的战略会议。
        strategy = _row("October", "26-27", tail="<p>Monetary Policy Strategy Meeting</p>")
        page = _page(
            _full_2026(),
            _year(2027, _row("September", "14-15"), strategy, _row("November", "16-17")),
        )
        ms = parse_fomc(page, TODAY)
        assert Meeting(date(2027, 11, 16), date(2027, 11, 17)) in ms
        assert all(m.start_date != date(2027, 10, 26) for m in ms)

    def test_a_statement_link_that_merely_mentions_strategy_is_still_a_meeting(self) -> None:
        # 真实页面上每场会议旁都挂着「Statement on Longer-Run Goals and Monetary Policy Strategy」。
        link = '<a href="#">Statement on Longer-Run Goals and Monetary Policy Strategy</a>'
        page = _page(
            _year(
                2026,
                _row("January", "27-28", tail=link),
                _row("March", "17-18"),
                _row("April", "28-29"),
                _row("June", "16-17"),
                _row("July", "28-29"),
            )
        )
        assert Meeting(date(2026, 1, 27), date(2026, 1, 28)) in parse_fomc(page, TODAY)

    def test_a_cross_month_meeting(self) -> None:
        page = _page(_full_2026(), _year(2023, _row("Jan/Feb", "31-1")))
        assert Meeting(date(2023, 1, 31), date(2023, 2, 1)) in parse_fomc(page, TODAY)

    def test_an_unknown_row_shape_rejects_the_whole_page(self) -> None:
        # 不认识的行**不能**被当成「不是会议」悄悄跳过 —— 那会让日程停在旧版本而无人知晓，
        # 或者更糟，漏掉一场真的会议。
        for weird in ("TBD", "17", "March 17-18", "17-18 (unscheduled)", "17–18"):
            page = _page(_full_2026(), _year(2025, _row("March", weird)))
            with pytest.raises(MacroParseError):
                parse_fomc(page, TODAY)

    def test_an_unknown_month_rejects(self) -> None:
        page = _page(_full_2026(), _year(2025, _row("Smarch", "17-18")))
        with pytest.raises(MacroParseError, match="月份"):
            parse_fomc(page, TODAY)

    def test_an_impossible_date_or_span_rejects(self) -> None:
        for days in ("30-31", "17-25"):
            month = "February" if days == "30-31" else "March"
            page = _page(_full_2026(), _year(2025, _row(month, days)))
            with pytest.raises(MacroParseError):
                parse_fomc(page, TODAY)

    def test_a_row_without_a_date_cell_rejects(self) -> None:
        broken = (
            '<div class="row fomc-meeting">'
            '<div class="fomc-meeting__month"><strong>May</strong></div></div>'
        )
        with pytest.raises(MacroParseError, match="找不到月份或日期格"):
            parse_fomc(_page(_full_2026(), _year(2025, broken)), TODAY)

    def test_too_few_meetings_this_year_rejects(self) -> None:
        # 页面改版后正则只匹配到零星几行 —— 这种「看起来成功」的失败要被挡住。
        few = _year(2026, _row("January", "27-28"), _row("March", "17-18"))
        with pytest.raises(MacroParseError, match="至少要"):
            parse_fomc(_page(few), TODAY)

    def test_no_year_sections_rejects(self) -> None:
        with pytest.raises(MacroParseError, match="分段"):
            parse_fomc("<html>maintenance</html>", TODAY)

    def test_a_duplicated_start_date_rejects(self) -> None:
        dup = _year(
            2026,
            *[_row("January", "27-28")] * 2,
            *[_row(m, "10-11") for m in ("March", "April", "June", "July")],
        )
        with pytest.raises(MacroParseError):
            parse_fomc(_page(dup), TODAY)

    def test_a_reschedule_replaces_rather_than_accumulates(self) -> None:
        # 美联储 2026-11-12 把 2027 年 12 月的会议提前到 11 月。两次解析各自独立，
        # 不会把旧日期带进新结果 —— 「不留孤儿」靠的是整窗删+插（见 test_run_macro）。
        before = _page(_full_2026(), _year(2027, _row("December", "7-8")))
        after = _page(_full_2026(), _year(2027, _row("November", "16-17")))
        assert Meeting(date(2027, 12, 7), date(2027, 12, 8)) in parse_fomc(before, TODAY)
        ms = parse_fomc(after, TODAY)
        assert Meeting(date(2027, 12, 7), date(2027, 12, 8)) not in ms
        assert Meeting(date(2027, 11, 16), date(2027, 11, 17)) in ms

    def test_coverage_starts_on_jan_1_of_last_year(self) -> None:
        assert coverage_start(TODAY) == date(2025, 1, 1)


# ---------------------------------------------------------------------------
# 配置，以及它与 SQL 不变式的绑定
# ---------------------------------------------------------------------------
class TestConfig:
    def test_the_real_file_loads(self) -> None:
        cfg = load_macro_config()
        assert [t.key for t in cfg.treasury_tenors] == ["3m", "2y", "10y"]
        assert cfg.history_start == date(2022, 1, 1)

    def test_a_typo_in_a_key_fails_loudly(self, tmp_path: Any) -> None:
        text = (CONFIG_DIR / "macro.yaml").read_text(encoding="utf-8")
        (tmp_path / "macro.yaml").write_text(
            text.replace("history_start", "histroy_start"), "utf-8"
        )
        with pytest.raises(ConfigError):
            load_macro_config(tmp_path)

    def test_duplicate_tenor_keys_are_rejected(self, tmp_path: Any) -> None:
        text = (CONFIG_DIR / "macro.yaml").read_text(encoding="utf-8")
        (tmp_path / "macro.yaml").write_text(text.replace('key: "2y"', 'key: "3m"'), "utf-8")
        with pytest.raises(ConfigError, match="重复"):
            load_macro_config(tmp_path)

    def test_the_sql_anchor_tolerance_is_the_python_one(self) -> None:
        """SQL 不变式里的容差必须与 ``ANCHOR_TOLERANCE`` 一致 —— 抄两份必然漂移，所以绑在一起。"""
        (a,) = [x for x in parse_invariants() if x.name.startswith("最新一天的 3 个月期收益率")]
        code = mask(a.sql)
        # **带边界**：裸子串匹配会让 `> 30` / `> 3.5` 也通过（变异测试实测）。
        pattern = rf"abs\(y - e\) > {re.escape(str(ANCHOR_TOLERANCE))}(?![\d.])"
        assert re.search(pattern, code), "SQL 里的容差必须恰好等于 ANCHOR_TOLERANCE"
        assert f"tenor = '{ANCHOR_TENOR}'" in a.sql  # 字面量在掩码里是 x，所以对原文看

    def test_the_fomc_duplicate_check_looks_at_each_date_on_its_own(self) -> None:
        """旧行没删掉时往往是「同开始日、不同结束日」—— 按 (start, end) 组合分组查不到它。"""
        (a,) = [x for x in parse_invariants() if x.name.startswith("fomc_meetings 必须有未来会议")]
        code = " ".join(mask(a.sql).lower().split())
        assert "group by start_date having" in code
        assert "group by end_date having" in code
        assert "group by start_date, end_date" not in code

    def test_the_sql_staleness_thresholds_are_the_configured_ones(self) -> None:
        """SQL 里的 7 / 7 / 14 必须与 macro.yaml 一致 —— 抄两份必然漂移，所以绑在一起。"""
        cfg = load_macro_config()
        (a,) = [x for x in parse_invariants() if x.name.startswith("利率数据不得静默停摆")]
        found = [int(n) for n in re.findall(r"current_date - (\d+)", mask(a.sql))]
        assert found == [
            cfg.stale_after_days.target_range,
            cfg.stale_after_days.yields,
            cfg.stale_after_days.fomc,
        ]


# ---------------------------------------------------------------------------
# 迁移 0004
# ---------------------------------------------------------------------------
def _sql_without_comments(name: str) -> str:
    raw = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    return "\n".join(ln.split("--", 1)[0] for ln in raw.splitlines())


class TestMigration0004:
    def test_every_new_public_table_has_rls_and_an_anon_read_policy(self) -> None:
        sql = _sql_without_comments("0004_rate_policy.sql")
        for table in ("rate_policy_daily", "treasury_yields", "fomc_meetings"):
            assert f"alter table {table}" in sql and "enable row level security" in sql
            assert re.search(rf'create policy "public read" on {table}\s+for select to anon', sql)

    def test_anon_gets_read_only(self) -> None:
        sql = _sql_without_comments("0004_rate_policy.sql")
        assert re.search(r"revoke all on [\w, ]+ from anon, authenticated", sql)
        grants = re.findall(r"grant ([\w, ]+) on [\w, .]+ to anon", sql)
        assert grants and all(g.strip() == "select" for g in grants)

    def test_the_writer_gets_only_the_verbs_it_needs(self) -> None:
        sql = _sql_without_comments("0004_rate_policy.sql")
        # 原始事实层：只 upsert，没有 DELETE。
        assert (
            "grant select, insert, update on rate_policy_daily, treasury_yields to pipeline_writer"
            in sql
        )
        # 日程：整窗删+插，没有 UPDATE。
        assert (
            "grant select, insert, delete on fomc_meetings" in sql and "to pipeline_writer" in sql
        )
        assert not re.search(r"on (rate_policy_daily|treasury_yields) for delete", sql)
        assert not re.search(r"on fomc_meetings for update", sql)

    def test_the_change_columns_are_constrained_in_the_database(self) -> None:
        sql = _sql_without_comments("0004_rate_policy.sql")
        assert "change_kind = 'up'    and change_bp > 0" in sql
        assert "change_kind = 'down'  and change_bp < 0" in sql
        assert "change_kind = 'mixed' and change_bp is null" in sql

    def test_the_rollback_drops_every_table_the_migration_creates(self) -> None:
        up = _sql_without_comments("0004_rate_policy.sql")
        down = _sql_without_comments("0004_rollback.sql")
        created = re.findall(r"create table ([\w.]+)", up)
        assert sorted(created) == sorted(
            ["rate_policy_daily", "treasury_yields", "fomc_meetings", "private.macro_fetch_state"]
        )
        for t in created:
            assert f"drop table if exists {t}" in down
