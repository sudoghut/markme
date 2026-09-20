"""三强股排名（§4）。

**摆正它的位置**：本项目不是一个榜单项目。三强只是加在这批标的上的又一个变量，
与 RSI / EMA / α-β 平级。本模块之所以不短，是因为「怎么排」比「RSI 怎么算」
有更多容易出错的边角，不是因为它更重要。

核心那条：**NaN 才是真正的确定性风险，不是并列。**
浮点数精确并列是零测度事件；而一个刚加入、只有 8 根 bar 的标的，
``mom_20`` 就是 NaN，``sorted(..., reverse=True)`` 把它放到哪里
取决于比较链的顺序 —— **跨输入顺序不确定**。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

__all__ = ["RankedSymbol", "is_squeaky", "rank_pool"]


@dataclass(frozen=True)
class RankedSymbol:
    """榜单里的一行。``delta_*`` 为 ``None`` 表示无定义（如最后一名没有下一名）。"""

    symbol: str
    rank: int
    score: float
    in_top_n: bool
    delta_to_next: float | None
    delta_to_median: float | None


def rank_pool(
    scores: Mapping[str, float | None],
    *,
    top_n: int,
) -> list[RankedSymbol]:
    """对一天的横截面排名。

    规则（照抄 low-buy ``relative_strength.py`` 已经写对的那套）：

    1. 非有限值（``NaN`` / ``±Inf`` / ``None``）一律**排除**，使其永不获胜；
    2. 降序排名，**并列时按 symbol 字典序**，保证确定性可复现；
    3. 若有限值的标的不足 ``top_n`` 个，只有这些有限的能上榜。

    返回的列表只含有限值的标的 —— 非有限值的标的没有名次，
    调用方对它们写 NULL 而不是给一个兜底名次。
    """
    if top_n < 1:
        raise ValueError(f"top_n 必须 >= 1，得到 {top_n}")

    finite: list[tuple[str, float]] = [
        (sym, float(v)) for sym, v in scores.items() if v is not None and math.isfinite(float(v))
    ]
    if not finite:
        return []

    # 先按 symbol 升序，再按 score 降序做**稳定**排序 —— 于是并列时
    # 字典序小的在前。两次排序而不是一个复合 key，是为了让意图直白可读。
    finite.sort(key=lambda t: t[0])
    finite.sort(key=lambda t: t[1], reverse=True)

    values = np.array([s for _, s in finite], dtype=float)
    median = float(np.median(values))

    rows: list[RankedSymbol] = []
    for i, (sym, score) in enumerate(finite):
        nxt = finite[i + 1][1] if i + 1 < len(finite) else None
        rows.append(
            RankedSymbol(
                symbol=sym,
                rank=i + 1,
                score=score,
                in_top_n=i < top_n,
                # 最后一名没有「下一名」——写 None，不写 0。
                delta_to_next=None if nxt is None else score - nxt,
                delta_to_median=score - median,
            )
        )
    return rows


def is_squeaky(rows: list[RankedSymbol], *, top_n: int, squeak_k: float) -> bool:
    """第 ``top_n`` 名与第 ``top_n+1`` 名是否「胶着」（§10.3）。

    阈值**必须归一化**，不能用固定的 pp 值。把算术摆出来：
    平静期 16 只标的铺开 2pp，相邻间隔约 0.13pp，远小于一个固定的 0.5pp 阈值
    → **一直触发**；高离散期铺开 25pp，间隔约 1.5pp → **永不触发**。
    于是固定阈值在最不需要提醒的时候最吵（平静期本来就全体密集），
    而在真正该提醒时沉默（高离散期里一个相对微小的间隔仍然是真胶着）。

    归一化后的语义也更合理：**同样的绝对间隔，在密集的池里是真分开了，
    在铺得很开的池里只是噪声。**
    """
    if top_n < 1:
        raise ValueError(f"top_n 必须 >= 1，得到 {top_n}")
    if squeak_k <= 0:
        raise ValueError(f"squeak_k 必须 > 0，得到 {squeak_k}")
    if len(rows) <= top_n:
        return False  # 没有第 top_n+1 名，谈不上胶着

    cutoff = rows[top_n - 1]
    if cutoff.delta_to_next is None:
        return False

    # **真并列是最胶着的情形** —— 第 N 名与第 N+1 名字面相等。
    # 必须在归一化之前判完：全池同值时 stdev 会是 0 或 3e-17
    # （取决于 np.mean 能否恰好整除），让语义完全相同的池得出相反结论。
    if cutoff.delta_to_next <= 0.0:
        return True

    spread = float(np.std([r.score for r in rows], ddof=1))
    if spread <= 0.0 or not math.isfinite(spread):
        # 无法归一化，而上面已经排除了并列 —— 保守地不报警。
        return False
    return cutoff.delta_to_next < squeak_k * spread
