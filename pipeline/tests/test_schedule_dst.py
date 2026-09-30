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

于是矩阵是 **7 条 cron × 2 个时区 × 2 种延迟 = 28 格**（全日市），
外加半日市的覆盖度断言。``settle_minutes`` 同步从 60 改到 330，理由写在
``config/app.yaml`` 里：Yahoo 的收盘价要到 22:15 ET 才结算完，
按 +60 分钟去问必然拿到半根 bar。
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

#: 与 `config/app.yaml` 的 settle_minutes 同步。全日市 → 21:30 ET 放行。
SETTLE = 330

WORKFLOWS = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"

#: daily.yml 里那七条 cron：(小时, 分钟, 相对会话日的 UTC 天偏移)。
#: 偏移 1 的那几条写的是 `2-6`（周二到周六）—— 02:00Z 周六 = 21:00 ET 周五。
CRONS: tuple[tuple[int, int, int], ...] = (
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


def _fired_at(day: date, cron: tuple[int, int, int], delay_h: int) -> datetime:
    """这条 cron 为 ``day`` 那一场实际在什么时刻执行（UTC）。"""
    hh, mm, day_off = cron
    return datetime.combine(day + timedelta(days=day_off), time(hh, mm), tzinfo=UTC) + timedelta(
        hours=delay_h
    )


def _decide(
    day: date, cron: tuple[int, int, int], delay_h: int, *, half: bool = False
) -> GateDecision:
    """把**当天和次日两场**都喂进去 —— 生产里 sessions 是全量的。

    只喂一场会让「跨午夜」伪装成 ``skipped_holiday``，而真实症状是
    ``skipped_too_early``（挑到了第二天那一场）。测试要复现真实症状。
    """
    sessions = [_session(day, 1, half=half), _session(day + timedelta(days=1), 2, half=half)]
    return when_to_run(sessions, _fired_at(day, cron, delay_h), SETTLE)


def _ran_for_that_session(
    day: date, cron: tuple[int, int, int], delay_h: int, *, half: bool = False
) -> bool:
    """**放行，且放行的是 ``day`` 那一场。** 两个条件缺一不可。"""
    d = _decide(day, cron, delay_h, half=half)
    return d.should_run and d.session is not None and d.session.date == day


#: 全日市的落点表，与 `daily.yml` 头部那张表逐格对应。
#: 键是 (时区标记, 延迟小时)，值是七条 cron 各自放不放行。
FULL_DAY_TABLE: dict[tuple[str, int], tuple[bool, ...]] = {
    ("EDT", 0): (False, False, False, False, True, True, False),
    ("EDT", 3): (False, True, True, False, False, False, False),
    ("EST", 0): (False, False, False, False, False, True, True),
    ("EST", 3): (False, False, True, True, False, False, False),
}

DAYS = {"EDT": EDT_DAY, "EST": EST_DAY}


class TestTheLandingTable:
    """7 条 cron × 2 个时区 × 2 种延迟。**逐格列举，不靠循环里的一个断言。**"""

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
    def test_no_passing_run_ever_crosses_midnight_et(self, zone: str, delay: int) -> None:
        """**放行的那几跑必须落在会话当天的 ET 日期上。**

        这条是 2026-09-30 新加的。跨过午夜 ET 之后 ``when_to_run`` 会挑到
        第二天那一场，判 ``skipped_too_early`` —— exit 0、不告警、
        当天数据**永久丢失**。比「太早」坏，因为太早还会被后面几跑救回来。
        """
        day = DAYS[zone]
        for cron in CRONS:
            if not _ran_for_that_session(day, cron, delay):
                continue
            assert _fired_at(day, cron, delay).astimezone(ET).date() == day

    @pytest.mark.parametrize(("zone", "delay"), list(FULL_DAY_TABLE))
    def test_every_passing_run_is_after_the_vendor_settles(self, zone: str, delay: int) -> None:
        """放行的跑都在 21:30 ET 之后 —— 也就是闸门 2 的开门时刻。

        实测的可用时刻在 21:19–22:15 ET 之间（2026-09-29，见 app.yaml）。
        这条把「放行」和「供应商大概率已经结算」绑在一起：
        在它之前放行，拿到的是半根 bar，然后记一条毫无意义的 `stale_vendor`。
        """
        day = DAYS[zone]
        for cron in CRONS:
            if not _ran_for_that_session(day, cron, delay):
                continue
            fired = _fired_at(day, cron, delay).astimezone(ET)
            assert (fired.hour, fired.minute) >= (21, 30), f"{fired:%H:%M} ET 早于闸门 2"


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
        assert self._daily()["concurrency"]["cancel-in-progress"] is False
