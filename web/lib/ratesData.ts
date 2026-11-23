/**
 * 读利率窄条要用的三张表（五个读），折成一个 `RatesStatus`（docs/add-rate-policy.md §4.5）。
 *
 * **这个函数永远不抛、永远不 `failClosed`** —— 与首页其余读取刻意相反。
 * 窄条是装饰性的：表缺失（迁移还没跑）、读失败、表为空，都只能让**窄条自己**变成
 * 「暂不可用」或隐藏，页面其余部分照常。`rest()` 本身不抛，它把失败折叠成 `{ ok: false }`，
 * 这里再把它折叠成一个状态。
 *
 * 五个读（都带 `revalidate`，命中的是 Data Cache，出站流量仍然接近零，§10.1）：
 *
 * 1. 最新一行目标区间；
 * 2. 最近一次 `change_kind` 非空的行（上次加息 / 降息）；
 * 3. 库里**最早**的一行目标区间日期（「自 X 起无变动」的 X）；
 * 4. 国债收益率最近若干行（日期降序，每个期限至少够取两个点）；
 * 5. `end_date > 今天` 的最早一场 FOMC 会议。
 */
import { macro } from "./config";
import {
  buildModel,
  type ChangeRow,
  type MeetingRow,
  type RatePolicyRow,
  type RatesStatus,
  type YieldRow,
} from "./rates";
import { rest } from "./supabase";

const num = (v: unknown): number => Number(v);

/**
 * 对外的入口：**保证不抛**。页面在最前面就发起这个读、最后才 await，
 * 中途若 `failClosed` 提前结束渲染，一个没人 await 的 rejected promise 会变成未处理的拒绝。
 * `rest()` 自己不抛，但下面的折叠逻辑（数值转换、`buildModel`）是我们的代码 ——
 * 兜底放在这里，而不是相信它们永远不出错。
 */
export async function loadRates(revalidate: number, today: string): Promise<RatesStatus> {
  try {
    return await load(revalidate, today);
  } catch (e) {
    return { kind: "unavailable", reason: `利率数据处理出错：${(e as Error).message}` };
  }
}

async function load(revalidate: number, today: string): Promise<RatesStatus> {
  const tenors = macro.treasury_tenors;
  const [latest, change, earliest, yields, meeting] = await Promise.all([
    rest<RatePolicyRow>(
      "rate_policy_daily?select=date,target_lower,target_upper,effr,change_kind,change_bp&order=date.desc&limit=1",
      revalidate,
    ),
    rest<ChangeRow>(
      "rate_policy_daily?select=date,change_kind,change_bp&change_kind=not.is.null&order=date.desc&limit=1",
      revalidate,
    ),
    // 库里实际最早的一天：「自 X 起无变动」的 X。
    rest<{ date: string }>("rate_policy_daily?select=date&order=date.asc&limit=1", revalidate),
    // 每个期限取最新与次新两个点；某期限在某天缺格时多取几天兜底。
    rest<YieldRow>(
      `treasury_yields?select=date,tenor,yield_pct&order=date.desc&limit=${tenors.length * 6}`,
      revalidate,
    ),
    rest<MeetingRow>(
      `fomc_meetings?select=start_date,end_date,fetched_at&end_date=gt.${today}&order=end_date.asc&limit=1`,
      revalidate,
    ),
  ]);

  for (const r of [latest, change, earliest, yields, meeting]) {
    if (!r.ok) return { kind: "unavailable", reason: r.reason };
  }
  // 上面的循环收窄不了异构元组，逐个再判一次（与 `app/page.tsx` 的写法同一个理由）。
  if (!latest.ok || !change.ok || !earliest.ok || !yields.ok || !meeting.ok) {
    return { kind: "unavailable", reason: "读取失败" };
  }

  const latestRow = latest.rows[0] ?? null;
  // 三张表**全空** = 宏观管道还没回填过：整条不画。只要有一张有数据就画有的那部分。
  if (latestRow === null && yields.rows.length === 0 && meeting.rows.length === 0) {
    return { kind: "hidden" };
  }

  const model = buildModel(
    {
      latest:
        latestRow === null
          ? null
          : {
              ...latestRow,
              target_lower: num(latestRow.target_lower),
              target_upper: num(latestRow.target_upper),
              effr: num(latestRow.effr),
              change_bp: latestRow.change_bp === null ? null : num(latestRow.change_bp),
            },
      lastChange:
        change.rows[0] === undefined
          ? null
          : {
              ...change.rows[0],
              change_bp: change.rows[0].change_bp === null ? null : num(change.rows[0].change_bp),
            },
      earliest: earliest.rows[0]?.date ?? null,
      yields: yields.rows.map((r) => ({ ...r, yield_pct: num(r.yield_pct) })),
      nextMeeting: meeting.rows[0] ?? null,
    },
    tenors,
    macro.stale_after_days,
    today,
  );
  return { kind: "ok", model };
}
