"""§11 M5 的验收标准：**冻结时钟覆盖 4 个 cron × 2 个时区 × 2 类交易日 = 16 组**。

> M5 的 DST 测试是里程碑表里最重要的一条验收标准。
> §13 把夏令时列为「数据错误且不易察觉」，而你**无法靠等待来验证它** ——
> 要等到 3 月或 11 月。冻结时钟的单元测试是唯一能在今天就知道
> 冬令时那几条 cron 写对没有的办法。

被验证的是 ``daily.yml`` 里那四条 cron 与 §7.2 闸门 2 合在一起的**净效果**：

============  ==================  ==================
cron (UTC)    EDT（UTC-4）落点     EST（UTC-5）落点
============  ==================  ==================
``0 21``      17:00 ET ✅          16:00 ET ❌ 太早
``40 21``     17:40 ET ✅          16:40 ET ❌ 太早
``0 22``      18:00 ET ✅          17:00 ET ✅
``40 22``     18:40 ET ✅          17:40 ET ✅
============  ==================  ==================

**EST 两跑通过，EDT 四跑全部通过** —— 这正是 §7.1 写死的那句话。
而半日市（13:00 收盘 → 14:00 ET 放行）下四条全部通过，因为它们都在 14:00 之后。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from pipeline.calendar_gate import when_to_run
from pipeline.sessions import Session

UTC = ZoneInfo("UTC")
SETTLE = 60
WORKFLOWS = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"

#: daily.yml 里那四条 cron 的 (小时, 分钟)，UTC。
CRONS = ((21, 0), (21, 40), (22, 0), (22, 40))

#: EDT 与 EST 各取一个**周三**（避开月末月初的边界噪音）。
EDT_DAY = date(2026, 6, 10)
EST_DAY = date(2026, 12, 9)


def _session(d: date, *, half: bool = False) -> Session:
    close = time(13, 0) if half else time(16, 0)
    return Session(date=d, ordinal=1, close_et=close, is_half_day=half)


def _ran(day: date, hh: int, mm: int, *, half: bool = False) -> bool:
    """那一条 cron 在那一天到底放不放行。"""
    now = datetime.combine(day, time(hh, mm), tzinfo=UTC)
    return when_to_run([_session(day, half=half)], now, SETTLE).should_run


class TestTheSixteenCombinations:
    """4 个 cron × 2 个时区 × 2 类交易日。**逐一列举，不用循环里的一个断言。**"""

    @pytest.mark.parametrize(("hh", "mm"), CRONS)
    def test_edt_full_day_all_four_pass(self, hh: int, mm: int) -> None:
        assert _ran(EDT_DAY, hh, mm), f"EDT 全日市 {hh:02d}:{mm:02d}Z 应当放行"

    @pytest.mark.parametrize(
        ("hh", "mm", "expected"),
        [(21, 0, False), (21, 40, False), (22, 0, True), (22, 40, True)],
    )
    def test_est_full_day_only_the_later_two_pass(self, hh: int, mm: int, expected: bool) -> None:
        """**EST 只有后两条通过。**

        前两条落在 16:00 / 16:40 ET —— 收盘后不足 60 分钟，
        闸门 2 判 skipped_too_early，exit 0，不告警。
        把 cron 按 UTC 写死而不过闸门 2 的实现，会在这里
        **写入一个还没定稿的收盘价**。
        """
        assert _ran(EST_DAY, hh, mm) is expected

    @pytest.mark.parametrize(("hh", "mm"), CRONS)
    def test_edt_half_day_all_four_pass(self, hh: int, mm: int) -> None:
        """半日市 13:00 收盘 → 14:00 ET 放行，四条 cron 都在它之后。"""
        assert _ran(EDT_DAY, hh, mm, half=True)

    @pytest.mark.parametrize(("hh", "mm"), CRONS)
    def test_est_half_day_all_four_pass(self, hh: int, mm: int) -> None:
        assert _ran(EST_DAY, hh, mm, half=True)


class TestTheNetEffectMatchesTheSpec:
    def test_est_two_runs_edt_four_runs(self) -> None:
        """§7.1 写死的那句话：**EST 两跑通过，EDT 四跑全部通过。**"""
        assert sum(_ran(EDT_DAY, h, m) for h, m in CRONS) == 4
        assert sum(_ran(EST_DAY, h, m) for h, m in CRONS) == 2

    def test_the_first_passing_run_is_always_17_00_et(self) -> None:
        """两个季节里，第一条**放行**的 cron 都落在 17:00 ET。

        这才是 §7.1 真正承诺的东西 —— 「收盘 +60 分钟」，
        与 UTC 偏移无关。
        """
        for day in (EDT_DAY, EST_DAY):
            first = next(
                datetime.combine(day, time(h, m), tzinfo=UTC).astimezone(
                    ZoneInfo("America/New_York")
                )
                for h, m in CRONS
                if _ran(day, h, m)
            )
            assert (first.hour, first.minute) == (17, 0), f"{day} 首个放行是 {first:%H:%M} ET"


class TestTheWorkflowFileActuallyHasThoseCrons:
    """**光测逻辑不够** —— 逻辑对而 cron 写错，效果一样是当天零数据。

    这条把 ``daily.yml`` 里的那四行与上面的矩阵绑在一起：
    改了 cron 而没改测试，或反过来，都会红。
    """

    def _daily(self) -> dict[str, Any]:
        text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
        out: dict[str, Any] = yaml.safe_load(text)
        return out

    def test_the_four_crons_are_present(self) -> None:
        text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
        found = set(re.findall(r'cron:\s*"(\d+)\s+(\d+)\s+\*\s+\*\s+1-5"', text))
        assert found == {(str(m), str(h)) for h, m in CRONS}, found

    def test_weekend_is_excluded_at_the_cron_level(self) -> None:
        """周末本来就会被闸门 1 拦（``skipped_holiday``），
        但那要先起一个 runner。``1-5`` 让它连起都不起。"""
        text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
        assert text.count("* * 1-5") == len(CRONS)

    def test_concurrency_does_not_cancel_in_progress(self) -> None:
        """**取消会把 T2 打断在半路。**

        数据会完整回滚（那没问题），但 ``runs`` 里会留下一行永远停在
        ``running`` —— 而那是留给「硬崩溃」的信号。cron 延迟导致两跑重叠时
        不该发出那个信号。
        """
        cfg = self._daily()
        assert cfg["concurrency"]["cancel-in-progress"] is False
        assert cfg["concurrency"]["group"] == "markme-daily"

    def test_backfill_shares_the_concurrency_group(self) -> None:
        """回填与日常运行写同一批表，不能并发。"""
        cfg: dict[str, Any] = yaml.safe_load(
            (WORKFLOWS / "backfill.yml").read_text(encoding="utf-8")
        )
        assert cfg["concurrency"]["group"] == "markme-daily"

    def test_backfill_is_dispatch_only(self) -> None:
        """§7.4：``backfill.yml`` 只能手动触发。一条 schedule 都不该有。"""
        text = (WORKFLOWS / "backfill.yml").read_text(encoding="utf-8")
        assert "schedule:" not in text

    def test_the_secret_is_scoped_to_the_step(self) -> None:
        """§8.3：secret 挂 step 级而非 job/workflow 级 ——
        checkout 与 uv sync 都不需要它。"""
        text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
        head = text[: text.index("MARKME_DB_URL")]
        assert head.count("env:") <= 1, "MARKME_DB_URL 之前不该有 job 级 env"


class TestKeepaliveCoversTheWeekend:
    """§8.6：Supabase 的暂停计时**不认交易日**。"""

    def test_it_runs_every_day(self) -> None:
        text = (WORKFLOWS / "keepalive.yml").read_text(encoding="utf-8")
        assert re.search(r"cron:\s*'[\d ]+\* \* \*'", text), "必须含周末"

    def test_it_runs_the_invariants_not_just_a_select_one(self) -> None:
        """§12 #9 第 3 条：不变式断的是**线上数据库状态**，不是代码，
        所以不能只挂在 push 触发的 ci.yml 上。"""
        text = (WORKFLOWS / "keepalive.yml").read_text(encoding="utf-8")
        assert "pipeline.check_invariants" in text
        assert "test_db_integration.py" in text


class TestHeartbeatResetsTheCronTimer:
    def test_it_makes_a_commit_monthly(self) -> None:
        """**GitHub 在仓库 60 天无活动后自动停用 schedule 触发器**，
        而 workflow 的「运行」不算活动 —— 提交才算。
        这个项目的稳态恰恰是「跑得很好、没人再推代码」。"""
        text = (WORKFLOWS / "heartbeat.yml").read_text(encoding="utf-8")
        cfg: dict[str, Any] = yaml.safe_load(text)
        assert cfg["permissions"]["contents"] == "write"
        assert "git commit" in text
        assert re.search(r"cron:\s*'[\d ]+1 \* \*'", text), "每月一次"
