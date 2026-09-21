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


class TestEventsStalePublishesNulls:
    """§3.5(4)：事件抓取失败时「核心价格与四个核心指标照常写入，**事件列写 NULL**」。

    **不能把库里的旧事件再发布一遍。** 抓取失败的时候，库里那条「下一次财报」
    恰恰最可能是已经被改期、已经作废的那一条 —— 而改期正是每周去抓一次的
    全部理由。拿它算出的倒计时会走向一个不存在的日子，到期后翻成
    「财报后 1 天」，**播报一场从未发生的财报**。

    §9.3.2 那条「倒计时必须与 symbol_events 一致」**抓不到它**：
    它比的正是同一条陈旧的行，于是完全自洽。所以另加了一条不变式。
    """

    def test_the_distances_are_cleared_when_the_fetch_failed(self) -> None:
        from pipeline.run_daily import run_once

        code = _code(run_once)
        i = code.index('escalate("ok_events_stale")')
        assert "events_by_symbol = {}" in code[i : i + 400], "失败时必须清空，不能沿用旧值"

    def test_the_invariant_exists_and_is_independent(self) -> None:
        from pipeline.invariants import parse_invariants

        names = [a.name for a in parse_invariants()]
        assert any("ok_events_stale" in n for n in names), names


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
        # 写入起点前移到修复点（抓取起点还要再往前一个预热窗口，见下一条）。
        assert "min(start, repair_from)" in code, "必须把写入起点前移到修复点"
        assert "write_from < start" in code

    def test_the_repair_window_carries_warm_up_history(self) -> None:
        """**预热不是可选项。**

        把抓取起点直接设成 repair_from，EMA/RSI/alpha 会在修复点上
        从冷启动开始算 —— 一批本来正确的行被 NULL 或冷启动值覆盖，
        然后还照这个结果重排了名次。
        """
        from pipeline.run_daily import run_once

        code = _code(run_once)
        assert "write_from" in code, "写入起点与抓取起点必须分开"
        assert "lookback_window(sessions, write_from" in code, "修复也要取一整个预热窗口"
        assert 'r["date"] >= write_from' in code, "预热区只用于计算，不写库"


class TestTheWholeWindowIsReranked:
    """§3.0 规则 2 的那张表：三层**同样日期范围**。

    只排当天时，一次除息会让供应商追溯改写全部历史复权因子 ——
    历史 ``mom_20`` 变了，而 ``strength_daily`` 还留着旧名次、旧 top-N 成员，
    以及由它们派生的 ``days_in_top_n``。代码甚至**检测到了**因子变化，
    却只是记了一笔。
    """

    def test_rerank_covers_the_window_not_just_today(self) -> None:
        from pipeline.run_daily import run_once

        code = _code(run_once)
        assert "write_from <= s.date <= session.date" in code, "整窗重排"
        assert "for day, rows in strength_by_day.items()" in code

    def test_removed_sessions_have_their_ranking_rows_deleted(self) -> None:
        """日历里消失的历史日，榜单行会变成**孤儿**。

        基表里还在（于是 §9.3.2 那条「每个 strength_daily.date 都必须在
        trading_sessions 里存在」会变红），而 ``v_strength_enriched`` 的
        INNER join 里已经没有它 —— 一行公开的、永远对不上的历史数据。
        """
        from pipeline.run_daily import run_once

        code = _code(run_once)
        assert "revision.removed_historical" in code
        assert "replace_strength(conn, gone, [])" in code

    def test_the_spec_table_says_the_same_range(self) -> None:
        """把依据钉在文档上 —— 这条约束将来最可能被当成「性能优化」删掉。"""
        from pathlib import Path

        doc = (Path(__file__).resolve().parents[2] / "docs" / "create-project.md").read_text(
            encoding="utf-8"
        )
        assert "同样日期范围，**按日期删+插，全池重排**" in doc


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
            # **只看这个 except 块自己的块体** —— 切到第一个 return report 为止。
            # 取一个固定字数的窗口会滑进后面的代码：`ok_events_stale` 那条路径
            # 里有一句**合法的** `events_by_symbol = {}`（§3.5(4) 要求事件列写
            # NULL），而这条断言针对的是「预算耗尽时别清空事件再继续」。
            assert "return report" in b, "必须放弃这一跑"
            body = b[: b.index("return report")]
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


