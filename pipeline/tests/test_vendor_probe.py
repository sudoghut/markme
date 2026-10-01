"""探针的两条硬约束：**只读**，且**不挡管道**。

探针是临时诊断工具，每交易日跑四次、每次近两小时（权限是 `contents: read`，数据走 artifact）。
这两条如果破了，代价都落在生产上：一条会让诊断工具拿到写库能力，
另一条会把管道堵在它后面近两小时 —— 而管道当天的放行窗口只有两跑。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from pipeline.vendor_probe import FIELDS, sample_once

WORKFLOWS = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
PROBE = WORKFLOWS / "vendor-probe.yml"


class TestItCannotTouchTheDatabase:
    """**这是探针唯一真正危险的地方。**

    它只是来看数据源什么时候定稿的，没有任何理由拿到 `MARKME_DB_URL`。
    §8.3 要求 secret 挂 step 级；这里更进一步：**整个文件里一次都不该出现**。
    """

    def test_no_database_secret_anywhere_in_the_workflow(self) -> None:
        text = PROBE.read_text(encoding="utf-8")
        for forbidden in ("MARKME_DB_URL", "SUPABASE", "POSTGRES", "DATABASE_URL"):
            assert forbidden not in text, f"探针不该碰 {forbidden}"

    def test_the_module_never_imports_the_store(self) -> None:
        """连 import 都不该有 —— `pipeline.store` 是唯一能写库的那一层。"""
        src = (Path(__file__).resolve().parent.parent / "vendor_probe.py").read_text(
            encoding="utf-8"
        )
        assert "pipeline.store" not in src
        assert "psycopg" not in src


class TestItDoesNotBlockThePipeline:
    def _cfg(self) -> dict[str, Any]:
        out: dict[str, Any] = yaml.safe_load(PROBE.read_text(encoding="utf-8"))
        return out

    def test_it_is_not_in_the_daily_concurrency_group(self) -> None:
        """同组会把管道堵在探针后面近两小时，而管道当天只有两跑放行。"""
        assert self._cfg()["concurrency"]["group"] != "markme-daily"

    def test_it_has_a_timeout(self) -> None:
        """没有 timeout 的长任务会把 runner 占到 6 小时上限。"""
        assert self._cfg()["jobs"]["probe"]["timeout-minutes"] <= 180


#: **必须是 float 的 NaN，不能是 Python 的 None。**
#: `None` 会让那一列的 dtype 变成 `object`，于是 `_finite` 走的是
#: `float(None)` 抛 TypeError 那条分支 —— 而真实 yfinance 的半成品 bar 是
#: `float64` 的 NaN，走的是另一条。用 None 的话，**这个探针存在的全部理由
#: 那条分支从未被任何测试执行过**。
NAN = float("nan")


def _frame(day: date, *, close: float, prior: int = 3) -> pd.DataFrame:
    """构造 ``prior`` 根已定稿的历史 bar + ``day`` 当天那一根。

    ``close=NAN`` 模拟**半成品**：有 Open/Volume，没有收盘。
    历史那几根永远是完整的 —— 它们是 ``prior_bars`` 那一列的来源。
    """
    days = [day - timedelta(days=prior - i) for i in range(prior)] + [day]
    closes = [700.0 + i for i in range(prior)] + [close]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
    return pd.DataFrame(
        {
            ("Open", "QQQ"): [740.17] * len(days),
            ("High", "QQQ"): [741.0] * len(days),
            ("Low", "QQQ"): [735.0] * len(days),
            ("Close", "QQQ"): closes,
            ("Adj Close", "QQQ"): closes,
            ("Volume", "QQQ"): [26610972.0] * len(days),
        },
        index=idx,
    )


class _Recorder:
    """记下 `sample_once` 到底拿什么参数去问供应商。

    模块 docstring 的核心主张是「**逐字照抄 `fetch.py` 的那组参数**，
    否则测到的是另一件事」。不断言参数的话，那句话零覆盖 ——
    实测把 `auto_adjust` 翻成 True、把 `end` 少加一天、把 `group_by` 换掉，
    测试全都照样绿，而每一个都会让两周的数据变成一张全是同一个值的表。
    """

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.kwargs: dict[str, Any] = {}
        self.args: tuple[Any, ...] = ()

    def __call__(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        self.args, self.kwargs = args, kwargs
        return self.frame


class TestItAsksTheVendorExactlyWhatProductionAsks:
    """观测对象必须和生产走同一条路径，否则测到的是另一件事。"""

    DAY = date(2026, 9, 29)

    def _record(self) -> _Recorder:
        rec = _Recorder(_frame(self.DAY, close=NAN))
        sample_once(["QQQ"], self.DAY, now=datetime(2026, 9, 30, 1, 5, tzinfo=UTC), download=rec)
        return rec

    def test_auto_adjust_is_false_like_fetch(self) -> None:
        """翻成 True，yfinance 就不给 `Adj Close` 列 —— 两周数据全写「从未结算」。"""
        assert self._record().kwargs["auto_adjust"] is False

    def test_end_is_exclusive_so_the_session_day_is_included(self) -> None:
        """`end` 是**开区间**。少加一天 → 空帧 → `has_row` 恒 0。

        `fetch.py` 用粗体警告过这个 off-by-one：它会静默丢掉**今天那一行**。
        """
        rec = self._record()
        assert rec.kwargs["end"] == (self.DAY + timedelta(days=1)).isoformat()

    def test_it_asks_for_prior_days_too(self) -> None:
        """只抓 1 天的话，限流返回的空帧与「还没结算」在 CSV 里分不开。"""
        assert self._record().kwargs["start"] < self.DAY.isoformat()

    def test_the_column_layout_matches_fetch(self) -> None:
        rec = self._record()
        assert rec.kwargs["group_by"] == "column"
        assert rec.kwargs["threads"] is False

    def test_it_does_not_hammer_the_vendor(self) -> None:
        """采样间隔是 15 分钟。改成 1 秒 = 8 秒内打 8 次。"""
        from pipeline import vendor_probe

        assert vendor_probe.INTERVAL_SECONDS >= 5 * 60


class TestItMeasuresTheThingWeCareAbout:
    """探针必须能把**半成品 bar** 和**定稿 bar** 分开 —— 那是它存在的全部理由。

    关键在于它**绕开了 `pipeline.fetch`**：`fetch.py` 会把 `adj_close` 为 NaN 的
    行整行丢掉，走那条路的话，要观测的东西在被测量之前就消失了。
    """

    DAY = date(2026, 9, 29)

    def test_a_half_formed_bar_is_recorded_as_present_but_unsettled(self) -> None:
        rows = sample_once(
            ["QQQ"],
            self.DAY,
            now=datetime(2026, 9, 30, 1, 5, tzinfo=UTC),
            download=lambda *a, **k: _frame(self.DAY, close=NAN),
        )
        assert len(rows) == 1
        row = rows[0].as_row()
        assert row["has_row"] == 1, "这一行在，只是没结算 —— 与「供应商没给这一天」是两件事"
        assert row["prior_bars"] == 3, "历史那几根在 → 这次请求确实成功了"
        assert row["adj_close"] is None, "闸门 3 看的就是这一格"
        assert row["open"] == pytest.approx(740.17), "开盘价要记下来 —— 它定稿前会变"
        assert row["volume"] == pytest.approx(26610972.0)

    def test_a_settled_bar_is_recorded_with_its_close(self) -> None:
        rows = sample_once(
            ["QQQ"],
            self.DAY,
            now=datetime(2026, 9, 30, 2, 9, tzinfo=UTC),
            download=lambda *a, **k: _frame(self.DAY, close=737.93),
        )
        row = rows[0].as_row()
        assert row["adj_close"] == pytest.approx(737.93)
        assert row["close"] == pytest.approx(737.93)

    def test_a_missing_day_is_recorded_as_absent(self) -> None:
        rows = sample_once(
            ["QQQ"],
            self.DAY,
            now=datetime(2026, 9, 30, 0, 0, tzinfo=UTC),
            download=lambda *a, **k: pd.DataFrame(),
        )
        row = rows[0].as_row()
        assert row["has_row"] == 0
        assert row["adj_close"] is None
        assert row["prior_bars"] == 0, "一根历史都没有 → 是这次请求失败了，不是供应商没发"

    def test_every_row_has_every_column(self) -> None:
        """列缺一个，几天后的分析就得猜 —— 而那时已经没法回头重采。"""
        rows = sample_once(
            ["QQQ"],
            self.DAY,
            now=datetime(2026, 9, 30, 1, 5, tzinfo=UTC),
            download=lambda *a, **k: _frame(self.DAY, close=NAN),
        )
        assert set(rows[0].as_row()) == set(FIELDS)

    def test_the_timestamp_is_the_real_wall_clock_not_the_cron_slot(self) -> None:
        """**GitHub 会迟到 3 小时以上**，预定时刻没有意义。"""
        at = datetime(2026, 9, 30, 1, 5, tzinfo=UTC)
        row = sample_once(
            ["QQQ"], self.DAY, now=at, download=lambda *a, **k: _frame(self.DAY, close=NAN)
        )[0].as_row()
        assert row["sampled_at_utc"].startswith("2026-09-30T01:05")
        assert "21:05" in row["sampled_at_et"], "ET 那一列是分析时真正要看的"
