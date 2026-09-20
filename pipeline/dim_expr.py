"""``display.dim_when`` 的受限表达式（§6.2）。

一个 ``dim_when`` 回答「这一格要不要打灰」，例如 ``abs(alpha_t_stat) < 2``。

三条硬规则：

1. **绝不用 ``eval``。** config 虽然由我们自己写，但它会被前端在构建期读入；
   给一份数据文件任意代码执行能力是无谓的风险。这里用 ``ast`` 解析后
   走白名单，遇到任何不在白名单里的节点直接拒绝。
2. **空值 fail closed —— 判定为「打灰」。** §3.4 的 QQQ 自回归会产出
   ``alpha_t_stat`` 为空（残差恒为 0 → 0/0）。若按 ``NaN < 2 == False``
   处理，基准那一行就**不打灰**，等于 UI 宣称一个「统计显著」的 0.00% alpha
   —— 恰好是真相的反面，还发生在全站的基准行上。
3. **求值期绝不抛异常。** 一个「要不要打灰」的判断没有资格让整页渲染失败。
   任何意外都收敛成「打灰」。
"""

from __future__ import annotations

import ast
import math
import numbers
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from pipeline.errors import ConfigError

__all__ = ["DimExpr", "DimExprError", "parse_dim_when"]

# 白名单：只允许比较、一元负号、数字、字段名，以及 abs()。
_ALLOWED_FUNCS = frozenset({"abs"})
_ALLOWED_CMP = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
_ALLOWED_BOOL = (ast.And, ast.Or)

# 源码长度上限。一元运算符可以无括号地无限叠加（``"-"*10000``），
# 而 CPython 的解析器对此会抛 RecursionError / MemoryError 而不是 SyntaxError。
_MAX_SOURCE_LEN = 500


class DimExprError(ConfigError):
    """``dim_when`` 不合法。

    继承 :class:`~pipeline.errors.ConfigError` 而不是 ``ValueError``，
    理由见后者的文档 —— 否则从 pydantic 验证器里抛出时会被包装成
    ``ValidationError``，「不论从哪个入口进来都是同一个错误类型」的保证就破了。
    """


def _check(node: ast.AST, fields: set[str]) -> None:
    """递归校验 AST，只放行白名单节点。"""
    match node:
        case ast.Expression():
            _check(node.body, fields)
        case ast.Compare(left=left, ops=ops, comparators=comps):
            if not all(isinstance(op, _ALLOWED_CMP) for op in ops):
                raise DimExprError("只允许 < <= > >= == != 这几种比较")
            _check(left, fields)
            for c in comps:
                _check(c, fields)
        case ast.BoolOp(op=op, values=values):
            if not isinstance(op, _ALLOWED_BOOL):
                raise DimExprError("只允许 and / or")
            for v in values:
                _check(v, fields)
        case ast.UnaryOp(op=ast.USub() | ast.UAdd(), operand=operand):
            _check(operand, fields)
        case ast.Call(func=ast.Name(id=fname), args=args, keywords=kw):
            if fname not in _ALLOWED_FUNCS:
                raise DimExprError(f"不允许调用 {fname}()，只允许 {sorted(_ALLOWED_FUNCS)}")
            if kw or len(args) != 1:
                raise DimExprError(f"{fname}() 只接受一个位置参数")
            _check(args[0], fields)
        case ast.Name(id=name):
            if name in _ALLOWED_FUNCS:
                raise DimExprError(f"{name} 是函数，是不是漏了括号？应写成 {name}(字段名)")
            fields.add(name)
        case ast.Constant(value=value):
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise DimExprError(f"只允许数字常量，得到 {value!r}")
            # 1e400 会解析成 inf，`abs(x) < 1e400` 则恒为真 —— 那不是作者的本意。
            if not math.isfinite(float(value)):
                raise DimExprError(f"数字常量必须是有限值，得到 {value!r}")
        case _:
            raise DimExprError(f"不允许的语法节点：{type(node).__name__}")


def _eval(node: ast.AST, row: Mapping[str, object]) -> bool | float | None:
    """在已校验过的 AST 上求值。``None``/``NaN`` 一路向上传播为 ``None``。"""
    match node:
        case ast.Expression():
            return _eval(node.body, row)
        case ast.Compare(left=left, ops=ops, comparators=comps):
            cur = _eval(left, row)
            for op, comp_node in zip(ops, comps, strict=True):
                right = _eval(comp_node, row)
                if cur is None or right is None:
                    return None  # 空值参与比较 → 未知，交给调用方 fail closed
                if not _apply_cmp(op, cur, right):
                    return False
                cur = right
            return True
        case ast.BoolOp(op=op, values=values):
            results = [_eval(v, row) for v in values]
            if isinstance(op, ast.And):
                if any(r is False for r in results):
                    return False
                return None if any(r is None for r in results) else True
            if any(r is True for r in results):
                return True
            return None if any(r is None for r in results) else False
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            n = _as_number(_eval(operand, row))
            return None if n is None else -n
        case ast.UnaryOp(op=ast.UAdd(), operand=operand):
            n = _as_number(_eval(operand, row))
            return None if n is None else +n
        case ast.Call(func=ast.Name(id="abs"), args=[arg]):
            n = _as_number(_eval(arg, row))
            return None if n is None else abs(n)
        case ast.Name(id=name):
            return _as_number(row.get(name))
        case ast.Constant(value=value):
            return float(value)  # type: ignore[arg-type]
    raise AssertionError(f"unreachable: {type(node).__name__}")  # pragma: no cover


