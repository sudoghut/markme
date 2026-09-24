/**
 * 「最近的一个即将发生的事件」—— 表格「事件」列画的那个倒计时标签（§10.2）。
 *
 * **单独拿出来是因为它有两个消费者**：画标签的 `NextEvent`，和按这一列排序的
 * `sortValue`。这两处必须看同一个数 —— 一旦各算各的，屏幕上按天数排出来的顺序
 * 会和标签上印的天数对不上，而那种错**看不出来**（每一行单独看都是对的）。
 */
import { metricValue } from "./metricValue";
import type { MetricRow } from "./supabase";

export type NextEvent = {
  days: number;
  kind: "财报" | "除息";
  /** 未确认的事件日（§3.5）。`null` = 数据库里就没说。 */
  estimated: boolean | null;
};

/** 财报与除息里**先到的那个**；两个都没有则 `null`。 */
export function nextEvent(row: MetricRow): NextEvent | null {
  const earnings = metricValue(row, "days_to_next_earnings");
  const dividend = metricValue(row, "days_to_next_dividend");

  // 同一天时取财报 —— 与初版 `e <= d` 的取舍一致，别在重构里悄悄改掉。
  if (earnings !== null && (dividend === null || earnings <= dividend)) {
    return { days: earnings, kind: "财报", estimated: row["next_earnings_is_estimated"] as boolean | null };
  }
  if (dividend !== null) {
    return { days: dividend, kind: "除息", estimated: row["next_dividend_is_estimated"] as boolean | null };
  }
  return null;
}
