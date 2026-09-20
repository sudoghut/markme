"""跑 ``supabase/invariants.sql`` 并在有违规时**非零退出**（§9.3.2）。

``keepalive.yml`` 的入口。它以 **pipeline_writer** 身份连库，而不是 postgres
—— 这一点是 M3 的一条 SERIOUS 逼出来的：初版的两条安全断言查
``information_schema.role_table_grants``，那个视图对调用者是有过滤的，
以 writer 跑时**返回 0 行、静默漏过**。手动跑好好的，一挂上定时任务就永久绿。

所以这个入口存在的理由不只是「有个地方能跑」，而是：
**跑它的身份必须和生产一致**，否则验证的是另一件事。
"""

from __future__ import annotations

import os
import sys
from typing import Any

from pipeline.invariants import run_invariants

__all__ = ["main"]


def main() -> int:
    dsn = os.environ.get("MARKME_DB_URL")
    if not dsn:
        sys.stderr.write("缺少 MARKME_DB_URL\n")
        return 1

    import psycopg

    with psycopg.connect(dsn) as conn:

        def execute(sql: str) -> list[dict[str, Any]]:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d.name for d in cur.description or []]
                return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

        who = execute("select current_user as u")[0]["u"]
        violations = run_invariants(execute)

    sys.stdout.write(f"以 {who} 身份跑了 invariants.sql\n")
    if not violations:
        sys.stdout.write("ALL PASS\n")
        return 0

    sys.stdout.write(f"\n{len(violations)} 条违规：\n")
    for v in violations:
        sys.stdout.write(f"- {v.assertion}\n")
        for row in v.rows:
            sys.stdout.write(f"    {row}\n")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
