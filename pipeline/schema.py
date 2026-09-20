"""从 ``0001_init.sql`` 里解析出真实的列集合（§6.2 校验 1/2）。

M1 期间 ``check_columns_match`` 的「数据库列」是一份**手抄常量**——
两名 reviewer 独立核对过它与设计文档一致，但那终究是第二份需要对齐的名单。
这里让它真的去读 SQL，于是那份常量可以删掉。

**为什么不连数据库去读 information_schema**：那样 CI 就需要凭证，
而 §8.4 已经说明 fork PR 拿不到 secret。解析文件让这条校验在任何地方都能跑，
包括外部贡献者的 PR —— 而那恰恰是它最该起作用的时候。

**解析器的设计底线是「宁可响亮失败，不可悄悄答错」**：
它只在 :func:`pipeline.sqltext.mask` 给出的词法掩码上找结构字符（括号、逗号），
于是 ``$$ … $$`` 函数体里的分号、字符串里的 ``--`` 和括号都不会把它带偏；
同名表出现两次直接抛异常，而不是取第一处 —— 取第一处会在「迁移里加了一张
同名临时表」这种场景下返回一份**看起来很正常的错误列集合**，
而错误的列集合恰恰会让 §6.2 的双向校验变成一道假的关口。
"""

from __future__ import annotations

import re
from pathlib import Path

from pipeline.errors import ConfigError
from pipeline.sqltext import mask

