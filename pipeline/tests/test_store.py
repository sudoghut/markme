"""``pipeline/store.py`` 的纯逻辑测试（§7.3.1 的差分、§9.1.3 的 sanitizer 接线）。

对着**真实数据库**的那一组在 ``test_store_integration.py`` 里 ——
T1/T2/T3 的原子性只能连库验，断言 SQL 文本什么都证明不了。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import numpy as np
import pytest

from pipeline.store import (
    METRICS_WRITE_COLUMNS,
    PRICE_WRITE_COLUMNS,
    STRENGTH_WRITE_COLUMNS,
    diff_prices,
)
from pipeline.store import _rows_to_tuples as rows_to_tuples

D1, D2 = date(2024, 6, 3), date(2024, 6, 4)


def _row(sym: str, d: date, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": sym,
        "date": d,
        "close": 100.0,
        "adj_close": 99.0,
        "source": "yfinance",
    }
    base.update(kw)
    return base


class TestWriteColumnsMatchTheSchema:
    """写入列必须是 schema 的子集，且**排除生成列与触发器维护的列**。"""

    def test_generated_and_trigger_columns_are_not_written(self) -> None:
        assert "adj_factor" not in PRICE_WRITE_COLUMNS, "生成列写不得"
        assert "updated_at" not in PRICE_WRITE_COLUMNS, "触发器维护"
        assert "computed_at" not in METRICS_WRITE_COLUMNS, "触发器维护"
        assert "id" not in STRENGTH_WRITE_COLUMNS

    def test_write_columns_are_real_columns(self) -> None:
        from pipeline.schema import MIGRATIONS_DIR, parse_create_table_columns

        sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
        for table, cols in (
            ("prices_daily", PRICE_WRITE_COLUMNS),
            ("metrics_daily", METRICS_WRITE_COLUMNS),
            ("strength_daily", STRENGTH_WRITE_COLUMNS),
        ):
            actual = set(parse_create_table_columns(sql, table))
            assert set(cols) <= actual, f"{table}: {set(cols) - actual}"

    def test_every_metrics_column_is_written(self) -> None:
        """反方向：schema 有而这里没写的列，会**永远是 NULL** 而没人发现。"""
        from pipeline.schema import MIGRATIONS_DIR, parse_create_table_columns

        sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
        actual = set(parse_create_table_columns(sql, "metrics_daily"))
        assert actual - set(METRICS_WRITE_COLUMNS) == {"computed_at"}


class TestTheSanitizerIsOnTheWritePath:
    """§9.1.3：写入边界是**唯一的**窄入口。"""

    def test_nan_becomes_none(self) -> None:
        rows = [{"symbol": "X", "date": D1, "rsi_14": float("nan"), "n_obs": np.int64(7)}]
        out = rows_to_tuples(rows, ("symbol", "date", "rsi_14", "n_obs"))
        assert out == [("X", D1, None, 7)]
        assert type(out[0][3]) is int, "psycopg 不认 np.int64"

    def test_extra_jsonb_is_serialised_and_cleaned(self) -> None:
        """``extra`` 走 ``json.dumps``，而裸 ``NaN`` 不是合法 JSON。"""
        rows = [{"symbol": "X", "extra": {"a": float("inf"), "b": 1.0}}]
        out = rows_to_tuples(rows, ("symbol", "extra"))
        assert json.loads(out[0][1]) == {"a": None, "b": 1.0}

    def test_a_null_extra_stays_null_not_the_string_null(self) -> None:
        out = rows_to_tuples([{"symbol": "X", "extra": None}], ("symbol", "extra"))
        assert out[0][1] is None

    def test_missing_columns_become_none(self) -> None:
        out = rows_to_tuples([{"symbol": "X"}], ("symbol", "date", "rsi_14"))
        assert out == [("X", None, None)]


class TestPriceDiff:
    """§7.3.1：**只 upsert 与库里真正不同的行。**"""

    def test_unchanged_rows_are_not_rewritten(self) -> None:
        """正常日子只有今天那一行是新的，其余 399 行原样不变 →
        写入量从 400 行降到 1 行。"""
        fetched = [_row("A", D1), _row("A", D2)]
        existing = {("A", D1): _row("A", D1)}
        out = diff_prices(fetched, existing)
        assert out.unchanged == 1
        assert [r["date"] for r in out.changed] == [D2]

    def test_a_changed_adj_close_is_written(self) -> None:
        out = diff_prices([_row("A", D1, adj_close=98.0)], {("A", D1): _row("A", D1)})
        assert out.n_changed == 1

    def test_a_source_change_is_written(self) -> None:
        """整窗降级换了源 —— 即便价格恰好相同，``source`` 列也要更新，
        否则 §3.0 规则 3 的「窗口内源唯一」在库里就对不上。"""
        out = diff_prices([_row("A", D1, source="stooq")], {("A", D1): _row("A", D1)})
        assert out.n_changed == 1

    def test_a_dividend_rewrite_shows_up_as_a_factor_change(self) -> None:
        """§3.0 规则 2 的探测器：MSFT 除息当日，供应商**重写全部历史**。"""
        out = diff_prices(
            [_row("A", D1, adj_close=98.0)],  # close 不变，adj_close 变 → 因子变了
            {("A", D1): _row("A", D1, adj_close=99.0)},
        )
        assert out.factor_changed == ("A",)

    def test_a_plain_price_move_is_not_a_factor_change(self) -> None:
        """两列同比例变化 = 只是价格变了，因子没动。"""
        out = diff_prices(
            [_row("A", D1, close=200.0, adj_close=198.0)],
            {("A", D1): _row("A", D1, close=100.0, adj_close=99.0)},
        )
        assert out.n_changed == 1
        assert out.factor_changed == ()

    def test_null_close_does_not_silently_mean_unchanged(self) -> None:
        """**「NULL 比 NULL」当成「没变」是 §3.0 规则 2 明确警告过的。**

        Stooq 行的 ``close`` 是 NULL，生成列 ``adj_factor`` 随之为 NULL ——
        不会报错，但也比不出任何东西。用 ``adj_close`` 逐行比对兜底。
        """
        old = _row("A", D1, close=None, adj_close=99.0, source="stooq")
        new = _row("A", D1, close=None, adj_close=97.0, source="stooq")
        out = diff_prices([new], {("A", D1): old})
        assert out.n_changed == 1
        assert out.factor_changed == ("A",)

    def test_two_stooq_rows_with_the_same_price_are_unchanged(self) -> None:
        old = _row("A", D1, close=None, adj_close=99.0, source="stooq")
        out = diff_prices([dict(old)], {("A", D1): old})
        assert out.unchanged == 1
        assert out.factor_changed == ()

    def test_a_brand_new_symbol_is_all_changed(self) -> None:
        out = diff_prices([_row("B", D1), _row("B", D2)], {})
        assert out.n_changed == 2
        assert out.unchanged == 0

    def test_float_noise_does_not_count_as_a_change(self) -> None:
        out = diff_prices(
            [_row("A", D1, adj_close=99.0 + 1e-12)], {("A", D1): _row("A", D1, adj_close=99.0)}
        )
        assert out.unchanged == 1

    def test_a_nan_price_is_treated_as_missing_not_equal(self) -> None:
        out = diff_prices([_row("A", D1, adj_close=float("nan"))], {("A", D1): _row("A", D1)})
        assert out.n_changed == 1


def test_truncate_keeps_messages_bounded() -> None:
    """``runs.message`` 是给人看的，也不该把一条 10 万字的堆栈塞进库。"""
    from pipeline.store import _truncate

    assert _truncate(None) is None
    assert _truncate("abc") == "abc"
    long = "x" * 5000
    assert len(_truncate(long) or "") == 4000


@pytest.mark.parametrize(
    "status",
    [
        "running",
        "ok",
        "ok_events_stale",
        "skipped_holiday",
        "skipped_too_early",
        "skipped_already_done",
        "stale_vendor",
        "partial",
        "failed",
    ],
)
def test_every_status_is_allowed_by_the_schema_check(status: str) -> None:
    """``runs.status`` 有 ``check`` 枚举约束（§9.1）。

    代码里出现一个 schema 不认的状态值，会在**写终态那一刻**才炸 ——
    也就是整条管道跑完之后，而且那次失败本身也写不进去。
    """
    from pipeline.schema import MIGRATIONS_DIR

    sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
    assert f"'{status}'" in sql, f"{status} 不在 0001_init.sql 的枚举里"