def _apply_cmp(op: ast.cmpop, left: bool | float, right: bool | float) -> bool:
    lhs, rhs = float(left), float(right)
    match op:
        case ast.Lt():
            return lhs < rhs
        case ast.LtE():
            return lhs <= rhs
        case ast.Gt():
            return lhs > rhs
        case ast.GtE():
            return lhs >= rhs
        case ast.Eq():
            return lhs == rhs
        case ast.NotEq():
            return lhs != rhs
    raise AssertionError("unreachable")  # pragma: no cover


def _as_number(value: object) -> float | None:
    """把一行里的一个值转成 ``float``；**一切空值语义都收敛成 ``None``**。

    接受 ``numbers.Real`` 而不是只接受 ``int | float``，因为这一行的值来自数据库
    与 pandas：psycopg 会把 Postgres ``numeric`` 列（``alpha_t_stat`` 正是）
    还成 :class:`decimal.Decimal`，pandas 会把 ``n_obs`` 还成 ``numpy.int64``，
    而 ``pandas.NA`` / ``NaT`` 是**空值**。把它们当成类型错误抛出来，
    就等于「空值 fail closed」这条承诺对真实数据不成立。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool 是 int 的子类，会悄悄参与数值比较。dim_when 是数值判断，不接受布尔。
        return None
    # 注意 Decimal **没有**注册为 numbers.Real（它只注册了 numbers.Number），
    # 所以必须单列 —— 否则 psycopg 还回来的每一个 numeric 都会被当成空值，
    # 于是每一格都打灰，而「fail closed」看起来还在正常工作。
    if isinstance(value, numbers.Real | Decimal):
        f = float(value)
        # 必须是 isfinite 而不是 isnan：``r2 = inf`` 会让 ``r2 < 0.3``
        # 返回 False（**不打灰**）而不是 fail closed ——
        # 一个无穷大的 R² 显然是坏数据，不该被当成「拟合得很好」。
        return None if not math.isfinite(f) else f
    # pandas.NA / NaT 这类 null 哨兵：它们既不是 Real 也不是 None。
    # 用 `!= value` 识别不可靠（NA 的比较会抛），所以看它是不是自称为空。
    if value is not value:  # NaN 的经典自反性检查
        return None
    return None  # 其余一律视为「无可用数值」→ 由调用方 fail closed


@dataclass(frozen=True)
class DimExpr:
    """一个解析好的 ``dim_when``。

    ``fields`` 是它引用到的字段名，config 校验会拿它去核对这些字段
    确实是该指标的 output（否则就是一个永远求不出值的表达式）。
    """

    source: str
    fields: frozenset[str]
    _tree: ast.Expression

    def evaluate(self, row: Mapping[str, object]) -> bool:
        """求值。**空值与任何意外一律判定为「打灰」**（fail closed）。"""
        try:
            result = _eval(self._tree, row)
        except Exception:
            return True
        return True if result is None else bool(result)


def parse_dim_when(source: str) -> DimExpr:
    """解析并校验一条 ``dim_when``；非法即抛 :class:`DimExprError`。"""
    if len(source) > _MAX_SOURCE_LEN:
        raise DimExprError(f"dim_when 过长（{len(source)} > {_MAX_SOURCE_LEN} 字符）")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise DimExprError(
            f"dim_when 语法错误（第 {exc.offset} 字符处）：{source[:120]!r}"
        ) from exc
    except (RecursionError, MemoryError) as exc:
        # `"-"*10000 + "x"` 之类：一元运算符可以无括号地无限叠加，
        # CPython 对此抛的不是 SyntaxError。
        raise DimExprError(f"dim_when 嵌套过深：{source[:120]!r}") from exc

    fields: set[str] = set()
    _check(tree, fields)

    # 根节点必须是比较或布尔运算。否则 `dim_when: "alpha_t_stat"` 会被接受，
    # 而它的真实语义是「t 值恰好为 0 时打灰」—— 与作者意图相反，且悄无声息。
    if not isinstance(tree.body, ast.Compare | ast.BoolOp):
        raise DimExprError(
            f"dim_when 必须是一个比较表达式（如 `abs(x) < 2`），得到：{source[:120]!r}"
        )
    if not fields:
        raise DimExprError(f"dim_when 没有引用任何字段，是个常量：{source[:120]!r}")
    return DimExpr(source=source, fields=frozenset(fields), _tree=tree)
