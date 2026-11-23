"""纽约联储 Markets Data API：EFFR 与联邦基金目标区间。

``markets.newyorkfed.org/api/rates/unsecured/effr/search.json`` 的每一行自带
``targetRateFrom`` / ``targetRateTo`` / ``percentRate``（2026-11-23 实测），所以
目标区间与 EFFR 同源、同一次请求，不需要 API key。

**判加息/降息只在这里做一次**（``with_changes``），结果落库成 ``change_kind`` /
``change_bp``。前端只读结果，不重新实现一遍 —— 两份实现就是两个要对齐的地方。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pipeline.macro.errors import MacroParseError

__all__ = ["RatePoint", "effr_url", "parse_effr", "with_changes"]

_BASE = "https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json"

ChangeKind = Literal["up", "down", "mixed"]


@dataclass(frozen=True)
class RatePoint:
    date: date
    target_lower: Decimal
    target_upper: Decimal
    effr: Decimal


def effr_url(start: date, end: date) -> str:
    return f"{_BASE}?startDate={start.isoformat()}&endDate={end.isoformat()}"


def _pct(raw: Any, what: str, day: str, *, allow_zero: bool = False) -> Decimal:
    """一个百分数，必须是有限数且落在 ``[0, 25)`` 里（``allow_zero`` 才含 0）。

    **0 只有目标区间的下沿可以是**：2022-03-16 之前的零利率时期，目标区间是
    ``0.00–0.25``，下沿就是 ``0.0``。把它也当成错数拒收，会让包含 2022 年的整份
    响应被拒、纽约联储这一路永远起不来（对着 2024 年以后的数据测试时碰不到它）。
    EFFR 与上沿在任何时期都 > 0。

    这只拦得住**差 100 倍往上**的单位错误（``388``）。小数形式（``0.0388``）在零利率
    时期是合法的 EFFR（0.08），范围卡不住它 —— 那一类靠 :func:`parse_effr` 里的
    **行内一致性**（EFFR 必须落在目标区间 ±0.5 之内）拦。``bool`` 是 ``int`` 的子类，
    要先排除。
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise MacroParseError(f"{day} 的 {what} 不是数字：{raw!r}")
    if not math.isfinite(raw):
        raise MacroParseError(f"{day} 的 {what} 不是有限数：{raw!r}")
    value = Decimal(str(raw))
    floor_ok = value >= 0 if allow_zero else value > 0
    if not (floor_ok and value < Decimal(25)):
        raise MacroParseError(f"{day} 的 {what}={value} 不在百分数的合理范围内，疑似单位错误")
    return value


#: EFFR 与目标区间的最大容差（百分点）。资金面紧张时 EFFR 真会越出区间
#: （2019-09 越过上沿约 5bp），所以不能卡成「必须在区间内」；0.5 只为拦单位错。
_EFFR_TOLERANCE = Decimal("0.5")


def parse_effr(payload: str) -> list[RatePoint]:
    """解析响应，按日期升序返回。**任何一行不对整次拒收。**"""
    try:
        doc = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MacroParseError(f"纽约联储响应不是合法 JSON：{exc}") from exc
    rows = doc.get("refRates") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not rows:
        raise MacroParseError("纽约联储响应缺少非空的 refRates 列表")

    points: dict[date, RatePoint] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise MacroParseError(f"refRates 里有非对象的行：{row!r}")
        raw_day = row.get("effectiveDate")
        try:
            day = date.fromisoformat(str(raw_day))
        except ValueError as exc:
            raise MacroParseError(f"effectiveDate 不是 ISO 日期：{raw_day!r}") from exc
        if row.get("type") != "EFFR":
            raise MacroParseError(f"{day} 的 type 不是 EFFR：{row.get('type')!r}")
        lower = _pct(row.get("targetRateFrom"), "targetRateFrom", str(day), allow_zero=True)
        upper = _pct(row.get("targetRateTo"), "targetRateTo", str(day))
        if lower >= upper:
            raise MacroParseError(f"{day} 的目标区间下沿 {lower} 不低于上沿 {upper}")
        effr = _pct(row.get("percentRate"), "percentRate", str(day))
        if not (lower - _EFFR_TOLERANCE <= effr <= upper + _EFFR_TOLERANCE):
            raise MacroParseError(
                f"{day} 的 EFFR={effr} 离目标区间 {lower}–{upper} 太远（容差 ±{_EFFR_TOLERANCE}）"
                "，疑似单位错误"
            )
        if day in points:
            raise MacroParseError(f"{day} 出现了不止一行")
        points[day] = RatePoint(day, lower, upper, effr)
    return [points[d] for d in sorted(points)]


def with_changes(points: list[RatePoint]) -> list[dict[str, Any]]:
    """给每一行补 ``change_kind`` / ``change_bp``，输入必须已按日期升序。

    只在**目标区间相对前一行变了**的那一行填值，其余为 ``None``：

    - 上沿与下沿**同向同幅** → ``up`` / ``down``，``change_bp`` 是带符号的整数 bp；
    - 两沿变了但不同向或不同幅 → ``mixed``，``change_bp`` 为 ``None``
      （**不猜方向**，页面写「未知」）；
    - 窗口第一行没有前值，不记变动。

    用 ``Decimal`` 做差，而不是 float：``0.32 - 0.07`` 是 ``0.25``，``0.57 - 0.32`` 却是
    ``0.24999999999999994`` —— 同样是「上调 25bp」，float 会让上下沿的差不相等、被判成 ``mixed``，
    或 ``int(x * 100)`` 得到 24。（``4.35 - 4.1`` 这类例子恰好等于 0.25，**不能**用来测它。）
    """
    out: list[dict[str, Any]] = []
    prev: RatePoint | None = None
    for p in points:
        kind: ChangeKind | None = None
        bp: int | None = None
        if prev is not None and (
            p.target_upper != prev.target_upper or p.target_lower != prev.target_lower
        ):
            d_up = p.target_upper - prev.target_upper
            d_lo = p.target_lower - prev.target_lower
            if d_up == d_lo and d_up != 0:
                kind = "up" if d_up > 0 else "down"
                bp = int(d_up * 100)
            else:
                kind = "mixed"
        out.append(
            {
                "date": p.date,
                "target_lower": p.target_lower,
                "target_upper": p.target_upper,
                "effr": p.effr,
                "change_kind": kind,
                "change_bp": bp,
                "source": "nyfed",
            }
        )
        prev = p
    return out
