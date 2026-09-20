"""跑 ``supabase/invariants.sql`` 里的断言，逐条报告（§9.3.2 / §12 #9）。

每条断言**必须返回 0 行**。返回了行就是违规。本模块只负责解析与执行并把
违规清单交回调用方（M5 的 keepalive job 据此非零退出）—— 它自己不碰连接，
于是「怎么连库」这件事只在一个地方决定。

为什么要有这个脚本而不是把 SQL 直接丢给 psql：
断言之间需要**逐条命名报告** —— 「invariants.sql 失败了」对排查毫无帮助，
「`public 下的每张表都必须开启 RLS` 返回了 sectors」才有。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline.errors import ConfigError
from pipeline.sqltext import mask

__all__ = ["Assertion", "Violation", "parse_invariants", "run_invariants"]

INVARIANTS_SQL = Path(__file__).resolve().parent.parent / "supabase" / "invariants.sql"

# 「少了一条断言」这种损坏**没有任何症状** —— 文件照样解析、照样全绿，
# 只是少验了一件事。所以这个正则对两类手滑都得宽容：
#   · 大小写（`-- Name:`）
#   · 行首缩进（`  -- name:`）
# 两者都会让标注失配，而失配的后果是那条断言被并进上一条、随后被
# `_first_statement` 在第一个分号处**整段丢掉**。
_NAME_RE = re.compile(r"^[ \t]*--\s*name:\s*(?P<name>.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Assertion:
    name: str
    sql: str


@dataclass(frozen=True)
class Violation:
    assertion: str
    rows: list[str]


def parse_invariants(path: Path | None = None) -> list[Assertion]:
    """按 ``-- name: …`` 把文件切成一条条带名字的断言。"""
    src = path or INVARIANTS_SQL
    text = src.read_text(encoding="utf-8")
    masked = mask(text)
    # 找标注要**留着注释**（标注就住在注释里），但要抹掉字面量与 $$ 块 ——
    # 一个出现在字符串里的 `-- name:` 不是标注，而把它当标注会从一条
    # 断言的正中间切一刀，切出来的前半段还是个未闭合的引号。
    for_marks = mask(text, comments=False)
    marks = list(_NAME_RE.finditer(for_marks))
    if not marks:
        raise ValueError(f"{src} 里没有任何 `-- name:` 标注")

    out: list[Assertion] = []
    seen: set[str] = set()
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        name = text[m.start("name") : m.end("name")]
        body = _first_statement(text[m.end() : end], masked[m.end() : end])
        if not body:
            # 静默丢掉一条断言，对一个「少一条就没有症状」的文件来说
            # 是最糟的默认行为。
            raise ConfigError(f"{src}：`-- name: {name}` 后面没有语句")
        if name in seen:
            raise ConfigError(f"{src} 里有两条同名断言：{name!r} —— 报告会分不清是哪一条")
        seen.add(name)
        out.append(Assertion(name=name, sql=body))
    return out


def _first_statement(chunk: str, masked_chunk: str) -> str:
    """取 ``chunk`` 里**第一条**语句（到第一个顶层分号为止）。

    只剥尾部的 ``;`` 是不够的：一条断言后面常跟着解释它为什么存在的散文，
    那些散文是 ``--`` 注释，剥完尾分号就会被一起送进 ``execute()``。
    多数驱动会把「SQL + 一堆注释」照样跑过去，于是**这个 bug 没有症状**，
    直到某天某条注释里出现一个分号，那条断言就开始报「语法错误」——
    而错误信息会指向一段注释。这里直接在掩码上切第一个顶层分号。
    """
    depth = 0
    for i, ch in enumerate(masked_chunk):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            return chunk[:i].strip()
    return chunk.strip()


def run_invariants(
    execute: Callable[[str], Sequence[dict[str, object]]],
    path: Path | None = None,
) -> list[Violation]:
    """逐条执行。``execute`` 接受一段 SQL、返回行列表。

    返回违规清单（空列表表示全过）。**一条断言自身报错也算违规** ——
    一个跑不起来的探测器和一个不触发的探测器一样没用。
    """
    violations: list[Violation] = []
    for a in parse_invariants(path):
        try:
            rows = execute(a.sql)
        except Exception as exc:
            violations.append(Violation(a.name, [f"断言本身执行失败：{exc}"]))
            continue
        if rows:
            violations.append(
                Violation(a.name, [str(next(iter(r.values()), r)) for r in rows[:20]])
            )
    return violations
