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


class TestStatusEscalation:
    """散在各处的 ``report.status = ...`` 很容易互相覆盖。

    codex 抓到的那一条就是这个形状：``revalidate_site`` 只认 ``ok``，
    于是「事件失败 + 重验证失败」保持 exit 0 —— 页面可能整整一小时
    停在旧内容上，而无人知晓。
    """

    def test_it_only_moves_towards_worse(self) -> None:
        r = RunReport(status="partial")
        r.escalate("ok")
        assert r.status == "partial", "不能被降回去"
        r.escalate("failed")
        assert str(r.status) == "failed"

    def test_events_stale_still_escalates_on_a_later_failure(self) -> None:
        """**这就是那条 SERIOUS。**"""
        r = RunReport(status="ok_events_stale")
        r.escalate("partial")
        assert r.status == "partial"
        assert r.exit_code == 1, "必须告警"

    def test_a_healthy_run_stays_healthy(self) -> None:
        r = RunReport(status="ok")
        r.escalate("ok")
        assert r.status == "ok" and r.exit_code == 0

    def test_stale_vendor_is_not_downgraded_to_partial(self) -> None:
        r = RunReport(status="stale_vendor")
        r.escalate("partial")
        assert r.status == "stale_vendor", "同级不互相覆盖，消息都留在 messages 里"


class TestCalendarRepairIsBounded:
    """§9.1.4 第 3 条：历史日增减 → **从最早变化点起**完整修复。

    只记 partial 然后建议「跑一次 backfill」是**无效的建议** ——
    backfill 走的是同一个 run_once，窗口同样被 lookback_bars 封顶。
    修订点若早于那个窗口，trading_sessions 已经改了，
    而 metrics/strength 会无限期停在旧的 ordinal 上。
    """

    def test_the_window_is_extended_back_to_the_repair_point(self) -> None:
        from pipeline.run_daily import run_once

        code = _code(run_once)
        assert "repair_from < start" in code, "必须把左端点前移"
        assert "start = repair_from" in code

    def test_every_affected_day_is_reranked(self) -> None:
        """ordinal 刚在它们脚下整体变过，「相邻 session」「20 个 session 前」
        两个判断的答案都跟着变了 —— 只重排当天是不够的。"""
        from pipeline.run_daily import run_once

        code = _code(run_once)
        assert "rerank_days" in code
        assert "for day, rows in strength_by_day.items()" in code


class TestBudgetExhaustionAbandonsTheRun:
    """§7.3.1 对预算/限流耗尽的处置是**放弃这一跑**，两个阶段一视同仁。

    价格阶段一直是直接 return；事件阶段曾经写成「把事件清空然后照常跑完」
    —— 那会把每一个标的最新行的八个事件列写成 NULL，
    **包括库里本来就有、而且完全有效的那些**。
    「中止」和「清空之后照常发布」不是一回事。
    """

    def test_both_phases_return_instead_of_continuing(self) -> None:
        import inspect

        from pipeline.run_daily import run_once

        src = inspect.getsource(run_once)
        # 两处 except 都必须以 return 收场，而不是把结果替换成空的再往下走。
        # **去掉注释再断言。** 下面那段解释里就写着 `events_by_symbol = {}`，
        # 直接对源文本断言会在散文上命中 —— 一条在注释上通过的断言
        # 什么都没验证（这个错在本仓库里犯过两次了）。
        code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
        blocks = code.split("except (BudgetExceeded, RetryAfterTooLong)")[1:]
        assert len(blocks) == 2, "价格阶段与事件阶段各一处"
        for b in blocks:
            body = b[:600]
            assert "return report" in body, "必须放弃这一跑"
            assert "events_by_symbol = {}" not in body, "不能清空事件再继续"


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
