"""``run_daily`` 的退出语义与写入契约（§7.2 / §9.1.2）。

**这张表在 review 之前一条测试都没有**，而 ``run_daily`` 自己的 docstring
管它叫「整张表里最容易写错的」。四个能活下来的突变：

- ``startswith(("ok", "skipped"))`` → ``startswith("ok")``
  每个周末和假日都 exit 1 并告警；
- → ``status == "ok"``：``ok_events_stale`` 开始告警，
  而那恰好推翻模块 docstring 里整段论证；
- ``replace_strength`` 删掉那句 ``delete``：§9.1.2 要防的榜单拼接就此发生；
- ``upsert_metrics`` 的冲突目标改成 ``("symbol",)``。
"""

from __future__ import annotations

from datetime import date

import pytest

from pipeline.run_daily import RunReport
from pipeline.store import (
    METRICS_WRITE_COLUMNS,
    PRICE_WRITE_COLUMNS,
    STRENGTH_WRITE_COLUMNS,
)


def _code(fn: object) -> str:
    """函数体，**去掉 docstring**。

    直接对 ``getsource`` 断言会在注释和 docstring 上命中 ——
    而这几条测的恰恰是「SQL 里有没有那句话」。一条在散文上通过的断言
    什么都没验证。
    """
    import inspect

    src = inspect.getsource(fn)  # type: ignore[arg-type]
    doc = inspect.getdoc(fn)
    if doc:
        for line in doc.splitlines():
            src = src.replace(line, "")
    return " ".join(ln.split("#", 1)[0] for ln in src.splitlines())


class TestExitSemantics:
    """§7.2 的那张表。**只有这一处决定告警不告警。**"""

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            ("ok", 0),
            ("skipped_holiday", 0),
            ("skipped_too_early", 0),
            ("skipped_already_done", 0),
            # **这一行是最容易写错的那一行。**
            # 记成非 0 会让 §7.1 的条件重试不跳过 → 夏令时那四跑全部执行
            # 完整管道 → 18:40 那跑若降级到 Stooq，好数据被更粗的源静默覆盖。
            ("ok_events_stale", 0),
            ("stale_vendor", 1),
            ("partial", 1),
            ("failed", 1),
        ],
    )
    def test_each_status_maps_to_the_documented_exit_code(self, status: str, code: int) -> None:
        assert RunReport(status=status).exit_code == code  # type: ignore[arg-type]

    def test_every_status_in_the_schema_is_covered_here(self) -> None:
        """schema 里加了一个新状态而这里没覆盖 → 它的告警行为没人定义过。"""
        from pipeline.schema import MIGRATIONS_DIR

        sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
        start = sql.index("status") + sql[sql.index("status") :].index("in (")
        enum = sql[start : sql.index(")", start)]
        in_schema = set(__import__("re").findall(r"'(\w+)'", enum))
        covered = {
            "ok",
            "ok_events_stale",
            "skipped_holiday",
            "skipped_too_early",
            "skipped_already_done",
            "stale_vendor",
            "partial",
            "failed",
        }
        assert in_schema - {"running"} == covered, in_schema

    def test_messages_join_readably(self) -> None:
        r = RunReport(status="partial")
        r.note("a")
        r.note("b")
        assert r.message == "a；b"


class TestWriteContracts:
    """每张表的冲突目标**不同**，初稿笼统写成一个是错的（§9.1.2）。"""

    def test_strength_is_delete_then_insert_not_upsert(self) -> None:
        """只 upsert 不删，某次重跑产出的标的数比上次少时，
        上一次的尾部名次会**存活下来** —— 显示出的榜单是**两次计算的拼接**。
        而那恰恰是 §7.2「重跑、补跑…结果都一样」承诺要保证的那张表。
        """
        from pipeline.store import replace_strength

        src = _code(replace_strength)
        assert "delete from strength_daily where date" in src
        assert "on conflict" not in src, "这张表不走 upsert"

    def test_prices_and_metrics_conflict_on_symbol_and_date(self) -> None:
        from pipeline.store import upsert_metrics, upsert_prices

        for fn in (upsert_prices, upsert_metrics):
            assert '("symbol", "date")' in _code(fn), fn.__name__

    def test_prices_never_delete(self) -> None:
        """写入角色对 prices/metrics/symbols **根本没有 DELETE 权限**
        （§8.1.1 实测）—— 修复只能走 UPDATE / upsert。"""
        from pipeline.store import upsert_metrics, upsert_prices

        for fn in (upsert_prices, upsert_metrics):
            assert "delete" not in _code(fn).lower(), fn.__name__

    def test_events_are_insert_only_with_a_separate_window_delete(self) -> None:
        """幂等性来自**整窗删+插**，不是主键冲突（§3.5(1)）。"""
        from pipeline.store import delete_events_window, insert_events

        assert "on conflict" not in _code(insert_events)
        assert "event_date >= %s" in _code(delete_events_window)

    def test_symbols_are_soft_deleted(self) -> None:
        """§6.1.1 的那个例外：``delete from symbols`` 会被外键挡住，
        而且真删会让回补与历史榜单都断。"""
        from pipeline.store import sync_symbols

        src = _code(sync_symbols)
        assert "set enabled = false" in src
        assert "delete from symbols" not in src


class TestNullRowsForLaggingSymbols:
    """§7.2 闸门 3 的原话是「该标的**指标写 NULL**」—— 不是「不写」。"""

    def test_a_lagging_symbol_gets_a_full_null_row(self) -> None:
        from pipeline.config import load_config
        from pipeline.run_daily import _null_rows

        cfg = load_config()
        rows = _null_rows(["MU"], date(2024, 6, 12), cfg)
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == set(METRICS_WRITE_COLUMNS)
        assert row["symbol"] == "MU"
        assert row["rsi_14"] is None and row["alpha_annual"] is None
        # not-null default 的两列不能是 None
        assert row["extra"] == {}
        assert row["provisional_metrics"], "整行没值 → 每个软闸门指标都信不过"

    def test_the_event_columns_are_null(self) -> None:
        """不写这一行的话，昨天那行会成为该标的的最新行，
        而它带着昨天的八个事件列 —— §3.5(3) 的不变式当天就红。"""
        from pipeline.compute import EVENT_COLUMNS
        from pipeline.config import load_config
        from pipeline.run_daily import _null_rows

        row = _null_rows(["MU"], date(2024, 6, 12), load_config())[0]
        assert all(row[c] is None for c in EVENT_COLUMNS)


def test_write_column_tuples_are_disjointly_correct() -> None:
    """三张表的写入列各自独立，不该互相抄。"""
    assert "rank" in STRENGTH_WRITE_COLUMNS
    assert "rank" not in METRICS_WRITE_COLUMNS
    assert "adj_close" in PRICE_WRITE_COLUMNS
    assert "adj_close" not in METRICS_WRITE_COLUMNS
