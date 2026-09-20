"""``pipeline/invariants.py`` 的测试（M3）。

这个模块是 §9.3.2 的**探测器**。一个探测器自身没有测试，是这整套设计里
最不该出现的东西：它坏掉的样子恰恰是「一切正常」—— 断言被静默合并、
被静默截断、或者根本没被解析出来，跑完照样打印 ALL PASS。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.errors import ConfigError
from pipeline.invariants import (
    INVARIANTS_SQL,
    Violation,
    parse_invariants,
    run_invariants,
)
from pipeline.sqltext import mask

#: ``supabase/invariants.sql`` 里断言的条数。
#:
#: **这是一个刻意的硬编码。** 它要挡住的是「少了一条断言」这种没有任何症状
#: 的损坏：把 `-- name:` 打错一个字母，那条断言会被**并进上一条**的 SQL 里
#: 变成注释，跑起来依旧 0 行、依旧全绿。改这个数字应当是一次自觉的动作。
EXPECTED_ASSERTIONS = 20


class TestTheRealFile:
    def test_every_assertion_parses(self) -> None:
        assertions = parse_invariants()
        assert len(assertions) == EXPECTED_ASSERTIONS, (
            f"解析出 {len(assertions)} 条，期望 {EXPECTED_ASSERTIONS} 条。"
            "若是有意增删，请同步改 EXPECTED_ASSERTIONS；"
            "若不是，多半是某个 `-- name:` 标注被打错了。"
        )

    def test_names_are_unique_and_meaningful(self) -> None:
        names = [a.name for a in parse_invariants()]
        assert len(set(names)) == len(names)
        for n in names:
            assert len(n) > 8, f"断言名太短，报告里看不出是哪条：{n!r}"

    def test_every_body_is_a_query(self) -> None:
        """每条断言都必须是一条能跑的查询。

        ``run_invariants`` 把「断言自身报错」也算成违规，所以一条写坏的断言
        不会静默通过 —— 但那要等到连上库。这里在 CI 里就先挡一道。
        """
        for a in parse_invariants():
            # 每条断言前面都有一段解释它为什么存在的注释，那是**有意**的
            # （§9.3.2：一条没人看得懂的断言迟早被人删掉）。看掩码。
            code = mask(a.sql).strip().lower()
            assert code.startswith(("select", "with")), f"{a.name} 不是查询：{code[:60]}"
            assert ";" not in code, f"{a.name} 的正文里还留着分号：{code[-80:]}"

    def test_no_assertion_swallowed_its_neighbour(self) -> None:
        """正文里不该再出现 ``name:`` —— 出现就说明切分点漏了一个。"""
        for a in parse_invariants():
            assert "name:" not in a.sql.lower(), a.name


class TestParsing:
    def _write(self, tmp_path: Path, text: str) -> Path:
        p = tmp_path / "inv.sql"
        p.write_text(text, encoding="utf-8")
        return p

    def test_prose_after_the_statement_is_dropped(self, tmp_path: Path) -> None:
        """**这是 M2 那个 bug。** 只剥尾部分号时，下面这段散文会被一起
        送进 ``execute()``；多数驱动照样跑得过去，于是 bug 没有症状 ——
        直到某天注释里出现一个分号。"""
        p = self._write(
            tmp_path,
            "-- name: 第一条断言的名字\nselect 1;\n"
            "-- 为什么有这条：因为 a; 因为 b;\n"
            "-- name: 第二条断言的名字\nselect 2;\n",
        )
        out = parse_invariants(p)
        assert [a.sql for a in out] == ["select 1", "select 2"]

    def test_semicolon_inside_a_literal_does_not_split(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            "-- name: 字符串里的分号不算结束\nselect 'a;b' as x, (1);\n",
        )
        assert parse_invariants(p)[0].sql == "select 'a;b' as x, (1)"

    def test_marker_is_case_insensitive(self, tmp_path: Path) -> None:
        """``-- Name:`` 打成大写不该让两条断言**静默合并成一条**。"""
        p = self._write(
            tmp_path,
            "-- name: 小写标注的断言\nselect 1;\n-- Name: 大写标注的断言\nselect 2;\n",
        )
        assert len(parse_invariants(p)) == 2

    def test_duplicate_names_are_rejected(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path,
            "-- name: 完全一样的断言名\nselect 1;\n-- name: 完全一样的断言名\nselect 2;\n",
        )
        with pytest.raises(ConfigError, match="同名"):
            parse_invariants(p)

    def test_an_indented_marker_still_counts(self, tmp_path: Path) -> None:
        """``  -- name: x`` 和写错大小写一样，是个再平常不过的手滑。

        不认它的后果最恶劣：那条断言先被并进上一条，随后在第一个分号处
        **被整段丢掉** —— 文件照样解析、照样全绿，只是少验了一件事。
        """
        p = self._write(
            tmp_path,
            "-- name: 顶格标注的断言\nselect 1;\n  -- name: 缩进标注的断言\nselect 2;\n",
        )
        assert [a.sql for a in parse_invariants(p)] == ["select 1", "select 2"]

    def test_a_marker_inside_a_literal_is_not_a_marker(self, tmp_path: Path) -> None:
        """字符串里的 ``-- name:`` 会从一条断言的正中间切一刀，
        切出来的前半段还是个未闭合的引号。"""
        p = self._write(
            tmp_path,
            "-- name: 唯一真正的断言名字\nselect x from t where s = '\n-- name: 假的\n';\n",
        )
        out = parse_invariants(p)
        assert len(out) == 1
        assert out[0].name == "唯一真正的断言名字"

    def test_an_assertion_with_no_statement_is_loud(self, tmp_path: Path) -> None:
        """对一个「少一条就没有症状」的文件来说，静默丢掉是最糟的默认。"""
        p = self._write(
            tmp_path,
            "-- name: 有名字却没有语句\n\n-- name: 正常的那条断言\nselect 1;\n",
        )
        with pytest.raises(ConfigError, match="没有语句"):
            parse_invariants(p)

    def test_a_semicolon_inside_parens_does_not_end_the_statement(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, "-- name: 括号里的分号不算结束\nselect (1);\n")
        assert parse_invariants(p)[0].sql == "select (1)"

    def test_a_file_without_markers_is_loud(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, "select 1;\n")
        with pytest.raises(ValueError, match="name:"):
            parse_invariants(p)

    def test_the_shipped_path_points_at_a_real_file(self) -> None:
        assert INVARIANTS_SQL.is_file()


class TestRunning:
    SQL = "-- name: 这条断言必须返回零行\nselect 1;\n"

    def test_zero_rows_is_a_pass(self, tmp_path: Path) -> None:
        p = tmp_path / "inv.sql"
        p.write_text(self.SQL, encoding="utf-8")
        assert run_invariants(lambda _sql: [], p) == []

    def test_rows_are_a_violation(self, tmp_path: Path) -> None:
        p = tmp_path / "inv.sql"
        p.write_text(self.SQL, encoding="utf-8")
        out = run_invariants(lambda _sql: [{"table": "sectors"}], p)
        assert out == [Violation("这条断言必须返回零行", ["sectors"])]

    def test_an_assertion_that_blows_up_is_also_a_violation(self, tmp_path: Path) -> None:
        """一个跑不起来的探测器和一个不触发的探测器一样没用。"""
        p = tmp_path / "inv.sql"
        p.write_text(self.SQL, encoding="utf-8")

        def boom(_sql: str) -> list[dict[str, object]]:
            raise RuntimeError("relation does not exist")

        out = run_invariants(boom, p)
        assert len(out) == 1
        assert "relation does not exist" in out[0].rows[0]

    def test_violation_rows_are_capped(self, tmp_path: Path) -> None:
        """报告是给人看的：一条断言返回十万行时不该把日志淹掉。"""
        p = tmp_path / "inv.sql"
        p.write_text(self.SQL, encoding="utf-8")
        out = run_invariants(lambda _sql: [{"x": i} for i in range(500)], p)
        assert len(out[0].rows) == 20
