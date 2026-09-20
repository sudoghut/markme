"""配置层的异常基类。

单独成模块是为了让 :mod:`pipeline.dim_expr` 与 :mod:`pipeline.config`
共用同一个基类而不互相 import。
"""

from __future__ import annotations

__all__ = ["ConfigError"]


class ConfigError(Exception):
    """配置不合法。一律在加载期抛出，不留到运行期。

    **继承 ``Exception`` 而不是 ``ValueError`` 是刻意的。**
    pydantic 会把验证器里抛出的 ``ValueError`` 包装成 ``ValidationError``，
    于是同一个配置错误会因为「你是直接构造模型还是走 :func:`~pipeline.config.load_config`」
    而变成两种类型。非 ``ValueError`` 的异常会被 pydantic 原样放行，
    这样不论从哪个入口进来，拿到的都是同一个带说明的 ``ConfigError``。
    """
