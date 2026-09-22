/**
 * 市场时区与「陈旧」的判据 —— **一份定义，页面与演示页共用**。
 *
 * 之前这两样东西散在 `app/page.tsx` 里：时区藏在一个私有的 `todayISO()`，
 * 阈值藏在 `StaleBanner` 内部的 `<= 1`。于是 `/states` 那页只能靠手填一个
 * 数字去猜真实页面会不会亮黄条 —— 演示与真相可以静默地分叉。
 *
 * **这个文件不许 import `@/lib/config`。** 它被 `components/States.tsx` 用，
 * 而那个文件又被 `app/error.tsx`（`"use client"`）用 —— 于是 `lib/config`
 * 的 `node:fs` / `node:path` 会被拖进客户端 bundle，`next build` 当场失败。
 * 实测撞过一次。这里的东西必须是纯常量与纯函数。
 */

/**
 * 所有日期口径的时区。Python 那边的对应物是 `pipeline/calendar_gate.py` 的
 * `ET`（注释写着「全项目唯一的交易所时区常量」）—— 跨语言没法共享一个字面量，
 * 所以两边各留一个具名常量并互相指认，比两串裸字符串强。
 */
const MARKET_TZ = "America/New_York";

/** 给人看的时区标注。日期是交易所当地日，不写出来就会被读成本地时间。 */
export const MARKET_TZ_LABEL = "美东";

/**
 * 落后多少个**日历日**才亮陈旧黄条（§10.6）。
 *
 * 初版是「>1 个交易日」，而管道在收盘后约一小时才写入 —— 于是从收盘到写入
 * 之间、以及整个周末和假日，页面都在亮黄条说「管道可能停了」。数据完全正常，
 * 告警照出：一条天天出现的告警会被训练成噪声，等真的停了也没人会看，
 * 恰好废掉它唯一的用途。
 *
 * 用日历日而不是交易日，是因为「一周过去了还没更新」本身就是日历日的语义。
 * 美股最长的连续休市约 4 个日历日，所以 7 天窗口里必然含 ≥3 个 session，
 * 亮起来一定是真事；而 7 个**交易日**要等 9 天以上，白白把 dead-man 窗口
 * 拉长一倍。
 *
 * **不做成 config 旋钮**：这个 7 是从市场结构推出来的（最长休市 ≈ 4 天），
 * 不是一项部署偏好，和 `MARKET_TZ` 同类。放进 `config/app.yaml` 还会把它
 * 拖进 pipeline 的 `_Strict` schema —— 一个纯前端的数字让收盘抓取多一个
 * 失败理由，不划算。
 */
export const STALE_AFTER_DAYS = 7;

/** 两个 `YYYY-MM-DD` 之间的日历日数。两边都按 UTC 午夜解析，所以差值是整天。 */
export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000);
}

/**
 * **交易所当地日期，不是 UTC 日期。**
 *
 * 用 UTC 时，从 UTC 午夜到下一个美股 session 之间的那几个小时里，
 * 「今天」会提前跨到下一天，于是查询把一个**预填的未来 session** 也算进来，
 * 落后天数就会多出一天 —— 一个在每天固定时段自动发生、而数据其实完全正常的偏差。
 */
export function todayISO(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: MARKET_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}