__all__ = [
    "MIGRATIONS_DIR",
    "metrics_daily_columns",
    "normalize_identifier",
    "parse_create_table_columns",
    "table_names",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "supabase" / "migrations"

# `create [global|local] [temp|temporary|unlogged] table <name> (`
# —— 允许带 schema 前缀（private.runs）。
#
# **temp / unlogged 必须认。** 漏掉它们，「同名表出现两次要抛」这条保护
# 恰好在唯一会用到它的场景里失效（有人加了一张同名临时表），而
# 「回滚脚本 drop 了 init 建的每一张表」那条测试也会漏掉新加的 unlogged 表
# —— 那张表对 table_names 是隐形的，连计数都不变。
#
# 名字后面接 `(`（有列清单）**或** `as`（CTAS）。CTAS 也要认：
# `table_names` 的唯一消费者是「回滚脚本 drop 了 init 建的每一张表」那条测试，
# 而一张 `create table … as select …` 建出来的表同样是真表、同样需要 drop。
# 不认它，那条测试就对这类表隐形 —— 连表数都不会变。
#
# 表名允许带引号（``"Archive"``、``"My Schema"."My Table"``）。只认 ``[\w.]+``
# 的话，一张带引号的表对 ``table_names`` 完全隐形 —— 于是「回滚 drop 了每一张表」
# 那条测试看不见它，而回滚后重跑 init 会撞 already exists。
# 掩码里引号内的内容是 ``x``，所以 ``"[^"]*"`` 在掩码上永远能配平。
_IDENT = r'(?:"[^"]*"|[\w$]+)'
_CREATE_RE = re.compile(
    r"create\s+(?:(?:global|local)\s+)?(?:(?:temp(?:orary)?|unlogged)\s+)?table\s+"
    rf"(?:if\s+not\s+exists\s+)?(?P<name>{_IDENT}(?:\s*\.\s*{_IDENT})?)\s*(?P<form>\(|as\b)",
    re.IGNORECASE,
)
# 建表体里一项的首个标识符：要么 "带引号的"，要么裸标识符。
_ITEM_NAME_RE = re.compile(r'"|[\w$]+')
# 表级约束（primary key / unique / check / constraint / foreign key）不是列。
#
# **只对不带引号的标识符生效。** `"check" boolean` 是一个合法的、叫 check 的列；
# 把它也滤掉会让解析器**少报一列**，而少报一列正是 §6.2 那条双向校验
# 最该抓、却会因此静默放过的方向：schema 建了、config 没声明、没人计算。
#
# ``exclude`` **不在这份名单里**：它是 unreserved 的，``exclude boolean`` 是
# 一个合法的列。它由 :func:`_looks_like_a_constraint` 另行判断。
_NOT_A_COLUMN = frozenset({"primary", "unique", "check", "constraint", "foreign", "like"})


def normalize_identifier(raw: str) -> str:
    """把 SQL 里写的表名归一化成它在 Postgres 里的真实名字。

    规则就是 Postgres 的规则：不带引号的折叠成小写，带引号的**保留大小写**
    并把 ``""`` 解成 ``"``。限定名的每一段各自适用。
    """
    parts: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if raw[i].isspace() or raw[i] == ".":
            i += 1
            continue
        if raw[i] == '"':
            j = i + 1
            while j < n:
                if raw[j] == '"':
                    if j + 1 < n and raw[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            parts.append(raw[i + 1 : j].replace('""', '"'))
            i = j + 1
        else:
            j = i
            while j < n and not (raw[j].isspace() or raw[j] == "."):
                j += 1
            parts.append(raw[i:j].lower())
            i = j
    return ".".join(parts)


def table_names(sql: str) -> list[str]:
    """SQL 里所有 ``create table`` 的表名，按出现顺序。

    给测试用：让「回滚脚本 drop 了 init 建的每一张表」这条断言从**同一份
    SQL** 推出表清单，而不是再手抄一份 —— 手抄的那份正是 M3 要消灭的东西。
    """
    masked = mask(sql)
    return [
        normalize_identifier(sql[m.start("name") : m.end("name")])
        for m in _CREATE_RE.finditer(masked)
    ]


def parse_create_table_columns(sql: str, table: str) -> list[str]:
    """从一段 SQL 里取出 ``table`` 的列名，按声明顺序。

    找不到表就抛，不返回空集 —— 一个悄悄返回空集的解析器会让 §6.2 的
    双向校验变成永远通过。同名表出现多次同样抛：见模块 docstring。
    """
    masked = mask(sql)
    wanted = normalize_identifier(table)
    hits = [
        m
        for m in _CREATE_RE.finditer(masked)
        if normalize_identifier(sql[m.start("name") : m.end("name")]) == wanted
    ]
    if not hits:
        raise ConfigError(f"在 SQL 里找不到 `create table {table}`")
    if len(hits) > 1:
        lines = [masked.count("\n", 0, m.start()) + 1 for m in hits]
        raise ConfigError(
            f"`create table {table}` 在 SQL 里出现了 {len(hits)} 次（第 {lines} 行）。"
            "取第一处会返回一份看起来很正常的错误列集合 —— 这里拒绝猜。"
        )
    if hits[0].group("form").lower() == "as":
        # `create table t as select …` 没有列清单，列名由那条 select 决定。
        # 解析它需要一个真正的 SQL 引擎 —— 这里**拒绝猜**，而不是返回空集。
        raise ConfigError(
            f"`create table {table} as …`（CTAS）没有列清单，解析不出列。"
            "若 §6.2 的校验真要覆盖这种表，得改成连库读 information_schema。"
        )
    open_idx = hits[0].end() - 1
    end_idx = _matching_paren(masked, open_idx, table)
    return _columns_from_body(sql[open_idx + 1 : end_idx], masked[open_idx + 1 : end_idx])


def _matching_paren(masked: str, open_idx: int, table: str) -> int:
    """``masked[open_idx] == '('`` 对应的右括号下标。"""
    depth = 0
    for i in range(open_idx, len(masked)):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    line = masked.count("\n", 0, open_idx) + 1
    raise ConfigError(f"`create table {table}`（第 {line} 行）的括号没有配平，文件到末尾仍未闭合")


def _columns_from_body(body: str, masked_body: str) -> list[str]:
    """把建表体切成顶层逗号分隔的项，取出其中是列定义的那些。

    结构（深度、逗号）看 ``masked_body``，内容取 ``body`` —— 两者等长同偏移。
    """
    spans: list[tuple[int, int]] = []
    start = 0
    depth = 0
    for i, ch in enumerate(masked_body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            spans.append((start, i))
            start = i + 1
    spans.append((start, len(masked_body)))

    columns: list[str] = []
    for lo, hi in spans:
        # 名字的位置在掩码上定，内容在原文上取 —— 只有这样才能既跳过
        # 整段注释（掩码上全是空白），又拿到真的标识符。
        m = _ITEM_NAME_RE.search(masked_body, lo, hi)
        if m is None:
            continue  # 这一项在掩码上什么都没剩：它整个是注释
        if m.group(0) == '"':
            # 带引号：掩码里是 "xxxx"，长度可靠，内容去原文取。
            #
            # **不 lower()，并且要把 `""` 解成 `"`。** 这两条都是 Postgres 的
            # 标识符规则：不带引号的标识符折叠成小写，带引号的**原样保留大小写**。
            # 把 `"RSI_14"` 报成 `rsi_14`，正好让「config 写 rsi_14、库里其实是
            # RSI_14」这件事通过 §6.2 的校验 —— 而线上 PostgREST 会对 rsi_14
            # 返回 404。一个会答错的解析器，答错的方向恰好是校验最该抓的那个。
            end = masked_body.index('"', m.end(), hi) + 1
            columns.append(body[m.start() + 1 : end - 1].replace('""', '"'))
            continue
        # 不带引号的标识符：Postgres 折叠成小写。
        name = m.group(0).lower()
        if name in _NOT_A_COLUMN:
            continue  # 表级约束
        if name == "exclude" and _looks_like_a_constraint(masked_body, m.end(), hi):
            continue
        columns.append(name)
    if not columns:
        # `create table t (like other including all)` 会走到这里。
        # 返回空集会让 §6.2 的双向校验变成永远通过 —— 宁可响亮失败。
        raise ConfigError("解析出的列集合为空；这张表可能用了 `like` 继承或别的没处理的语法")
    return columns


def _looks_like_a_constraint(masked_body: str, start: int, end: int) -> bool:
    """``exclude`` 是不是表级约束而不是一个叫 exclude 的列？

    它和 ``primary`` / ``unique`` / ``check`` 不同：那几个在 Postgres 里是
    **保留字**，不加引号就当不了列名，所以无条件当约束是安全的。
    ``exclude`` 是 unreserved 的 —— ``exclude boolean`` 是一个合法的列，
    而把它当约束滤掉就是「少报一列」，正是 §6.2 最该抓的那个方向。
    约束写法只有 ``exclude using …`` 和 ``exclude (…)``。
    """
    rest = masked_body[start:end].lstrip()
    return rest.startswith("(") or rest[:5].lower() == "using"


def metrics_daily_columns(migrations_dir: Path | None = None) -> set[str]:
    """``metrics_daily`` 在 ``0001_init.sql`` 里的实际列集合。

    直接把**整张表的列**交给 :func:`pipeline.config.check_columns_match` 即可 ——
    它自己会剔除 ``STRUCTURAL_COLUMNS``。让调用方维护一份「哪些列是结构列」
    只会制造第二份需要对齐的名单（§6.1.1）。
    """
    path = (migrations_dir or MIGRATIONS_DIR) / "0001_init.sql"
    if not path.is_file():
        raise ConfigError(f"缺少迁移文件：{path}")
    return set(parse_create_table_columns(path.read_text(encoding="utf-8"), "metrics_daily"))
