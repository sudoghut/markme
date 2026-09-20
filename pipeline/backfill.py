"""全量回填（dispatch only）。

**回填不过是「每天重抓整窗」这个操作的第一次执行**（§6.1.1）——
``lookback_bars`` 只有一个，回填与日常运行共用。这不是巧合而是设计：
两个数字要对齐的地方，就是将来会不对齐的地方。

所以这里没有第二套逻辑，只有三点不同：

1. ``backfill_interval_seconds``（默认 5，比日常的 2 更慢）—— §7.3.1
   「回填是一次性的大动作：按标的分批、批间 sleep，宁可跑 20 分钟」。
2. 跳过闸门 1/2（``--force``）：dispatch 触发，周末跑很正常。
3. ``from_date`` 只作为**人工覆盖**存在，默认值由 ``lookback_bars`` 推出。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from pipeline.calendar_gate import ET
from pipeline.config import load_config
from pipeline.run_daily import run_once
from pipeline.store import connect, finish_run, start_run

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    dsn = os.environ.get("MARKME_DB_URL")
    if not dsn:
        sys.stderr.write("缺少 MARKME_DB_URL\n")
        return 1
    _ = argv if argv is not None else sys.argv[1:]

    cfg = load_config()
    now = datetime.now(tz=ET)
    with connect(dsn) as conn:
        run_id = start_run(conn, now.date(), os.environ.get("GITHUB_SHA"))
        try:
            report = run_once(conn, cfg, now=now, force=True, backfill=True)
        except Exception as exc:
            conn.rollback()
            finish_run(conn, run_id, "failed", message=f"{type(exc).__name__}: {exc}")
            sys.stderr.write(f"failed: {exc}\n")
            return 1
        finish_run(
            conn,
            run_id,
            report.status,
            session_date=report.session_date,
            rows_prices=report.rows_prices,
            rows_metrics=report.rows_metrics,
            message="backfill: " + report.message,
        )
        sys.stdout.write(f"{report.status}: {report.message}\n")
        return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