class TestLaggingSymbolsKeepTheirHistory:
    """闸门 3 判的是**新鲜度**，不是可信度。

    曾经这里是 ``prices = prices[~prices["symbol"].isin(lagging)]`` ——
    把落后标的的**整个窗口**丢掉，而只用 ``_null_rows`` 补回
    ``session.date`` 那一行。后果按天计是隐形的，按修复计是灾难性的：

    一次日历修复会把 rerank 窗口左移几百天，于是那几百天的榜单
    **每一天**都被重写成「没有这个标的」，其余名次集体上移。
    修复跑结束后窗口缩回滚动 400 根，比它更老的那些天**再也不会被重排** ——
    错名次就是最终状态，而 ``days_in_top_n`` / ``rank_delta_1d``
    正是从这些行导出的。

    ``invariants.sql`` 一条都抓不到：涉及榜单的那几条都是
    ``where date = max(date)``，而「视图不得比基表少行」两边同减、计数相等。
    """

    def test_run_once_does_not_drop_the_whole_window(self) -> None:
        """负向断言。**这一条是本文件里唯一合理的 grep** ——

        它钉的不是「代码里有某句话」，而是「代码里**不许**再出现那句话」，
        而那正是 grep 唯一真正可靠的用法。
        """
        from pipeline.run_daily import run_once

        code = _code(run_once)
        assert "isin(lagging)" not in code, "落后标的的历史 bar 不能被整窗丢掉"

    # 「落后标的的历史行仍然参与排名」这件事的**行为**测试在
    # `test_run_once_behavior.py` 里。曾经放在这里的那条是**恒真**的：
    # 它只 import compute_strength 自己拼一个 metrics 列表，从头到尾没碰过
    # `run_once` —— 把 `prices[~prices["symbol"].isin(lagging)]` 原样加回去，
    # 它照样绿。（顺带它还依赖 universe.yaml 里前三个字典序标的恰好都是 stock。）


class TestTheReadTransactionIsActuallyReleased:
    """注释承诺放掉的事务，得真的有一行 ``rollback``。

    ``_already_done`` 那条 SELECT 会开一个事务；连接是 ``autocommit=False``，
    于是它一路挂到整窗抓取结束 —— 5–10 分钟的 idle-in-transaction，
    正是 pooler 的空闲事务杀手最爱的形状，被杀的表现是一次健康运行报 ``failed``。
    这段注释存在过一阵子，**底下却没有代码**。
    """

    def test_a_rollback_follows_the_already_done_check(self) -> None:
        import inspect

        from pipeline.run_daily import run_once

        src = inspect.getsource(run_once)
        after = src.split("_already_done(conn, session.date)", 1)[1]
        # **先断言分隔符还在。** `str.split` 在找不到分隔符时返回单元素列表，
        # `[0]` 于是变成「从这里到函数末尾的全部源码」—— 那里面还有另外三处
        # rollback，断言会**静默退化成恒真**。这个仓库已经三次栽在
        # 「测试在散文上通过」上，这次不让它靠一句中文注释活着。
        marker = "# 3. 抓取整窗"
        assert marker in after, f"分隔符 {marker!r} 不在了，这条断言的作用域已经失效"
        head = after.split(marker, 1)[0]
        assert head.count("conn.rollback()") >= 2, (
            "跳过分支一次、继续往下走的那条路也要一次 —— 注释承诺了就要有代码"
        )


def test_write_column_tuples_are_disjointly_correct() -> None:
    """三张表的写入列各自独立，不该互相抄。"""
    assert "rank" in STRENGTH_WRITE_COLUMNS
    assert "rank" not in METRICS_WRITE_COLUMNS
    assert "adj_close" in PRICE_WRITE_COLUMNS
    assert "adj_close" not in METRICS_WRITE_COLUMNS
