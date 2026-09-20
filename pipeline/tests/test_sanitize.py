"""``pipeline/sanitize.py`` 的测试（§9.1.3）。

这条规则的价值全在「NaN 绝不进库」上，而它失败的样子有两种：
一种是整批写入失败（吵闹，会被发现），一种是 `NaN` 真的被存进 `numeric`
然后在 `order by` 里排到所有数字之前（安静，榜单从此长期错乱）。
测第二种。
"""

from __future__ import annotations

import json
import math
from datetime import date
from decimal import Decimal
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from pipeline.sanitize import clean_records, clean_value


class TestTheThreeNonFiniteValues:
    @pytest.mark.parametrize("v", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_become_none(self, v: float) -> None:
        assert clean_value(v) is None

    @pytest.mark.parametrize(
        "v", [np.float64("nan"), np.float64("inf"), np.float32("-inf"), np.float64(1.5)]
    )
    def test_numpy_floats(self, v: np.floating) -> None:
        out = clean_value(v)
        assert out is None or (isinstance(out, float) and math.isfinite(out))

    @pytest.mark.parametrize("v", [pd.NA, pd.NaT, None])
    def test_pandas_missing_sentinels(self, v: object) -> None:
        """``pd.NA`` 参与 bool 运算会抛 —— 必须在任何比较之前就被认出来。"""
        assert clean_value(v) is None

    def test_decimal_nan(self) -> None:
        """``Decimal('NaN')`` 不是 float，``math.isnan`` 也不收它。

        而 ``numeric`` 列读回来正是 ``Decimal`` —— 这条路真实存在。
        """
        assert clean_value(Decimal("NaN")) is None
        assert clean_value(Decimal("Infinity")) is None
        assert clean_value(Decimal("1.25")) == Decimal("1.25")

    def test_numpy_scalars_become_python_scalars(self) -> None:
        """docstring 说「psycopg 不认 ``np.float64``」，但之前没有一条测试
        断言**输出的类型**。实测：删掉 ``np.generic`` 那一步，
        ``np.int64(7)`` 会悄悄保持 numpy 类型而只有一条测试变红。
        """
        assert type(clean_value(np.int64(7))) is int
        assert type(clean_value(np.bool_(True))) is bool
        assert type(clean_value(np.float32(1.5))) is float

    def test_numpy_arrays_and_sets_are_cleaned(self) -> None:
        """``np.ndarray`` **不是** ``collections.abc.Sequence`` ——
        单靠 Sequence 分支会漏掉它，而它完全可能出现在 extra jsonb 里。"""
        assert clean_value(np.array([1.0, np.nan])) == [1.0, None]
        assert clean_value(np.array(np.nan)) is None  # 0 维
        assert sorted(x for x in clean_value({1.0, 2.0}) if x is not None) == [1.0, 2.0]

    def test_finite_values_pass_through_unchanged(self) -> None:
        for v in (0, 1.5, -3, "abc", True, b"x"):
            assert clean_value(v) == v


class TestNestedContainers:
    def test_extra_jsonb_dict_is_cleaned(self) -> None:
        """**这是 ``df.replace`` 够不到的那一处。**

        §9.1.3 点名 ``extra`` jsonb 列有完全相同的问题，而 DataFrame 级的
        replace 不会走进嵌套 dict —— 实测 ``{'extra': {'x': nan}}`` 原样穿过去，
        然后 ``json.dumps`` 吐出裸 ``NaN``，那不是合法 JSON。
        """
        out = clean_value({"x": float("nan"), "y": 1.0, "z": {"deep": float("inf")}})
        assert out == {"x": None, "y": 1.0, "z": {"deep": None}}

    def test_lists_are_cleaned(self) -> None:
        assert clean_value([1.0, float("nan"), [float("-inf")]]) == [1.0, None, [None]]

    def test_strings_are_not_treated_as_sequences(self) -> None:
        """``str`` 也是 ``Sequence`` —— 递归进去会把 'abc' 拆成 ['a','b','c']。"""
        assert clean_value("abc") == "abc"
        assert clean_value(b"abc") == b"abc"


class TestTheWholeBoundary:
    def test_no_non_finite_float_survives(self) -> None:
        """出口断言：``allow_nan=False`` 序列化不得因为**非有限浮点**而失败。

        必须带 ``default=str``：真实的行里有 ``date``，而 ``date`` 本来就不是
        JSON 类型。上一版没带它，于是这条断言只对「恰好全是浮点和字符串」
        的构造行成立 —— 它测的是夹具，不是不变式。
        """
        rows = [
            {
                "symbol": "AAPL",
                "date": date(1900, 1, 1),
                "rsi_14": float("nan"),
                "extra": {"a": float("inf")},
            },
            {
                "symbol": "MSFT",
                "date": date(1900, 1, 2),
                "rsi_14": np.float64(55.5),
                "extra": {"a": 1.0},
            },
        ]
        cleaned = clean_records(rows)
        json.dumps(cleaned, allow_nan=False, default=str)  # 不抛就是通过
        assert cleaned[0]["rsi_14"] is None
        assert cleaned[0]["extra"] == {"a": None}

    def test_a_warmup_batch_survives(self) -> None:
        """§12 #5 回补 400 根，每个标的约 126 个前导行全是 NaN ——
        这是第一次 backfill 必然撞上的形状，不是理论风险。"""
        df = pd.DataFrame(
            {
                "symbol": ["AAPL"] * 400,
                "rsi_14": [float("nan")] * 126 + [50.0] * 274,
                "alpha_annual": [float("nan")] * 126 + [0.1] * 274,
            }
        )
        cleaned = clean_records(cast("list[dict[str, Any]]", df.to_dict("records")))
        assert sum(r["rsi_14"] is None for r in cleaned) == 126
        json.dumps(cleaned, allow_nan=False, default=str)

    def test_keys_are_stringified(self) -> None:
        assert clean_records(cast("list[dict[str, Any]]", [{1: 2}]))[0] == {"1": 2}
