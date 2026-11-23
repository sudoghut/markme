/**
 * 利率窄条的**全部判断逻辑**（docs/add-rate-policy.md §3 / §4.5）。纯函数，没有 I/O。
 *
 * 为什么单独拿出来：这条窄条有五种状态（正常 / 部分陈旧 / 空表 / 读失败 / 没有日程），
 * 而组件只该负责画。判断逻辑塞进组件里就只能靠肉眼验收 ——
 * 拿出来之后 `rates.test.ts` 能逐条断言（`npm test`，CI 的 web job 跑它）。
 *
 * **判加息 / 降息不在这里。** 它只在 Python 管道里做一次、落成 `change_kind` /
 * `change_bp`，这里只读。两份实现就是两个要对齐的地方（BACKLOG 里 `dim_expr` 孪生的教训）。
 *
 * 利率一律是**百分数**（4.63 = 4.63%），存的和展示的是同一个单位，这里不做 ×100 / ÷100。
 */
import { daysBetween } from "./market.ts";

export type ChangeKind = "up" | "down" | "mixed";

export type RatePolicyRow = {
  date: string;
  target_lower: number;
  target_upper: number;
  effr: number;
  change_kind: ChangeKind | null;
  change_bp: number | null;
};

/** 「上次变动」只需要这三列。 */
export type ChangeRow = Pick<RatePolicyRow, "date" | "change_kind" | "change_bp">;

export type YieldRow = { date: string; tenor: string; yield_pct: number };
export type MeetingRow = {
  start_date: string;
  end_date: string;
  /** 这份日程最近一次被整窗重写的时刻（timestamptz）。日程**抓取**陈旧与否只能看它。 */
  fetched_at: string;
};
export type TenorLabel = { key: string; label: string };

export type StaleAfterDays = { target_range: number; yields: number; fomc: number };

export type RatesInput = {
  /** 最新一行目标区间；表空时为 `null`。 */
  latest: RatePolicyRow | null;
  /** 最近一次 `change_kind` 非空的行；库里从未变动过时为 `null`。 */
  lastChange: ChangeRow | null;
  /**
   * 库里**实际最早**的目标区间日期；表空时为 `null`。「无变动」只能从这天说起 ——
   * 不能从配置里的 `history_start` 说起：表为空（或刚回填了一行）时，那是在断言一个没有数据支撑的事实。
   */
  earliest: string | null;
  /** 国债收益率，**日期降序**（调用方按 `order=date.desc` 取）。 */
  yields: YieldRow[];
  /** `end_date > 今天` 的最早一场；没有时为 `null`。 */
  nextMeeting: MeetingRow | null;
};

export type YieldCell = {
  key: string;
  label: string;
  value: number;
  /** 较该期限前一个有数据的交易日的变动（bp，整数，带符号）；只有一个点时为 `null`。 */
  changeBp: number | null;
  date: string;
};

export type RatesModel = {
  range: { lower: number; upper: number; date: string; stale: boolean } | null;
  lastChange: { text: string; kind: ChangeKind; date: string; daysAgo: number } | null;
  /**
   * `lastChange` 为 `null` 且**确有目标区间数据**时，页面写「自 <这个日期> 起无变动」。
   * 没有目标区间数据（`range` 为 `null`）时它也是 `null`，此时页面**不写**「上次变动」。
   */
  noChangeSince: string | null;
  yields: { cells: YieldCell[]; spread2s10sBp: number | null; stale: boolean } | null;
  /** `null` = 没有已知的下次会议（页面写「暂无日程」，不写大数字）。 */
  meeting: {
    text: string;
    /** 距会议**开始日**的日历日数；会议进行中（开始日 ≤ 今天 < 结束日）时 ≤ 0。 */
    daysUntil: number;
    inProgress: boolean;
    stale: boolean;
  } | null;
};

/** 整条的最终状态，组件据此选画法。 */
export type RatesStatus =
  | { kind: "hidden" } // 表是空的（还没回填）：整条不画，不是「暂不可用」
  | { kind: "unavailable"; reason: string } // 读失败：说一句，不是消失
  | { kind: "ok"; model: RatesModel };

/** 陈旧：没有数据，或最新一行距今超过阈值（日历日）。 */
export function isStale(latestDate: string | null, today: string, thresholdDays: number): boolean {
  if (latestDate === null) return true;
  return daysBetween(latestDate, today) > thresholdDays;
}

