"""美国财政部 Daily Treasury Par Yield Curve（CSV）。

单位是**百分数**（``4.63`` = 4.63%）—— 与 ``create-project.md`` §3.4 讲过的
``^IRX`` 同一类单位坑，库里存的和页面展示的是同一个数，不在中间换算。
表头必须**逐字**包含配置里列出的每个期限，对不上整次拒收。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from pipeline.macro.errors import MacroParseError

if TYPE_CHECKING:
    from pipeline.macro.config import TenorSpec

__all__ = [
    "ANCHOR_TENOR",
    "ANCHOR_TOLERANCE",
    "YieldPoint",
    "check_against_policy_rate",
    "parse_par_yields",
    "treasury_url",
]

#: 用来对账的期限与容差（百分点）。3 个月国债收益率与 EFFR 同属货币市场利率，历史上两者的
#: 差距在 1.5 个百分点以内；3 是留了余量的上限。它**只为拦单位错误**（0.0432 对 3.88 差 3.8），
#: 不是一个关于「3 个月期该是多少」的判断。supabase/invariants.sql 里有同一个数，由测试绑定。
ANCHOR_TENOR = "3m"
ANCHOR_TOLERANCE = Decimal("3")


@dataclass(frozen=True)
class YieldPoint:
    date: date
    tenor: str
    yield_pct: Decimal


def treasury_url(year: int) -> str:
    return (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all"
        f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
    )


def parse_par_yields(
    text: str, tenors: list[TenorSpec], *, allow_empty: bool = False
) -> list[YieldPoint]:
    """解析一年的 CSV，按 (日期, 期限) 升序返回。**任何一处不对整次拒收。**

    **只有空格子 / ``N/A`` 算「这天没有这个期限」**，跳过那一格；
    其余任何不是数字的内容都拒收 —— 把乱码静默当缺失，就是把错数当没事。

    ``allow_empty``：只有表头、没有数据行时返回 ``[]`` 而不是拒收。**只给新年头两周的
    当年 CSV 用**（调用方判断）；其余时候空表就是出事了。
    """
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise MacroParseError("财政部 CSV 是空的") from exc
    header = [h.strip() for h in header]
    if "Date" not in header:
        raise MacroParseError(f"财政部 CSV 表头里没有 Date：{header!r}")
    index = {name: i for i, name in enumerate(header)}
    missing = [t.column for t in tenors if t.column not in index]
    if missing:
        raise MacroParseError(f"财政部 CSV 表头缺少期限列 {missing}（表头：{header!r}）")

    seen: set[tuple[date, str]] = set()
    out: list[YieldPoint] = []
    n_rows = 0
    for raw in reader:
        if not raw or not any(c.strip() for c in raw):
            continue
        n_rows += 1
        if len(raw) != len(header):
            raise MacroParseError(
                f"财政部 CSV 有一行列数不对（{len(raw)} != {len(header)}）：{raw!r}"
            )
        cell_day = raw[index["Date"]].strip()
        try:
            day = datetime.strptime(cell_day, "%m/%d/%Y").date()
        except ValueError as exc:
            raise MacroParseError(f"财政部 CSV 日期格式不是 MM/DD/YYYY：{cell_day!r}") from exc
        for t in tenors:
            cell = raw[index[t.column]].strip()
            if cell == "" or cell.upper() == "N/A":
                continue
            try:
                value = Decimal(cell)
            except InvalidOperation as exc:
                raise MacroParseError(f"{day} 的 {t.column} 不是数字：{cell!r}") from exc
            if not value.is_finite() or not (Decimal(0) < value < Decimal(25)):
                raise MacroParseError(
                    f"{day} 的 {t.column}={cell} 不在 (0, 25) 百分数范围内，疑似单位错误"
                    "（范围只拦得住 ≥25 与 ≤0）"
                )
            key = (day, t.key)
            if key in seen:
                raise MacroParseError(f"{day} 的 {t.column} 出现了不止一次")
            seen.add(key)
            out.append(YieldPoint(day, t.key, value))
    if n_rows == 0:
        if allow_empty:
            return []
        raise MacroParseError("财政部 CSV 只有表头、没有数据行")
    if not out:
        raise MacroParseError("财政部 CSV 里没有解析出任何配置期限的数值")
    out.sort(key=lambda p: (p.date, p.tenor))
    return out


def check_against_policy_rate(points: list[YieldPoint], effr: Decimal) -> None:
    """最新一天的 3 个月期收益率必须与 ``effr`` 在同一个量级，否则整次拒收。

    ``(0, 25)`` 的范围检查拦不住小数形式（``0.0432`` 也在范围内）。财政部这一路没有行内
    可对账的第二个数，所以拿**另一个来源**（纽约联储的 EFFR）当锚 —— 与 ``nyfed`` 解析器里
    「EFFR 必须落在目标区间附近」是同一类防线，只是锚在另一份响应里。

    ``effr`` 由调用方提供；纽约联储这一跑失败时调用方没有它，就跳过这道核对
    （``invariants.sql`` 里有同一条事后断言兜底）。
    """
    anchors = [p for p in points if p.tenor == ANCHOR_TENOR]
    if not anchors:
        return
    newest = max(anchors, key=lambda p: p.date)
    if abs(newest.yield_pct - effr) > ANCHOR_TOLERANCE:
        raise MacroParseError(
            f"{newest.date} 的 {ANCHOR_TENOR} 收益率 {newest.yield_pct} 与 EFFR {effr} 相差超过 "
            f"{ANCHOR_TOLERANCE} 个百分点，疑似单位错误"
        )
