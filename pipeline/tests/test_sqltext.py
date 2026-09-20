"""``pipeline/sqltext.mask`` 的测试（M3）。

掩码器的契约只有三句，但每一句都被下游当成前提：
**等长**（下标能在掩码与原文之间互换）、**换行保留**（行号还能算）、
**不猜**（未闭合的引号抛异常，而不是把剩下半个文件当字符串吞掉）。
"""

from __future__ import annotations

import pytest

from pipeline.errors import ConfigError
from pipeline.schema import MIGRATIONS_DIR
from pipeline.sqltext import mask


class TestTheContract:
    @pytest.mark.parametrize(
        "sql",
        [
            "select 1",
            "select 'a' -- b\n, 2",
            "select $$ x ; ( $$",
            'select "a b" from t',
            "/* a /* 嵌套 */ b */ select 1",
        ],
    )
    def test_same_length_and_newlines_kept(self, sql: str) -> None:
        m = mask(sql)
        assert len(m) == len(sql)
        assert [i for i, c in enumerate(m) if c == "\n"] == [
            i for i, c in enumerate(sql) if c == "\n"
        ]


class TestWhatGetsMasked:
    def test_line_comment(self) -> None:
        comment = "-- ; ( )"
        assert mask(f"select 1 {comment}\nselect 2") == (
            "select 1 " + " " * len(comment) + "\nselect 2"
        )

    def test_block_comment_can_nest(self) -> None:
        """Postgres 的块注释是嵌套的 —— 非嵌套实现会在第一个 ``*/`` 就收工，
        把后面真正的 SQL 当成注释外的内容，反之亦然。"""
        out = mask("/* a /* b */ c */select 1")
        assert out.strip() == "select 1"

    def test_string_literal(self) -> None:
        assert mask("where s = 'a;b--c'") == "where s =         "

    def test_doubled_quote_is_an_escape(self) -> None:
        """``'it''s'`` 是一个字面量，不是两个。

        对 ``'`` 而言这几乎测不出来：两段相邻的字面量抹出来的掩码和一段
        完全一样。**能失败的是双引号那一侧** —— 少了转义分支，
        ``"a""b"`` 会被当成两个标识符，填充结果就不是一整串 x。
        """
        assert ";" not in mask("a 'x'';y' b")
        assert mask('"a""b"') == '"xxxx"'

    def test_e_strings_honour_backslash_escapes(self) -> None:
        """``E'…'`` 认 ``\\'``，普通 ``'…'`` 不认。

        不处理它时，两个 E-string 会把闭合位置算错一位，于是第二个字面量
        里的 ``;`` **漏到掩码外面** —— 一条断言会被从字面量正中间截断。
        """
        assert ";" not in mask(r"select E'a\'x', E'b;c\'d';")[:-1]

    def test_plain_strings_do_not_honour_backslash(self) -> None:
        """``standard_conforming_strings=on``（PG9.1 起的默认）下反斜杠不是转义。
        多认一种转义会把 ``'C:\\path\\'`` 误判成未闭合。"""
        out = mask(r"a 'C:\path\' b")  # 不抛，且整个字面量被抹掉
        assert "C:" not in out
        assert out.startswith("a ") and out.endswith(" b")

    def test_quoted_identifier_keeps_its_shape(self) -> None:
        """一个列可以叫 ``"date"``。抹成空格，调用方就看不出「这里有一个词」——
        所以换成 ``x``：位置和长度照旧，而里面的 ``;`` 不再有结构含义。"""
        assert mask('select "a;b" from t') == 'select "xxx" from t'
        assert ";" not in mask('create table "t;x" (a int)')

    def test_dollar_quoted_body(self) -> None:
        """``0001_init.sql`` 的触发器函数体就是这个形状，**里面有分号**。"""
        sql = "create function f() returns trigger as $$ begin return new; end $$ language plpgsql;"
        out = mask(sql)
        assert out.count(";") == 1  # 只剩语句末尾那个
        assert out.endswith("language plpgsql;")

    def test_tagged_dollar_quote(self) -> None:
        assert ";" not in mask("$tag$ a ; b $tag$")

    def test_the_closing_tag_must_match_the_opening_one(self) -> None:
        """``$a$ … $b$ … $b$ … $a$`` 只有一个字面量，不是两个。

        「找下一个 ``$``」而不是「找同一个 tag」会在这里提前收工，
        于是外层字面量的后半段被当成 SQL。
        """
        assert mask("$a$ x $b$ y $b$ z $a$").strip() == ""

    def test_two_separate_dollar_blocks(self) -> None:
        sql = "$$a$$ ; $$b$$"
        out = mask(sql)
        assert len(out) == len(sql)
        assert out.strip() == ";"  # 两个块都没了，只剩那个顶层分号

    def test_dollar_placeholder_is_not_a_quote(self) -> None:
        """``$1`` 是参数占位，不是美元引用 —— 把它当引用会吞掉半个文件。"""
        assert mask("select $1 where x = $2") == "select $1 where x = $2"


class TestTheSwitches:
    """三个调用方要的组合都不一样 —— 每个开关都要能单独关掉。"""

    SQL = "select \"a b\", 'lit' -- c\nfrom t"

    def test_all_on_is_the_default(self) -> None:
        out = mask(self.SQL)
        assert len(out) == len(self.SQL)
        assert '"xxx"' in out
        assert "lit" not in out
        assert "-- c" not in out

    def test_comments_off_keeps_the_marker_visible(self) -> None:
        """``invariants.py`` 找 ``-- name:`` 用的就是这个组合：
        注释留着（标注住在里面），字面量抹掉（里面的标注不算数）。"""
        out = mask(self.SQL, comments=False)
        assert "-- c" in out
        assert "lit" not in out

    def test_literals_and_identifiers_off_keeps_policy_names(self) -> None:
        """对迁移做文本断言时要的是这个：注释没了，``"writer del"`` 还在。"""
        out = mask('create policy "writer del" on t; -- 注释\n', literals=False, identifiers=False)
        assert '"writer del"' in out
        assert "注释" not in out

    def test_a_literal_is_still_scanned_when_not_blanked(self) -> None:
        """关掉 ``literals`` 只是不抹它，**不是**把里面的 ``--`` 当注释开头。"""
        out = mask("a '--x' b -- real\n", literals=False)
        assert "'--x' b" in out
        assert "real" not in out


class TestItRefusesToGuess:
    @pytest.mark.parametrize(
        "sql",
        ["select 'unterminated", 'select "unterminated', "select $$ unterminated", "/* a"],
    )
    def test_unterminated_raises(self, sql: str) -> None:
        with pytest.raises(ConfigError):
            mask(sql)


def test_the_real_migration_masks_cleanly() -> None:
    """真文件跑一遍：掩码之后分号数只减不增，且括号在顶层是配平的。"""
    sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
    m = mask(sql)
    assert len(m) == len(sql)
    assert m.count(";") < sql.count(";")  # 函数体与注释里的分号被吃掉了
    assert m.count("(") == m.count(")")
