"""SQL 解析与 §6.2 双向列校验的测试（M3）。

**这组测试取代了 M1 里那份手抄常量。** 当时 ``REAL_METRICS_COLUMNS`` 是
「数据库列」的替身，由两名 reviewer 独立核对过 —— 但那终究是第二份需要对齐
的名单，而 §6.1.1 说得很清楚：两个要对齐的地方，就是将来会不对齐的地方。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.config import STRUCTURAL_COLUMNS, ConfigError, check_columns_match, load_config
from pipeline.schema import (
    MIGRATIONS_DIR,
    metrics_daily_columns,
    normalize_identifier,
    parse_create_table_columns,
    table_names,
)
from pipeline.sqltext import mask

INIT_SQL = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")


def _statements(sql: str) -> str:
    """去掉注释并把空白折叠成单空格。

    对迁移文件做文本断言时必须先这么做，否则会在两处误判：
    注释里提到 ``force row level security`` 会被当成真的写了它；
    而为了对齐而多打的空格（``alter table symbols          enable …``）
    会让一条正确的语句匹配不上。**测试该看语句，不该看注释和缩进。**

    用 :func:`mask` 而不是按行切 ``--``：下面好几条断言是**否定式**的
    （``"password" not in``、``"force row level security" not in``、
    ``'"writer del" on metrics_daily' not in``），而否定式断言在解析出错时
    是**静默通过**的。既然这整批改动就是为了替掉那个近似，
    这里自然不该留着它。
    ``literals`` 与 ``identifiers`` 关掉，因为策略名 ``"writer del"``
    正是要断言的内容。
    """
    return " ".join(mask(sql, literals=False, identifiers=False).lower().split())


INIT_STMTS = _statements(INIT_SQL)


class TestTheRealBidirectionalCheck:
    """§6.2 校验 1/2：config 的 core 列与 ``metrics_daily`` 的实际列双向一致。

    现在两边都是**真的**：左边读 ``config/metrics.yaml``，右边读
    ``supabase/migrations/0001_init.sql``。这条测试一旦通过，
    「改了 config 忘了迁移」和「schema 建了但没人算」两个方向都被堵上。
    """

    def test_config_and_migration_agree(self) -> None:
        check_columns_match(load_config(), metrics_daily_columns())

    def test_the_two_sides_are_actually_independent(self) -> None:
        """佐证上一条不是同义反复：两边的来源确实是两个文件。"""
        declared = load_config().metrics.core_columns
        in_sql = metrics_daily_columns()
        assert declared, "config 侧不能是空集"
        assert in_sql, "SQL 侧不能是空集"
        assert declared <= in_sql, "config 声明的每一列都该在 SQL 里"
        assert (in_sql - declared) == set(STRUCTURAL_COLUMNS)


class TestParser:
    def test_finds_all_metrics_columns(self) -> None:
        cols = metrics_daily_columns()
        # 抽查几类：普通列、生成列旁边的列、jsonb、数组、事件列
        for expected in (
            "symbol",
            "date",
            "rsi_14",
            "ema60_slope_20d",
            "alpha_annual",
            "next_earnings_is_estimated",
            "extra",
            "provisional_metrics",
            "computed_at",
        ):
            assert expected in cols, expected

    def test_table_level_constraints_are_not_columns(self) -> None:
        """``primary key (symbol, date)`` 是约束，不是一个叫 ``primary`` 的列。"""
        cols = metrics_daily_columns()
        for noise in ("primary", "unique", "check", "constraint", "foreign"):
            assert noise not in cols

    def test_handles_generated_column_with_nested_parens(self) -> None:
        """``generated always as (adj_close / nullif(close, 0)) stored`` 里有嵌套括号，
        朴素的按逗号切分会在这里把一列切成两列。
        """
        cols = parse_create_table_columns(INIT_SQL, "prices_daily")
        assert "adj_factor" in cols
        assert "close" in cols
        assert "adj_close" in cols
        assert "nullif" not in cols

    def test_handles_multiline_check_constraint(self) -> None:
        """``private.runs.status`` 的 check 里有一个跨多行的 in (…) 列表。"""
        cols = parse_create_table_columns(INIT_SQL, "private.runs")
        assert "status" in cols
        assert "session_date" in cols
        assert "running" not in cols, "枚举值不是列名"

    def test_trailing_comments_do_not_confuse_paren_matching(self) -> None:
        """列定义带尾部 ``--`` 注释，而注释里可能出现括号。"""
        sql = """
        create table t (
          a int,               -- 这里有个 ( 不配对的括号
          b text not null,     -- 以及一个 )
          primary key (a)
        );
        """
        assert parse_create_table_columns(sql, "t") == ["a", "b"]

    def test_missing_table_fails_loudly(self) -> None:
        """找不到表必须抛，**不能返回空集** —— 悄悄返回空集会让双向校验永远通过。"""
        with pytest.raises(ConfigError, match="找不到"):
            parse_create_table_columns(INIT_SQL, "no_such_table")

    def test_unbalanced_parens_fail_loudly(self) -> None:
        with pytest.raises(ConfigError, match="配平"):
            parse_create_table_columns("create table t (a int,", "t")

    def test_missing_migration_file_fails_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="缺少迁移文件"):
            metrics_daily_columns(tmp_path)


class TestTheParserRefusesToGuess:
    """**这组测的是「答错」而不是「失败」。**

    一个解析失败的解析器会让 CI 红，有人会去看。一个返回了*错误列集合*的
    解析器会让 §6.2 的双向校验继续绿着 —— 而那条校验的全部价值就是
    「config 和数据库不一致时要响」。下面每一条，在按行切注释的旧实现里
    都是悄悄给出一个看起来很正常的答案。
    """

    def test_a_semicolon_in_a_function_body_does_not_end_the_search(self) -> None:
        """``$$ … ; … $$`` 在真迁移里就在 create table 前面。"""
        sql = """
        create function f() returns trigger as $$
        begin
          -- create table decoy (x int, y int);
          return new;
        end $$ language plpgsql;
        create table t (a int, b text);
        """
        assert parse_create_table_columns(sql, "t") == ["a", "b"]

    def test_a_comment_marker_inside_a_string_is_not_a_comment(self) -> None:
        """``'a--b'`` 里的 ``--`` 不开启注释；按行切会把 ``, c int)`` 一起切掉，
        于是括号永远配不平 —— 或者更糟，恰好配平出一份少了列的答案。"""
        sql = "create table t (a text default 'x--y', b int, c int);"
        assert parse_create_table_columns(sql, "t") == ["a", "b", "c"]

    def test_a_paren_inside_a_string_does_not_shift_the_depth(self) -> None:
        sql = "create table t (a text default '((', b int);"
        assert parse_create_table_columns(sql, "t") == ["a", "b"]

    def test_a_comma_inside_a_string_does_not_split_a_column(self) -> None:
        sql = "create table t (a text default 'x,y' not null, b int);"
        assert parse_create_table_columns(sql, "t") == ["a", "b"]

    def test_the_same_table_twice_is_rejected(self) -> None:
        """取第一处会返回一份**看起来很正常的**错误列集合。"""
        sql = "create table t (a int); create table t (a int, b int);"
        with pytest.raises(ConfigError, match="出现了 2 次"):
            parse_create_table_columns(sql, "t")

    def test_a_table_constraint_without_a_space_is_still_a_constraint(self) -> None:
        """``unique(a,b)`` / ``check(a>0)`` —— 取 ``split()[0]`` 会得到
        ``unique(a,b)`` 和 ``check(a``，两者都匹配不上黑名单，
        于是一条**约束被当成一列**混进列集合。"""
        assert parse_create_table_columns("create table t (a int, b int, unique(a,b));", "t") == [
            "a",
            "b",
        ]
        assert parse_create_table_columns("create table t (a int, check(a > 0));", "t") == ["a"]

    def test_a_quoted_keyword_is_a_real_column(self) -> None:
        """``"check" boolean`` 是一个合法的、就叫 check 的列。

        **这是会静默答错的那个方向。** 把它当约束滤掉 = 少报一列，
        而「schema 建了、config 没声明、没人计算」正是 §6.2 双向校验
        最该抓、也最容易因此被放过的一侧。
        """
        cols = parse_create_table_columns(
            'create table t (a int, "check" boolean, "like" text, "my col" int);', "t"
        )
        assert cols == ["a", "check", "like", "my col"]

    def test_a_quoted_identifier_keeps_its_case(self) -> None:
        """Postgres 的规则：不带引号的标识符折叠成小写，**带引号的保留大小写**。

        把 `"RSI_14"` 报成 `rsi_14`，恰好让「config 写 rsi_14、库里其实是
        RSI_14」通过 §6.2 的双向校验 —— 而线上 PostgREST 会对 rsi_14 返回 404。
        答错的方向正是这条校验唯一要抓的那个。
        """
        assert parse_create_table_columns('create table t ("RSI_14" int);', "t") == ["RSI_14"]
        assert parse_create_table_columns("create table t (RSI_14 int);", "t") == ["rsi_14"]

    def test_a_doubled_quote_inside_an_identifier_is_decoded(self) -> None:
        """``"a""b"`` 是一个叫 ``a"b`` 的列，不是叫 ``a""b`` 的。"""
        assert parse_create_table_columns('create table t ("a""b" int);', "t") == ['a"b']

    def test_ctas_is_visible_but_refuses_to_guess_its_columns(self) -> None:
        """``create table t as select …`` 建出来的是真表，同样要 drop ——
        所以 ``table_names`` 必须看得见它，否则回滚测试对这类表隐形，
        连表数都不会变。

        但它**没有列清单**，列名由那条 select 决定，解析它需要一个真正的
        SQL 引擎。所以：看得见，但拒绝猜。
        """
        assert table_names("create table archive as select 1 as id;") == ["archive"]
        assert table_names("create table archive as table source;") == ["archive"]
        with pytest.raises(ConfigError, match="CTAS"):
            parse_create_table_columns("create table archive as select 1 as id;", "archive")

    def test_exclude_is_not_reserved_so_it_can_be_a_column(self) -> None:
        """``exclude`` 与 primary/unique/check 不同：它是 unreserved 的。
        只有 ``exclude using …`` / ``exclude (…)`` 才是约束。"""
        assert parse_create_table_columns(
            "create table t (a int, exclude boolean, b int);", "t"
        ) == [
            "a",
            "exclude",
            "b",
        ]
        assert parse_create_table_columns(
            "create table t (a int, exclude using gist (a with =));", "t"
        ) == ["a"]

    def test_temp_and_unlogged_tables_are_visible(self) -> None:
        """``create temp table`` 不匹配 ``create\\s+table``。

        漏掉它，「同名表出现两次要抛」恰好在唯一会用到它的场景里失效，
        而回滚测试也会漏掉一张新加的 unlogged 表 —— 那张表对解析器是隐形的，
        连 ``len(created) == 8`` 都不会变。
        """
        assert table_names("create temp table t (z int); create table u (a int);") == ["t", "u"]
        assert table_names("create unlogged table scratch (a int);") == ["scratch"]
        with pytest.raises(ConfigError, match="出现了 2 次"):
            parse_create_table_columns("create temp table t (z int); create table t (a int);", "t")

    def test_a_dollar_inside_an_identifier_is_not_a_dollar_quote(self) -> None:
        """Postgres 允许标识符从第二个字符起含 ``$``，而它的词法器在那个位置
        会继续读标识符、而不是开一个 ``$tag$`` 块。

        不认这条边界时，下面这句会从第一个 ``$x$`` 一路抹到第二个，
        解析结果是 ``['a', 'c']`` —— 一份**看起来很正常的、少了一列的**答案。
        """
        assert parse_create_table_columns("create table t (a$x$ int, b$x$ int, c int);", "t") == [
            "a$x$",
            "b$x$",
            "c",
        ]

    def test_an_empty_column_set_is_loud(self) -> None:
        """``create table t (like other including all)`` 一列都解析不出来。
        返回空集会让双向校验永远通过。"""
        with pytest.raises(ConfigError, match="空"):
            parse_create_table_columns("create table t (like other including all);", "t")

    def test_an_item_that_is_only_a_comment_is_skipped(self) -> None:
        """尾逗号加一整行注释 —— 掩码上这一项什么都不剩。"""
        sql = "create table t (\n  a int,\n  -- 只有注释的一项\n  b int\n);"
        assert parse_create_table_columns(sql, "t") == ["a", "b"]

    def test_unbalanced_parens_name_the_table(self) -> None:
        """旧消息只说「括号没有配平」—— 在一个 400 行的迁移里毫无用处。"""
        with pytest.raises(ConfigError, match=r"create table t.*没有配平"):
            parse_create_table_columns("create table t (a int,", "t")


class TestTableNames:
    def test_lists_every_created_table(self) -> None:
        names = table_names(INIT_SQL)
        assert names == [
            "trading_sessions",
            "symbols",
            "symbol_events",
            "prices_daily",
            "metrics_daily",
            "strength_daily",
            "private.runs",
            "private.fetch_state",
        ]

    def test_quoted_table_names_are_visible(self) -> None:
        """``create table "archive" (…)`` 建出来的也是真表，也要 drop。

        只认 ``[\\w.]+`` 时它对 ``table_names`` 完全隐形，于是「回滚 drop 了
        每一张表」那条测试看不见它，而回滚后重跑 init 会撞 already exists。
        """
        assert table_names('create table "archive" (id int);') == ["archive"]
        assert table_names('create table "My Schema"."My Table" (id int);') == [
            "My Schema.My Table"
        ]

    def test_identifier_case_follows_postgres_rules(self) -> None:
        """不带引号的折叠成小写，带引号的保留大小写；``""`` 解成 ``"``。"""
        assert normalize_identifier("Private.Runs") == "private.runs"
        assert normalize_identifier('"Private"."Runs"') == "Private.Runs"
        assert normalize_identifier('"a""b"') == 'a"b'

    def test_ignores_create_table_inside_a_comment(self) -> None:
        assert table_names("-- create table decoy (x int);\ncreate table t (a int);") == ["t"]


class TestMigrationShape:
    """迁移文件本身的一些硬要求，读一遍文本就能验。"""

    def test_is_wrapped_in_a_transaction(self) -> None:
        """§9.4：每个迁移包在 begin/commit 里，部分失败能回滚。

        不建 staging（§12 #9），生产上唯一的安全网就是「错了能退」。
        """
        assert INIT_STMTS.startswith("begin;")
        assert INIT_STMTS.endswith("commit;")

    def test_does_not_contain_create_role_or_a_password(self) -> None:
        """角色由前提步骤手工创建；迁移文件不含 create role，也不含任何密码。

        迁移文件是要提交进**公开仓库**的。
        """
        assert "create role" not in INIT_STMTS
        assert "password" not in INIT_STMTS

    def test_reloads_the_postgrest_schema_cache(self) -> None:
        """经 SQL Editor 建的对象，PostgREST 不会自己发现 —— 端点会 404。"""
        assert "notify pgrst" in INIT_STMTS

    def test_every_public_table_gets_rls(self) -> None:
        """六张 public 表一张都不能漏（§9.3.2 的不变式也查这个，但那要连库）。"""
        lowered = INIT_STMTS
        for table in (
            "trading_sessions",
            "symbols",
            "symbol_events",
            "prices_daily",
            "metrics_daily",
            "strength_daily",
        ):
            assert f"alter table {table} enable row level security" in lowered, table

    def test_every_public_table_has_an_anon_read_policy(self) -> None:
        lowered = INIT_STMTS
        for table in (
            "trading_sessions",
            "symbols",
            "symbol_events",
            "prices_daily",
            "metrics_daily",
            "strength_daily",
        ):
            assert f'"public read" on {table}' in lowered, table

    def test_every_writer_table_has_matching_policies(self) -> None:
        """漏掉任何一张的 writer 策略 → RLS 默认拒绝 → INSERT 在 T2 里失败
        → **整个当日事务回滚、当天零数据**。上一轮 trading_sessions 就是这么掉的。
        """
        lowered = INIT_STMTS
        writable = {
            "symbols": ("sel", "ins", "upd"),
            "prices_daily": ("sel", "ins", "upd"),
            "metrics_daily": ("sel", "ins", "upd"),
            "strength_daily": ("sel", "ins", "upd", "del"),
            "trading_sessions": ("sel", "ins", "upd", "del"),
            "symbol_events": ("sel", "ins", "upd", "del"),
        }
        for table, verbs in writable.items():
            for verb in verbs:
                assert f'"writer {verb}" on {table}' in lowered, f"{table} 缺 {verb} 策略"

    def test_delete_is_granted_to_exactly_three_tables(self) -> None:
        """只有可从上游完整再生的三张表才给 DELETE（§8.1.1）。

        价格层与指标层是原始事实与可重算派生层，写入凭证被攻陷时
        不该有能力把它们抹掉。
        """
        lowered = INIT_STMTS
        for table in ("strength_daily", "trading_sessions", "symbol_events"):
            assert f'"writer del" on {table}' in lowered, table
        for table in ("symbols", "prices_daily", "metrics_daily"):
            assert f'"writer del" on {table}' not in lowered, f"{table} 不该有 DELETE"

    def test_the_writer_can_read_the_view(self) -> None:
        """§9.3.2 有两条不变式查 ``v_strength_enriched``，而 ``keepalive.yml``
        是以 **pipeline_writer** 身份跑它们的。

        实测过漏掉这一条的样子：那两条以 postgres 跑全过，换成 writer 立刻变成
        「permission denied for view v_strength_enriched」。授权清单只列了
        六张表 —— 和 §9.3 那个 REVOKE 漏掉视图的洞是同一个形状，漏在另一侧。
        """
        assert "grant select on v_strength_enriched to pipeline_writer" in INIT_STMTS

    def test_the_view_is_revoked_from_anon(self) -> None:
        """同一个形状的另一侧：收权也不能只列六张表。"""
        assert "revoke all on v_strength_enriched from anon, authenticated" in INIT_STMTS

    def test_runs_lives_in_the_private_schema(self) -> None:
        """§9.3.1：runs 不走 SECURITY DEFINER 视图，改放 PostgREST 不暴露的 schema。"""
        assert "create table private.runs" in INIT_STMTS
        assert "create table runs" not in INIT_STMTS

    def test_no_force_row_level_security(self) -> None:
        """FORCE 影响的是表属主，而 pipeline_writer 不是属主、本来就受 RLS 约束。
        它不增加任何防护，只增加策略维护的失败面。
        """
        assert "force row level security" not in INIT_STMTS

    def test_rollback_exists_and_drops_everything_init_creates(self) -> None:
        """没有回滚脚本的迁移不允许执行（AGENTS.md / §9.4）。

        表清单**从 init 自己推**，不手抄。手抄的那一份正是 M3 要消灭的东西：
        0001 里加一张新表、回滚里忘了 drop，一份手抄的清单会让这条测试
        照样全绿，而下一次「回滚→重跑」会撞 `already exists`。
        """
        created = table_names(INIT_SQL)
        assert len(created) == 8, f"init 建表数变了：{created}"
        rollback = _statements((MIGRATIONS_DIR / "0001_rollback.sql").read_text(encoding="utf-8"))
        for table in created:
            assert f"drop table if exists {table}" in rollback, table
        assert "drop view if exists v_strength_enriched" in rollback
        # 触发器随表一起 drop，但函数不会 —— 不清掉，重跑 0001 会撞 already exists
        assert "drop function if exists touch_updated_at" in rollback
        assert "drop function if exists touch_computed_at" in rollback
        # 回滚不该删除 pipeline_writer：那个角色不是本迁移的产物
        assert "drop role" not in rollback
        # init 收了几类默认权限，回滚就要还几类 —— 少还一类，库回滚后
        # 与迁移前的状态不一致，而那种「回滚了但没回滚干净」最难排查。
        for kind in ("tables", "sequences", "functions"):
            assert f"revoke all on {kind} from anon, authenticated" in INIT_STMTS, kind
            assert f"grant all on {kind} to anon, authenticated" in rollback, kind