/** `加息 25bp` / `降息 50bp` / `调整（方向未知）`。`mixed` 不猜方向，也不写幅度。 */
export function changeText(kind: ChangeKind, bp: number | null): string {
  if (kind === "mixed" || bp === null) return "调整（上下沿不同步，方向未知）";
  return `${kind === "up" ? "加息" : "降息"} ${Math.abs(bp)}bp`;
}

/** 两个 `YYYY-MM-DD` 的会议日期，写成 `12-08 ~ 12-09`（同一天则只写一个）。 */
export function meetingText(m: MeetingRow): string {
  const md = (d: string) => d.slice(5);
  return m.start_date === m.end_date ? md(m.end_date) : `${md(m.start_date)} ~ ${md(m.end_date)}`;
}

/**
 * 每个期限取**最新与次新**两个点算当日变动。
 *
 * 用该期限**自己**的前一个点，而不是「全表的前一个日期」：某个期限在某天缺数据时
 * （财政部偶有空格），后者会拿一个不存在的点去相减。
 */
export function yieldCells(rows: YieldRow[], tenors: TenorLabel[]): YieldCell[] {
  const out: YieldCell[] = [];
  for (const t of tenors) {
    const mine = rows.filter((r) => r.tenor === t.key).sort((a, b) => b.date.localeCompare(a.date));
    const cur = mine[0];
    if (cur === undefined) continue; // 没有就不画这一格，**不用 0 或上一日冒充**
    const prev = mine[1];
    out.push({
      key: t.key,
      label: t.label,
      value: cur.yield_pct,
      changeBp: prev === undefined ? null : Math.round((cur.yield_pct - prev.yield_pct) * 100),
      date: cur.date,
    });
  }
  return out;
}

/** 10Y − 2Y（bp）。**只在两个期限是同一天的数据时才算**，否则是拿两天的数相减。 */
export function spread2s10s(cells: YieldCell[]): number | null {
  const y2 = cells.find((c) => c.key === "2y");
  const y10 = cells.find((c) => c.key === "10y");
  if (y2 === undefined || y10 === undefined || y2.date !== y10.date) return null;
  return Math.round((y10.value - y2.value) * 100);
}

export function buildModel(
  input: RatesInput,
  tenors: TenorLabel[],
  stale: StaleAfterDays,
  today: string,
): RatesModel {
  const { latest, lastChange, nextMeeting, earliest } = input;

  const range =
    latest === null
      ? null
      : {
          lower: latest.target_lower,
          upper: latest.target_upper,
          date: latest.date,
          stale: isStale(latest.date, today, stale.target_range),
        };

  const change =
    lastChange === null || lastChange.change_kind === null
      ? null
      : {
          text: changeText(lastChange.change_kind, lastChange.change_bp),
          kind: lastChange.change_kind,
          date: lastChange.date,
          daysAgo: daysBetween(lastChange.date, today),
        };

  // 没有目标区间数据就不说「无变动」：那不是「没变过」，是「不知道」。
  const noChangeSince = latest !== null && change === null ? (earliest ?? latest.date) : null;

  const cells = yieldCells(input.yields, tenors);
  const newest = cells.reduce<string | null>((m, c) => (m === null || c.date > m ? c.date : m), null);
  const yields =
    cells.length === 0
      ? null
      : {
          cells,
          spread2s10sBp: spread2s10s(cells),
          stale: isStale(newest, today, stale.yields),
        };

  const meeting =
    nextMeeting === null
      ? null
      : {
          text: meetingText(nextMeeting),
          // 数到**开始日**：「12-08 ~ 12-09 · 15 天后」说的是还有多久开会。
          // 数到结束日会让会议第一天显示「1 天后」，而会议正在进行。
          daysUntil: daysBetween(today, nextMeeting.start_date),
          inProgress: daysBetween(today, nextMeeting.start_date) <= 0,
          // 日程是暂定的、会被改期：它多久没被重新抓过，比它自己的日期更说明问题。
          stale: isStale(nextMeeting.fetched_at.slice(0, 10), today, stale.fomc),
        };

  return { range, lastChange: change, noChangeSince, yields, meeting };
}

/** 带符号的 bp：`+25bp` / `−13bp`（真减号，同 `lib/format.ts`），0 写 `0bp`。 */
export function signedBp(bp: number): string {
  if (bp > 0) return `+${bp}bp`;
  if (bp < 0) return `−${Math.abs(bp)}bp`;
  return "0bp";
}

/** 目标区间：`3.75–4.00%`。 */
export function rangeText(lower: number, upper: number): string {
  return `${lower.toFixed(2)}–${upper.toFixed(2)}%`;
}
