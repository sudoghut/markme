"""§11 M5 的验收标准：**冻结时钟覆盖 cron × 时区 × 交易日类型**。

> M5 的 DST 测试是里程碑表里最重要的一条验收标准。
> §13 把夏令时列为「数据错误且不易察觉」，而你**无法靠等待来验证它** ——
> 要等到 3 月或 11 月。冻结时钟的单元测试是唯一能在今天就知道
> 冬令时那几条 cron 写对没有的办法。

2026-09-30 起这份测试多了**两个维度**，都是实测逼出来的：

1. **不得跨 ET 午夜。** ``calendar_gate.when_to_run`` 用 ``now_et.date()``
   找会话，跑过午夜 ET 就会挑到第二天那一场 —— 判 ``skipped_too_early``，
   **exit 0、不告警、当天数据永久丢失**。这是比「太早」更坏的一种失败：
   太早会重试，越界不会。
2. **GitHub cron 会迟到。** 实测 2026-09-25…09-30 连续多日延后约 3–3.6 小时。
   一张只按「准时」算过的落点表，在生产里是另一张表。

M13（docs/provisional-close.md）把 ``settle_minutes`` 改回 60：收盘 +1 小时就出
**临时**结果（收盘价取自分钟线），定稿留给 ``final_settle_minutes``（330）之后的日线。
晚间 cron 加了一条 ``0 21``，于是矩阵是 **8 条 cron × 2 个时区 × 2 种延迟 = 32 格**
（全日市），外加半日市的覆盖度断言，以及**每天早上两条定稿 cron** 的落点断言。

跨午夜那一维仍然要测 —— 闸门照旧会把午夜后的那一跑判给第二天 —— 但它的后果
从「当天数据永久丢失」变成了「转为定稿跑」：闸门没放行而上一个 session 还没全员
定稿时，``run_daily`` 钳到那一天去定稿（``test_run_once_behavior.py`` 里测）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from pipeline.calendar_gate import ET, GateDecision, when_to_run
from pipeline.sessions import Session

UTC = ZoneInfo("UTC")

#: 与 `config/app.yaml` 的 settle_minutes 同步。全日市 → 17:00 ET 放行（临时结果）。
SETTLE = 60

WORKFLOWS = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"

#: daily.yml 里那八条晚间 cron：(小时, 分钟, 相对会话日的 UTC 天偏移)。
#: 偏移 1 的那几条写的是 `2-6`（周二到周六）—— 02:00Z 周六 = 21:00 ET 周五。
CRONS: tuple[tuple[int, int, int], ...] = (
    (21, 0, 0),
    (22, 0, 0),
    (23, 0, 0),
    (0, 0, 1),
    (1, 0, 1),
    (2, 0, 1),
    (3, 30, 1),
    (4, 30, 1),
)

#: GitHub 的两种落点：准时，和实测的迟到 3 小时。
DELAYS_HOURS = (0, 3)

#: EDT 与 EST 各取一个**周三**（次日也是交易日，避开周末边界）。
EDT_DAY = date(2026, 6, 10)
EST_DAY = date(2026, 12, 9)


def _session(d: date, ordinal: int = 1, *, half: bool = False) -> Session:
    close = time(13, 0) if half else time(16, 0)
    return Session(date=d, ordinal=ordinal, close_et=close, is_half_day=half)


def _fired_at(day: date, cron: tuple[int, int, int], delay_h: float) -> datetime:
    """这条 cron 为 ``day`` 那一场实际在什么时刻执行（UTC）。"""
    hh, mm, day_off = cron
    return datetime.combine(day + timedelta(days=day_off), time(hh, mm), tzinfo=UTC) + timedelta(
        hours=delay_h
    )


def _decide(
    day: date, cron: tuple[int, int, int], delay_h: float, *, half: bool = False
) -> GateDecision:
    """把**当天和次日两场**都喂进去 —— 生产里 sessions 是全量的。

    只喂一场会让「跨午夜」伪装成 ``skipped_holiday``，而真实症状是
    ``skipped_too_early``（挑到了第二天那一场）。测试要复现真实症状。
    """
    sessions = [_session(day, 1, half=half), _session(day + timedelta(days=1), 2, half=half)]
    return when_to_run(sessions, _fired_at(day, cron, delay_h), SETTLE)


def _ran_for_that_session(
    day: date, cron: tuple[int, int, int], delay_h: float, *, half: bool = False
) -> bool:
    """**放行，且放行的是 ``day`` 那一场。** 两个条件缺一不可。"""
    d = _decide(day, cron, delay_h, half=half)
    return d.should_run and d.session is not None and d.session.date == day


#: 全日市的落点表，与 `daily.yml` 头部那张表逐格对应。
#: 键是 (时区标记, 延迟小时)，值是八条 cron 各自放不放行。
_T, _F = True, False
FULL_DAY_TABLE: dict[tuple[str, int], tuple[bool, ...]] = {
    #             21:00 22:00 23:00 00:00 01:00 02:00 03:30 04:30 (UTC)
    ("EDT", 0): (_T, _T, _T, _T, _T, _T, _T, _F),
    ("EDT", 3): (_T, _T, _T, _T, _F, _F, _F, _F),
    ("EST", 0): (_F, _T, _T, _T, _T, _T, _T, _T),
    ("EST", 3): (_T, _T, _T, _T, _T, _F, _F, _F),
}

DAYS = {"EDT": EDT_DAY, "EST": EST_DAY}


class TestTheLandingTable:
    """8 条 cron × 2 个时区 × 2 种延迟。**逐格列举，不靠循环里的一个断言。**"""

    @pytest.mark.parametrize(("zone", "delay"), list(FULL_DAY_TABLE))
    @pytest.mark.parametrize("idx", range(len(CRONS)))
    def test_each_cell(self, zone: str, delay: int, idx: int) -> None:
        day, cron = DAYS[zone], CRONS[idx]
        expected = FULL_DAY_TABLE[(zone, delay)][idx]
        fired_et = _fired_at(day, cron, delay).astimezone(ET)
        assert _ran_for_that_session(day, cron, delay) is expected, (
            f"{zone} 延迟{delay}h：cron {cron[0]:02d}:{cron[1]:02d}Z 落在 "
            f"{fired_et:%m-%d %H:%M} ET，应当 {'放行' if expected else '不放行'}"
        )


class TestTheNetEffect:
    """净效果才是 §7.1 真正承诺的东西。"""

    @pytest.mark.parametrize(("zone", "delay"), list(FULL_DAY_TABLE))
    def test_every_situation_has_at_least_two_passing_runs(self, zone: str, delay: int) -> None:
        """**四种情形每一种都至少有 2 跑放行。**

        一跑是不够的：那一跑若撞上供应商比平时更慢，当天就没有第二次机会。
        """
        day = DAYS[zone]
        passing = sum(_ran_for_that_session(day, c, delay) for c in CRONS)
        assert passing >= 2, f"{zone} 延迟{delay}h 只有 {passing} 跑放行"

    @pytest.mark.parametrize(("zone", "delay"), list(FULL_DAY_TABLE))
    def test_a_run_past_midnight_et_no_longer_belongs_to_that_session(
        self, zone: str, delay: int
    ) -> None:
        """跨过午夜 ET 的那几跑，``when_to_run`` 会把它们判给**第二天**。

        M12 时这意味着当天数据**永久丢失**；M13 起闸门没放行时会转为定稿跑
        （钳到上一个还没全员定稿的 session）。但闸门本身的这条行为没变，
        而定稿跑的整条逻辑都押在它上面 —— 所以照样钉住。

        断的是闸门自己的行为（``_decide`` 的原始判定），不是
        ``_ran_for_that_session`` —— 后者已经把「会话必须等于当天」
        写进了定义，拿它来断这条是恒真的，抓不到任何回归。
        """
        day = DAYS[zone]
        for cron in CRONS:
            fired_et = _fired_at(day, cron, delay).astimezone(ET)
            if fired_et.date() == day:
                continue
            decision = _decide(day, cron, delay)
            assert decision.session is None or decision.session.date != day, (
                f"{fired_et:%m-%d %H:%M} ET 已过午夜，不该还被判给 {day} 那一场"
            )

    @pytest.mark.parametrize(("zone", "delay"), list(FULL_DAY_TABLE))
    def test_every_passing_run_is_after_the_gate_opens(self, zone: str, delay: int) -> None:
        """放行的跑都在 17:00 ET（收盘 +1h）之后 —— 闸门 2 的开门时刻。

        **注意这条断的不是「供应商已经结算」。** 实测日线要到 22:15 ET 前后才结算，
        17:00 那一跑拿到的是临时结果（分钟线），由 ``final_settle_minutes`` 与
        ``preliminary`` 标记负责，不是靠排期。
        """
        day = DAYS[zone]
        for cron in CRONS:
            if not _ran_for_that_session(day, cron, delay):
                continue
            fired = _fired_at(day, cron, delay).astimezone(ET)
            assert (fired.hour, fired.minute) >= (17, 0), f"{fired:%H:%M} ET 早于闸门 2"

    @pytest.mark.parametrize("zone", list(DAYS))
    def test_on_time_the_first_result_is_one_hour_after_the_close(self, zone: str) -> None:
        """**用户要的就是这个：收盘后一小时看到一个大致的结果。** 准时时第一跑 = 17:00 ET。"""
        day = DAYS[zone]
        first = min(
            _fired_at(day, c, 0).astimezone(ET) for c in CRONS if _ran_for_that_session(day, c, 0)
        )
        assert (first.hour, first.minute) == (17, 0), f"{zone} 准时第一跑在 {first:%H:%M} ET"


class TestTheVendorDeadlineHasSomethingToLandOn:
    """``vendor_deadline_et`` 必须 **<= 每种情形下「最晚那一跑」的落点**。

    否则当天**没有任何一跑**会走到告警那一侧 —— 供应商整天不出数也悄无声息，
    那比「每天都红」坏得多。这条把 `config/app.yaml` 的那个时刻与
    `daily.yml` 的那七条 cron 绑在一起：动任何一边都要重算。
    """

    @staticmethod
    def _deadline() -> time:
        cfg = yaml.safe_load(
            (WORKFLOWS.parent.parent / "config" / "app.yaml").read_text(encoding="utf-8")
        )
        raw = cfg["vendor_deadline_et"]
        hh, mm = str(raw).split(":")[:2]
        return time(int(hh), int(mm))

    #: 延迟按 **15 分钟粒度**扫 0–4h。整点粒度会漏：初稿的截止时刻定在 23:00，
    #: 就是因为只扫了 {0,1,2,3,3.6h} 就当成了穷尽 —— 细化之后发现 0.5h / 1.5h
    #: 延迟下有一整年的格子一跑都不会告警。**粗网格上的最小值不是最小值。**
    FINE_DELAYS = tuple(i / 4 for i in range(17))

    @pytest.mark.parametrize("zone", list(DAYS))
    def test_some_run_lands_at_or_after_the_deadline(self, zone: str) -> None:
        day, deadline = DAYS[zone], self._deadline()
        for delay in self.FINE_DELAYS:
            passing = [
                _fired_at(day, c, delay).astimezone(ET)
                for c in CRONS
                if _ran_for_that_session(day, c, delay)
            ]
            assert passing, f"{zone} 延迟{delay}h 一跑都没有"
            latest = max(passing)
            assert (latest.hour, latest.minute) >= (deadline.hour, deadline.minute), (
                f"{zone} 延迟{delay}h 最晚只跑到 {latest:%H:%M} ET，"
                f"早于截止 {deadline:%H:%M} —— 当天将永远不会告警（静默丢数据）"
            )

    @pytest.mark.parametrize("zone", list(DAYS))
    def test_the_deadline_is_still_after_the_vendor_usually_settles(self, zone: str) -> None:
        """截止时刻还得**晚于实测的结算时刻**，否则最后一跑又变成天天误报。

        两头夹出来的区间：`>= 22:15`（实测结算）且 `<= 22:30`（最晚放行的下界）。
        这个区间只有 15 分钟宽 —— 动 cron 之前先看一眼它还在不在。
        """
        deadline = self._deadline()
        assert (deadline.hour, deadline.minute) >= (22, 15), "早于实测结算时刻会天天误报"


#: daily.yml 里那两条**每天**的定稿 cron（UTC 小时, 分钟）。
FINALIZE_CRONS: tuple[tuple[int, int], ...] = ((13, 0), (15, 0))


class TestTheFinalizeCrons:
    """早上的两条定稿 cron：**每天**（含周末、假日），且永远落在当天开闸之前。

    落在开闸之前，闸门才会说「不跑」，``run_daily`` 才会转去定稿**上一个** session
    —— 周五的临时值由周六早上定稿，假日前一天的由假日当天定稿。
    若某条迟到到了开闸之后，它就变成当天的一次临时跑：不坏，但定稿那件事没人做了。
    """

    def test_they_are_in_the_workflow_and_run_every_day(self) -> None:
        text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
        found = re.findall(r'cron:\s*"(\d+)\s+(\d+)\s+\*\s+\*\s+\*"', text)
        assert [(int(h), int(m)) for m, h in found] == list(FINALIZE_CRONS)

    #: 0–4h、15 分钟粒度，与截止时刻那条同一把尺子。
    FINE_DELAYS = tuple(i / 4 for i in range(17))

    @pytest.mark.parametrize("zone", list(DAYS))
    @pytest.mark.parametrize("cron", FINALIZE_CRONS)
    def test_they_land_before_the_full_day_gate_opens(
        self, zone: str, cron: tuple[int, int]
    ) -> None:
        day = DAYS[zone]
        for delay in self.FINE_DELAYS:
            fired = _fired_at(day, (*cron, 0), delay)
            d = when_to_run([_session(day, 1)], fired, SETTLE)
            assert not d.should_run, (
                f"{zone} 延迟{delay}h：{fired.astimezone(ET):%H:%M} ET 已过当天开闸，"
                "定稿跑变成了当天的临时跑"
            )
            assert fired.astimezone(ET).date() == day, "必须落在 cron 的那个 ET 日内"


class TestHalfDays:
    """半日市 13:00 收盘 → 18:30 ET 放行，比全日市早三小时。"""

    @pytest.mark.parametrize(("zone", "delay"), list(FULL_DAY_TABLE))
    def test_half_day_also_has_at_least_two_passing_runs(self, zone: str, delay: int) -> None:
        day = DAYS[zone]
        passing = sum(_ran_for_that_session(day, c, delay, half=True) for c in CRONS)
        assert passing >= 2, f"{zone} 延迟{delay}h 半日市只有 {passing} 跑放行"

    @pytest.mark.parametrize(("zone", "delay"), list(FULL_DAY_TABLE))
    def test_half_day_opens_earlier_than_full_day(self, zone: str, delay: int) -> None:
        """半日市放行的跑**不少于**全日市 —— 开门早三小时，只可能更多。"""
        day = DAYS[zone]
        full = sum(_ran_for_that_session(day, c, delay) for c in CRONS)
        half = sum(_ran_for_that_session(day, c, delay, half=True) for c in CRONS)
        assert half >= full


class TestTheWorkflowFileActuallyHasThoseCrons:
    """**光测逻辑不够** —— 逻辑对而 cron 写错，效果一样是当天零数据。

    这条把 ``daily.yml`` 里那七行与上面的矩阵绑在一起：
    改了 cron 而没改测试，或反过来，都会红。
    """

    def _daily(self) -> dict[str, Any]:
        text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
        out: dict[str, Any] = yaml.safe_load(text)
        return out

    def test_the_crons_match_the_matrix(self) -> None:
        text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
        found = re.findall(r'cron:\s*"(\d+)\s+(\d+)\s+\*\s+\*\s+([\d-]+)"', text)
        assert [(int(h), int(m)) for m, h, _ in found] == [(h, m) for h, m, _ in CRONS]

    def test_crons_that_cross_utc_midnight_run_tuesday_to_saturday(self) -> None:
        """**偏移 1 的那几条必须是 `2-6`。**

        写成 `1-5` 的话，周五那一场永远没人抓（02:00Z 周六才是 21:00 ET 周五），
        而周一那一条会落在周日晚上 —— 一个不存在的会话。
        """
        text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
        found = re.findall(r'cron:\s*"(\d+)\s+(\d+)\s+\*\s+\*\s+([\d-]+)"', text)
        for (m, h, dow), (_, _, off) in zip(found, CRONS, strict=True):
            expected = "2-6" if off else "1-5"
            assert dow == expected, f"cron {h}:{m}Z 的星期段应当是 {expected}，实际 {dow}"

    def test_settle_minutes_in_config_matches_this_test(self) -> None:
        """测试里的 ``SETTLE`` 必须就是配置里的那个数，否则这张表测的是幻觉。"""
        cfg_path = WORKFLOWS.parent.parent / "config" / "app.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert cfg["settle_minutes"] == SETTLE

    def test_concurrency_does_not_cancel_in_progress(self) -> None:
        """**取消会把 T2 打断在半路。**

        数据会完整回滚（那没问题），但 ``runs`` 里会留下一行永远停在
        running，而那是留给「硬崩溃」的信号。
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
