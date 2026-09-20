"""写入边界上的一条硬规则：``NaN`` / ``±Inf`` 永不进库，一律转成 SQL ``NULL``（§9.1.3）。

**这不是理论风险，是第一次 backfill 必然撞上的东西。** §12 #5 回补 400 根 bar，
每个标的约 126 个前导行全带 NaN（RSI 前 14 根、EMA(60) 前 59 根、`mom_20` 前 20 根、
alpha/beta 在 `min_obs` 以下），17 个标的一起上。

两条失败路径都很难认：

- 走 REST：Python ``json.dumps`` 默认 ``allow_nan=True``，吐出裸 ``NaN`` ——
  **那不是合法 JSON**。PostgREST 拒收整个 body，整批失败。
- 走 psycopg：Postgres 的 ``numeric`` **接受** ``'NaN'``，于是 ``rsi_14 = NaN``
  被存下来，在 ``order by rsi_14 desc`` 里排在**所有数字之前**，
  读出来又被序列化成非法 JSON，把前端打挂。

第二条更坏：它没有任何报错，只是榜单从此长期错乱。

**关于 §9.1.3 提到的 ``df.replace([np.nan, np.inf, -np.inf], None)``：**
实测（pandas 3.0.6）它在**普通数值列上是有效的** —— 列被提升成 object，
三种非有限值都变成真正的 ``None``。我原本以为它无效，实测推翻了这个猜想。

但它覆盖不到两处，而这两处 §9.1.3 自己都点到了：

1. **``extra`` jsonb 列**。实测 ``df.replace`` 不会走进嵌套 dict：
   ``{'extra': {'x': nan}}`` 原样穿过去，然后 ``json.dumps`` 吐出裸 ``NaN``。
2. **不经过 DataFrame 的值**。事件列、``runs`` 的计数、手工拼的行 ——
   它们没有 ``.replace`` 可调。

所以这里不是「替代」那一招，而是把**写入边界**收成一个窄入口：
所有进库的行都过 :func:`clean_records`，于是「哪里可能漏掉 sanitize」
这个问题有唯一的答案。DataFrame 路径上再顺手 ``replace`` 一次也无妨，
但正确性不依赖它。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

__all__ = ["clean_records", "clean_value"]


def clean_value(v: Any) -> Any:
    """把一个值转成「可以安全交给 psycopg」的形式。

    - ``NaN`` / ``±Inf`` / ``pd.NA`` / ``pd.NaT`` / ``None`` → ``None``
    - numpy 标量 → 对应的 Python 标量（psycopg 不认 ``np.float64``）
    - ``dict`` / ``list`` / ``tuple`` → 递归处理（``extra`` jsonb 列同样会中招）
    - 其余原样返回
    """
    if v is None:
        return None

    # pd.NA / pd.NaT。必须在 float 分支之前：pd.NA 参与 bool 运算会抛。
    if v is pd.NaT or v is pd.NA:
        return None

    if isinstance(v, Mapping):
        return {str(k): clean_value(x) for k, x in v.items()}
    # numpy 数组**不是** collections.abc.Sequence —— 单靠下面那一行会漏掉它，
    # 而它完全可能出现在 extra jsonb 里。0 维数组先落地成标量。
    if isinstance(v, np.ndarray):
        return clean_value(v.item()) if v.ndim == 0 else [clean_value(x) for x in v.tolist()]
    if isinstance(v, set | frozenset):
        return [clean_value(x) for x in v]
    # str/bytes 也是 Sequence，必须排除掉
    if isinstance(v, Sequence) and not isinstance(v, str | bytes | bytearray):
        return [clean_value(x) for x in v]

    # numpy 标量先落地成 Python 对象，否则下面的 isinstance 判断会走偏。
    if isinstance(v, np.generic):
        v = v.item()

    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, Decimal):
        # Decimal('NaN') 不是 float，math.isnan 也不收它 —— 单独判。
        return None if not v.is_finite() else v

    return v


def clean_records(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """批量版：一串 ``dict`` 行进，一串干净的 ``dict`` 行出。

    写库路径上**只允许**经过这里的行。把它做成一个窄入口，
    是为了让「哪里可能漏掉 sanitize」这个问题有唯一的答案。
    """
    return [{str(k): clean_value(v) for k, v in row.items()} for row in rows]
