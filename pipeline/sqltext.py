"""一个**只做一件事**的 SQL 词法掩码器。

``pipeline/schema.py`` 与 ``pipeline/invariants.py`` 都需要在一段 SQL 里找
「结构字符」—— 前者找配平的括号和顶层逗号，后者找第一个顶层分号和
``-- name:`` 标注。两边最初都用「按行切掉 ``--`` 之后的部分」这种近似，
而那个近似有真正的牙齿：

- ``0001_init.sql`` 的触发器函数体是 ``$$ … $$``，**里面有分号**；
- 字符串字面量里可以出现 ``--``、``;``、``(``、``)``，例如
  ``check (source in ('yfinance', 'manual--override'))``；
- 双引号标识符同理，而且一个列真的可以叫 ``"date"``。

按行切注释的解析器遇到这些会**悄悄给出一个错误的答案**，而不是报错 ——
对 §6.2 那条「双向列校验」来说，错误的列集合比解析失败危险得多。

所以这里只提供一个原语：:func:`mask`，把「不是结构」的字符抹掉，
**且保持长度与换行不变**。于是调用方可以在掩码上定位、在原文上取内容，
两边的下标一一对应。

三类词法单元各有开关，因为三个调用方要的组合都不一样：

===================================  =========  ========  ===========
调用方                               comments   literals  identifiers
===================================  =========  ========  ===========
``schema.py``（找括号与逗号）        抹         抹        抹
``invariants.py`` 找 ``-- name:``    **留**     抹        抹
测试里对迁移做文本断言               抹         **留**    **留**
===================================  =========  ========  ===========

中间那一行是重点：标注本身住在注释里，所以不能抹注释；但一个出现在字符串
或 ``$$ … $$`` 里的 ``-- name:`` **不是**标注，抹掉字面量才能把它们分开。
"""

from __future__ import annotations

import re

from pipeline.errors import ConfigError

__all__ = ["mask"]

# $$ … $$ 或 $tag$ … $tag$
_DOLLAR_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")
# E'…' 里 \' 是转义（普通 '…' 在 standard_conforming_strings=on 下不是）。
_E_PREFIX_RE = re.compile(r"(?:^|[^\w$])[eE]$")


def mask(
    sql: str,
    *,
    comments: bool = True,
    literals: bool = True,
    identifiers: bool = True,
) -> str:
    """返回与 ``sql`` 等长的掩码，换行位置不变。

    ``comments`` 抹掉 ``--`` 行注释与 ``/* */`` 块注释（块注释可嵌套）。
    ``literals`` 抹掉 ``'…'``、``E'…'`` 与 ``$tag$…$tag$``。
    ``identifiers`` 把 ``"…"`` 的**内容**换成 ``x``（保留两个引号）——
    它是标识符不是字面量，抹成空白会让调用方看不出「这里有一个词」。

    无论开关如何，扫描**始终**是词法正确的：关掉 ``literals`` 只是不抹它，
    并不意味着把它里面的 ``--`` 当注释。

    未闭合的引号或块注释会抛 :class:`ConfigError` —— **不猜**。
    """
    out = list(sql)
    n = len(sql)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    i = 0
    while i < n:
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            if comments:
                blank(i, j)
            i = j

        elif sql.startswith("/*", i):
            j = _end_of_block_comment(sql, i)
            if comments:
                blank(i, j)
            i = j

        elif sql[i] == "'":
            escaped = bool(_E_PREFIX_RE.search(sql, 0, i))
            j = _end_of_quoted(sql, i, escaped=escaped)
            if literals:
                blank(i, j)
            i = j

        elif sql[i] == '"':
            j = _end_of_quoted(sql, i, escaped=False)
            if identifiers:
                for k in range(i + 1, j - 1):
                    if out[k] != "\n":
                        out[k] = "x"
            i = j

        elif sql[i] == "$":
            # **美元引用必须起于一个词边界。** Postgres 允许标识符从第二个字符起
            # 含 `$`，而它的词法器在这种位置会继续读标识符、而不是开一个引用块。
            # 不要这条判断的话，`create table t (a$x$ int, b$x$ int, c int)`
            # 会从第一个 $x$ 一路抹到第二个，解析结果是 ['a', 'c'] ——
            # 一份**看起来很正常的、少了一列的**答案。
            if i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in "_$"):
                i += 1
                continue
            m = _DOLLAR_RE.match(sql, i)
            if m is None:  # $1 这种参数占位不是美元引用
                i += 1
                continue
            tag = m.group(0)
            end = sql.find(tag, m.end())
            if end < 0:
                raise ConfigError(f"未闭合的美元引用 {tag}，起于第 {_line(sql, i)} 行")
            j = end + len(tag)
            if literals:
                blank(i, j)
            i = j

        else:
            i += 1

    return "".join(out)


def _line(sql: str, idx: int) -> int:
    return sql.count("\n", 0, idx) + 1


def _end_of_block_comment(sql: str, start: int) -> int:
    """``/*`` 之后配平的 ``*/`` 的下一个下标。Postgres 的块注释可嵌套。"""
    depth = 0
    j = start
    n = len(sql)
    while j < n:
        if sql.startswith("/*", j):
            depth += 1
            j += 2
        elif sql.startswith("*/", j):
            depth -= 1
            j += 2
            if depth == 0:
                return j
        else:
            j += 1
    raise ConfigError(f"未闭合的块注释，起于第 {_line(sql, start)} 行")


def _end_of_quoted(sql: str, start: int, *, escaped: bool) -> int:
    """``sql[start]`` 那个引号对应的闭合引号的下一个下标。

    ``escaped`` 为真时额外认 ``\\x`` 转义 —— 那是 ``E'…'`` 的规则。
    普通 ``'…'`` 在 ``standard_conforming_strings=on``（PG9.1 起的默认）下
    **不**认反斜杠，所以默认关闭：多认一种转义会让
    ``'C:\\path\\'`` 这类字面量被误判成未闭合。
    """
    q = sql[start]
    j = start + 1
    n = len(sql)
    while j < n:
        if escaped and sql[j] == "\\" and j + 1 < n:
            j += 2
            continue
        if sql[j] == q:
            if j + 1 < n and sql[j + 1] == q:  # '' / "" 是转义
                j += 2
                continue
            return j + 1
        j += 1
    raise ConfigError(f"未闭合的 {q} 引号，起于第 {_line(sql, start)} 行")
