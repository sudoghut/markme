/**
 * **显式列与 `extra` 双路读取** —— M6 的一条验收标准。
 *
 * §3.6 承诺「新增一个指标 = 写函数 + 改 config，不改前端代码」，而
 * §6.3 的脚注说得很直白：那个承诺**只有在前端从第一天起就同时读
 * 显式列和 `extra->>'<id>'` 时才成立**。如果 M6 只接了显式列，
 * 这个承诺是假的，而且要到第一次加指标时才会发现。
 */
import type { MetricRow } from "./supabase";

export function metricValue(row: MetricRow, id: string): number | null {
  const direct = row[id];
  if (direct !== undefined && direct !== null) {
    const n = Number(direct);
    return Number.isFinite(n) ? n : null;
  }
  const extra = row.extra;
  if (extra && id in extra) {
    const v = extra[id];
    if (v === null || v === undefined) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** 这一行的这个指标是不是「预热不足」（§3.3 软闸门 → §10.4 灰标）。 */
export function isProvisional(row: MetricRow, id: string): boolean {
  return (row.provisional_metrics ?? []).includes(id);
}
