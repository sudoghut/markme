"""美联储 FOMC 会议日程（``federalreserve.gov/monetarypolicy/fomccalendars.htm``）。

没有 API，只有 HTML —— 这是整个板块里最脆的一环，所以规则最严：**宁可拒收，不可猜**。

页面里**不是每一行都是利率决议会议**（2026-11-23 实测两种例外）：

- ``22 (notation vote)`` —— 书面表决（2025-08-22），不是会议；
- ``Monetary Policy Strategy Meeting`` —— 2027-10-26/27，没有利率决定。

两者都被**明确识别后跳过**。任何**其他**形状的行都不认识 —— 整次拒收，
由调用方保留库里的旧日程。把一个不认识的行当成决议会议，倒计时就会指向一个
不会宣布利率的日子。

日程是**暂定**的（页面自己写着 *Each meeting date is tentative*），
美联储在 2026-11-12 刚改过 2027 年的日程 —— 所以它必须抓、不能写死，
而且改期/取消要能传导到库里（整窗删+插，见 ``store.replace_fomc_meetings``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from pipeline.macro.errors import MacroParseError

__all__ = ["FOMC_URL", "Meeting", "coverage_start", "parse_fomc"]

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

#: 当年至少要解析出这么多场，否则整次拒收。美联储每年 8 场，2027 年只有 7 场
#: 决议会议（取消了 10 月那场的政策决定）；5 是留了余量的下限 ——
#: 它防的是「页面改版后正则只匹配到零星几行」这种**看起来成功**的失败。
_MIN_MEETINGS_IN_CURRENT_YEAR = 5

_YEAR_RE = re.compile(r"(\d{4}) FOMC Meetings")
_ROW_RE = re.compile(r'<div class="(?:fomc-meeting--shaded )?row fomc-meeting"[^>]*>')
_MONTH_RE = re.compile(r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>")
_DATE_RE = re.compile(r"fomc-meeting__date[^>]*>([^<]*)<")
_STRATEGY_RE = re.compile(r"<p>\s*Monetary Policy Strategy Meeting\s*</p>", re.IGNORECASE)
_DAYS_RE = re.compile(r"^(\d{1,2})-(\d{1,2})\*?$")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip


@dataclass(frozen=True)
class Meeting:
    start_date: date
    end_date: date


def coverage_start(today: date) -> date:
    """整窗删+插的起点：去年 1 月 1 日。端点由日期契约给出，与本次解析出几行无关。"""
    return date(today.year - 1, 1, 1)


def _month(token: str, row: str) -> int:
    m = _MONTHS.get(token.strip()[:3].lower())
    if m is None:
        raise MacroParseError(f"不认识的月份 {token!r}（行：{row!r}）")
    return m


def _parse_row(year: int, row_html: str) -> Meeting | None:
    """一行 → 会议；识别出的非决议行 → ``None``；不认识 → 抛。"""
    month_m = _MONTH_RE.search(row_html)
    date_m = _DATE_RE.search(row_html)
    if month_m is None or date_m is None:
        raise MacroParseError(f"{year} 年有一行找不到月份或日期格：{row_html[:200]!r}")
    month_cell = month_m.group(1).strip()
    date_cell = re.sub(r"\s+", " ", date_m.group(1)).strip()
    label = f"{year} {month_cell} {date_cell}"

    if "notation vote" in date_cell.lower() or _STRATEGY_RE.search(row_html):
        return None

    days = _DAYS_RE.match(date_cell)
    if days is None:
        raise MacroParseError(f"不认识的日期格式：{label!r}")
    d1, d2 = int(days.group(1)), int(days.group(2))
    months = [p for p in month_cell.split("/") if p.strip()]
    try:
        if len(months) == 1:
            m1 = m2 = _month(months[0], label)
        elif len(months) == 2:
            m1, m2 = _month(months[0], label), _month(months[1], label)
        else:
            raise MacroParseError(f"不认识的月份格式：{label!r}")
        start = date(year, m1, d1)
        end = date(year + (1 if m2 < m1 else 0), m2, d2)
    except ValueError as exc:
        raise MacroParseError(f"不是合法日期：{label!r}（{exc}）") from exc
    if not (0 <= (end - start).days <= 2):
        raise MacroParseError(f"会议跨度不在 0–2 天内：{label!r}（{start}…{end}）")
    return Meeting(start, end)


def parse_fomc(html: str, today: date) -> list[Meeting]:
    """解析整页，按开始日期升序返回。**任何一处对不上整次拒收。**"""
    headings = [(m.start(), int(m.group(1))) for m in _YEAR_RE.finditer(html)]
    if not headings:
        raise MacroParseError("页面里找不到任何「<年份> FOMC Meetings」分段（页面改版了？）")

    meetings: list[Meeting] = []
    for i, (pos, year) in enumerate(headings):
        end_pos = headings[i + 1][0] if i + 1 < len(headings) else len(html)
        segment = html[pos:end_pos]
        starts = [m.start() for m in _ROW_RE.finditer(segment)]
        for j, s in enumerate(starts):
            row = segment[s : starts[j + 1] if j + 1 < len(starts) else len(segment)]
            meeting = _parse_row(year, row)
            if meeting is not None:
                meetings.append(meeting)

    if len({m.start_date for m in meetings}) != len(meetings):
        raise MacroParseError("解析出了开始日期相同的两场会议")
    current = [m for m in meetings if m.start_date.year == today.year]
    if len(current) < _MIN_MEETINGS_IN_CURRENT_YEAR:
        raise MacroParseError(
            f"{today.year} 年只解析出 {len(current)} 场"
            f"（至少要 {_MIN_MEETINGS_IN_CURRENT_YEAR} 场）—— 页面结构多半变了，整次拒收"
        )
    meetings.sort(key=lambda m: m.start_date)
    return meetings
