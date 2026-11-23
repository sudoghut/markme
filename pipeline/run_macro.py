"""利率政策管道入口（docs/add-rate-policy.md，M16）。

**与 ``run_daily`` 完全独立** —— 不同进程、不同 workflow、不写 ``private.runs``。
利率是一个可选的装饰性板块，它的任何故障都不该有办法（哪怕是间接地）改变价格与
指标的状态码、重试或告警。

三个来源**互相独立**：

===========  ==========================================  ==========================
来源          写入                                         失败时
===========  ==========================================  ==========================
``nyfed``     整段历史 upsert（含 ``change_*`` 重算）       旧数据原样保留
``treasury``  今年 + 去年 upsert                            旧数据原样保留
``fomc``      **同一事务**里整窗删 + 插                      旧日程原样保留（不清空）
===========  ==========================================  ==========================

「失败」包括网络错误、响应形状对不上预期（解析器**宁可拒收、不可猜**）、以及自检不过。
每个来源的结果记进 ``private.macro_fetch_state``，**自己提交**、在数据事务之外 ——
数据事务回滚了，失败原因也要留下。

退出码：任一来源失败 → 1（workflow 变红、发通知）；全部成功 → 0。
一次成功的跑没有「部分成功」这种灰色地带。
"""

from __future__ import annotations

import os
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pipeline.calendar_gate import ET
from pipeline.macro.config import load_macro_config
from pipeline.macro.errors import MacroParseError
from pipeline.macro.fomc import FOMC_URL, coverage_start, parse_fomc
from pipeline.macro.nyfed import effr_url, parse_effr, with_changes
from pipeline.macro.treasury import (
    YieldPoint,
    check_against_policy_rate,
    parse_par_yields,
    treasury_url,
)
from pipeline.store import (
    connect,
    data_transaction,
    record_macro_fetch,
    replace_fomc_meetings,
    upsert_rate_policy,
    upsert_treasury_yields,
)
from pipeline.throttle import USER_AGENT, RequestBudget

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from collections.abc import Callable
    from datetime import date

    import psycopg

    from pipeline.macro.config import MacroConfig

__all__ = ["MacroReport", "http_get", "main", "run_macro"]


@dataclass
class MacroReport:
    #: 来源 → 失败原因；成功的来源不在里面。
    failures: dict[str, str] = field(default_factory=dict)
    #: 来源 → 写入行数。
    rows: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def describe(self) -> str:
        parts = [f"{s}: {n} 行" for s, n in self.rows.items()]
        parts += [f"{s}: 失败（{why}）" for s, why in self.failures.items()]
        return "；".join(parts) if parts else "没有任何来源被执行"


def http_get(url: str, timeout: float) -> str:
    """一次 GET，返回文本。诚实的 User-Agent（含仓库地址），非 2xx 抛异常。

    只用标准库：不为这三个来源新增任何依赖。
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        status = resp.status
        if status != 200:
            raise RuntimeError(f"{url} 返回 {status}")
        return str(resp.read().decode("utf-8-sig"))


def run_macro(
    conn: psycopg.Connection[Any],
    cfg: MacroConfig,
    *,
    today: date,
    fetch: Callable[[str], str],
    budget: RequestBudget | None = None,
) -> MacroReport:
    """跑一次。``fetch`` 与 ``today`` 可注入 —— 测试不连网、不看真实时钟。

    **每个来源各自 try/except、各自事务。** 一个来源失败不影响另外两个落库；
    失败的来源**一律不动库里的旧数据**（数据事务回滚，状态单独记）。
    """
    report = MacroReport()
    bud = budget or RequestBudget(
        max_requests=cfg.max_requests_per_run,
        interval_seconds=cfg.request_interval_seconds,
        retry_max_attempts=cfg.retry_max_attempts,
    )

    def get(url: str, what: str) -> str:
        return bud.request(lambda: fetch(url), what=what)

    #: nyfed 这一跑解析出的最新 EFFR，给 treasury 做量级核对。nyfed 失败时保持 ``None``。
    anchor: dict[str, Decimal] = {}

    def nyfed() -> int:
        payload = get(effr_url(cfg.history_start, today), "纽约联储 EFFR / 目标区间")
        points = parse_effr(payload)
        rows = with_changes(points)
        anchor["effr"] = points[-1].effr
        with data_transaction(conn):
            return upsert_rate_policy(conn, rows)

    def treasury() -> int:
        points: list[YieldPoint] = []
        for year in (today.year - 1, today.year):
            text = get(treasury_url(year), f"财政部国债收益率 {year}")
            # 新年头两周当年的 CSV 可能还只有表头，那是正常的；其余时候空表就是出事了。
            early_jan = year == today.year and today.month == 1 and today.day <= 14
            points += parse_par_yields(text, cfg.treasury_tenors, allow_empty=early_jan)
        if not points:
            raise MacroParseError("财政部两年的 CSV 合起来没有任何数据点")
        effr = anchor.get("effr")
        if effr is not None:
            check_against_policy_rate(points, effr)
        rows = [{"date": p.date, "tenor": p.tenor, "yield_pct": p.yield_pct} for p in points]
        with data_transaction(conn):
            return upsert_treasury_yields(conn, rows)

    def fomc() -> int:
        meetings = parse_fomc(get(FOMC_URL, "美联储 FOMC 日程"), today)
        start = coverage_start(today)
        rows = [
            {"start_date": m.start_date, "end_date": m.end_date}
            for m in meetings
            if m.end_date >= start
        ]
        # 删 + 插在同一个事务里，且**仅当解析与自检通过**（parse_fomc 不通过会抛）。
        with data_transaction(conn):
            return replace_fomc_meetings(conn, start, rows)

    for source, fn in (("nyfed", nyfed), ("treasury", treasury), ("fomc", fomc)):
        try:
            report.rows[source] = fn()
        except Exception as exc:
            # 数据事务已在 data_transaction 里回滚；这里再保险一次，
            # 再把失败原因记下来（自己提交，不依赖那个已回滚的事务）。
            conn.rollback()
            why = f"{type(exc).__name__}: {exc}"
            report.failures[source] = why
            record_macro_fetch(conn, source, why)
        else:
            record_macro_fetch(conn, source, None)
    return report


def main(argv: list[str] | None = None) -> int:
    del argv
    dsn = os.environ.get("MARKME_DB_URL")
    if not dsn:
        sys.stderr.write("缺少 MARKME_DB_URL\n")
        return 1
    cfg = load_macro_config()
    today = datetime.now(tz=ET).date()
    with connect(dsn) as conn:
        report = run_macro(
            conn,
            cfg,
            today=today,
            fetch=lambda url: http_get(url, cfg.request_timeout_seconds),
        )
    sys.stdout.write(f"{'ok' if report.ok else 'failed'}: {report.describe()}\n")
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
