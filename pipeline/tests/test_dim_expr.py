"""``dim_when`` 受限表达式的测试（§6.2）。

重点在两件事：**拒绝一切不在白名单里的语法**，以及**空值 fail closed**。
后者不是风格问题 —— §3.4 的 QQQ 自回归必然产出空的 ``alpha_t_stat``，
判错方向就会让基准行宣称一个「统计显著」的 0.00% alpha。
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from pipeline.dim_expr import DimExprError, parse_dim_when


class TestRejectsNonWhitelisted:
    """白名单之外的一律拒绝 —— 这份文件会被前端在构建期读入。"""

    @pytest.mark.parametrize(
        "source",
        [
            "__import__('os').system('rm -rf /')",
            "open('/etc/passwd').read()",
            "().__class__.__bases__",
            "alpha_t_stat.__class__",
            "[x for x in range(10)]",
            "lambda: 1",
            "min(alpha_t_stat, 2)",  # 函数白名单只有 abs
            "alpha_t_stat + 1 < 2",  # 算术不在白名单里
            "alpha_t_stat if r2 else 1",
            "not alpha_t_stat",
            "'string' < 2",
        ],
    )
    def test_rejected(self, source: str) -> None:
        with pytest.raises(DimExprError):
            parse_dim_when(source)

    def test_constant_expression_rejected(self) -> None:
        """不引用任何字段的表达式是个常量，必然是写错了。"""
        with pytest.raises(DimExprError, match="常量"):
            parse_dim_when("1 < 2")

    def test_syntax_error(self) -> None:
        with pytest.raises(DimExprError, match="语法错误"):
            parse_dim_when("abs(alpha_t_stat <")


class TestAcceptsRealConfig:
    """config 里实际用到的两条必须能解析。"""

    def test_alpha_t_stat(self) -> None:
        expr = parse_dim_when("abs(alpha_t_stat) < 2")
        assert expr.fields == {"alpha_t_stat"}

    def test_r2(self) -> None:
        expr = parse_dim_when("r2 < 0.3")
        assert expr.fields == {"r2"}

    def test_boolean_and_chained(self) -> None:
        expr = parse_dim_when("r2 < 0.3 and abs(alpha_t_stat) < 2")
        assert expr.fields == {"r2", "alpha_t_stat"}
        expr2 = parse_dim_when("0 < r2 < 0.3")
        assert expr2.fields == {"r2"}


class TestEvaluation:
    def test_true_and_false(self) -> None:
        expr = parse_dim_when("abs(alpha_t_stat) < 2")
        assert expr.evaluate({"alpha_t_stat": 1.5}) is True
        assert expr.evaluate({"alpha_t_stat": -1.5}) is True
        assert expr.evaluate({"alpha_t_stat": 3.0}) is False
        assert expr.evaluate({"alpha_t_stat": -3.0}) is False

    def test_boundary_is_strict(self) -> None:
        """|t| = 2 正好在边界上 —— ``< 2`` 为假，即**不**打灰。"""
        expr = parse_dim_when("abs(alpha_t_stat) < 2")
        assert expr.evaluate({"alpha_t_stat": 2.0}) is False

    @pytest.mark.parametrize("value", [None, float("nan")])
    def test_null_and_nan_fail_closed(self, value: float | None) -> None:
        """**这是本文件最重要的一条测试。**

        QQQ 对自己回归时残差恒为 0 → ``alpha_t_stat = 0/0``，写库时落成 NULL。
        若按 ``NaN < 2 == False`` 处理，基准那一行就不打灰 ——
        UI 会宣称一个「统计显著」的 0.00% alpha，恰好是真相的反面。
        """
        expr = parse_dim_when("abs(alpha_t_stat) < 2")
        assert expr.evaluate({"alpha_t_stat": value}) is True

    def test_missing_field_fails_closed(self) -> None:
        expr = parse_dim_when("abs(alpha_t_stat) < 2")
        assert expr.evaluate({}) is True

    def test_and_short_circuits_on_definite_false(self) -> None:
        """``False and NULL`` 是确定的 False，不该被空值拖成「打灰」。"""
        expr = parse_dim_when("r2 < 0.3 and abs(alpha_t_stat) < 2")
        assert expr.evaluate({"r2": 0.9, "alpha_t_stat": None}) is False

    def test_and_with_unknown_fails_closed(self) -> None:
        expr = parse_dim_when("r2 < 0.3 and abs(alpha_t_stat) < 2")
        assert expr.evaluate({"r2": 0.1, "alpha_t_stat": None}) is True

    def test_or_with_definite_true(self) -> None:
        expr = parse_dim_when("r2 < 0.3 or abs(alpha_t_stat) < 2")
        assert expr.evaluate({"r2": 0.1, "alpha_t_stat": None}) is True

    def test_bool_input_fails_closed(self) -> None:
        """bool 是 int 的子类，会悄悄参与数值比较 —— 拒绝它，但不抛异常。

        求值期不得抛：一个「要不要打灰」的判断没有资格让整页渲染失败。
        """
        expr = parse_dim_when("r2 < 0.3")
        assert expr.evaluate({"r2": True}) is True

    @pytest.mark.parametrize(
        "value",
        [Decimal("0.5"), Decimal("0.1"), 1, 0.5],
        ids=["decimal-high", "decimal-low", "int", "float"],
    )
    def test_accepts_db_and_pandas_numeric_types(self, value: object) -> None:
        """psycopg 会把 Postgres ``numeric`` 还成 ``Decimal`` ——
        而 ``alpha_t_stat`` / ``r2`` 在 metrics_daily 里正是 ``numeric``。
        把它当成类型错误抛出来，就等于这个求值器对**真实数据**不可用。
        """
        expr = parse_dim_when("r2 < 0.3")
        assert expr.evaluate({"r2": value}) is (float(value) < 0.3)  # type: ignore[arg-type]

    def test_unknown_object_fails_closed(self) -> None:
        """没见过的类型（如 pandas.NA）也归为「打灰」，而不是把页面搞崩。"""
        expr = parse_dim_when("r2 < 0.3")
        assert expr.evaluate({"r2": object()}) is True


class TestMustBeAComparison:
    """根节点必须是比较或布尔运算。

    否则 ``dim_when: "alpha_t_stat"`` 会被接受，而它的真实语义是
    「t 值恰好为 0 时打灰」—— 与作者意图相反，且悄无声息。
    一个现实的走到这一步的方式：编辑时把 `abs(x) < 2` 剪成了 `abs(x)`。
    """

    @pytest.mark.parametrize("source", ["alpha_t_stat", "abs(r2)", "-r2"])
    def test_non_comparison_rejected(self, source: str) -> None:
        with pytest.raises(DimExprError, match="必须是一个比较表达式"):
            parse_dim_when(source)


class TestPathologicalInput:
    def test_deep_unary_chain_is_not_a_crash(self) -> None:
        """一元运算符可以无括号地无限叠加，CPython 对此抛的不是 SyntaxError。"""
        with pytest.raises(DimExprError):
            parse_dim_when("-" * 1000 + "r2 < 1")

    def test_overlong_source_rejected(self) -> None:
        with pytest.raises(DimExprError, match="过长"):
            parse_dim_when("r2 < 1 and " * 200 + "r2 < 1")

    def test_infinite_constant_rejected(self) -> None:
        """``abs(x) < 1e400`` 恒为真 —— 那不是作者的本意。"""
        with pytest.raises(DimExprError, match="有限值"):
            parse_dim_when("r2 < 1e400")

    def test_bare_abs_gives_a_useful_message(self) -> None:
        with pytest.raises(DimExprError, match="漏了括号"):
            parse_dim_when("abs < 2")

    def test_nan_is_not_silently_compared(self) -> None:
        expr = parse_dim_when("r2 < 0.3")
        assert not math.isnan(0.0)  # 守住测试本身的前提
        assert expr.evaluate({"r2": float("nan")}) is True


class TestInfinity:
    """无穷大必须 fail closed，和 NaN 一样。

    这一条是 review 抓到的：我给**表达式常量**加了 isfinite 检查，
    却漏了**行值** —— 于是 ``r2 = inf`` 会让 ``r2 < 0.3`` 返回 False
    （不打灰），等于把一个无穷大的 R² 当成「拟合得很好」。
    """

    @pytest.mark.parametrize(
        "value",
        [float("inf"), float("-inf"), Decimal("Infinity"), Decimal("-Infinity")],
        ids=["float-inf", "float-neg-inf", "decimal-inf", "decimal-neg-inf"],
    )
    def test_infinite_row_value_fails_closed(self, value: object) -> None:
        expr = parse_dim_when("r2 < 0.3")
        assert expr.evaluate({"r2": value}) is True

    def test_infinite_inside_abs_fails_closed(self) -> None:
        expr = parse_dim_when("abs(alpha_t_stat) < 2")
        assert expr.evaluate({"alpha_t_stat": float("-inf")}) is True
