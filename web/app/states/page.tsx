/**
 * **四种非理想态的演示页**（§11 M7 验收：「四种状态可手动触发演示」）。
 *
 * 这几个态在真实数据上**恰恰很难触发** —— 空库只有第一次部署那一刻有，
 * 陈旧要等管道真的停掉，缺失要等某个标的真的被闸门剔除。
 * 而 §10.6 的原话是「这四种状态比 happy path 更能决定这个看板可不可信」：
 * 一个只能靠等事故来验收的东西，实际上没被验收过。
 *
 * **为什么是单独一页，而不是给仪表盘加个 `?preview=` 开关。**
 * 那个开关会让真实的仪表盘在某个 URL 下显示编造的数字，
 * 而 §10.4 整节在论证「诚实优先于漂亮」。这里的样例数据是假的，
 * 所以它住在一个自己的地址上、每一块都带着「演示」的标注，
 * 不可能被误当成行情。
 *
 * 这一页**不读数据库**，所以它在数据源不可达时照样能打开 ——
 * 恰好是最需要看懂这几个态的时刻。
 */
import { PoolTable, type Row } from "@/components/PoolTable";
import { EmptyDatabase, StaleBanner, UnavailableNotice, failClosed } from "@/components/States";
import { metrics as metricSpecs, universe } from "@/lib/config";
import { poolColumns } from "@/lib/columns";
import { parseSort, sortRows } from "@/lib/sort";
import type { MetricRow } from "@/lib/supabase";

export const revalidate = false;

/** 样例行。数字是编的，只为把渲染分支走到。 */
function sample(over: Partial<MetricRow>): MetricRow {
  return {
    symbol: "DEMO",
    date: "2000-01-01",
    extra: {},
    provisional_metrics: [],
    mom_20: 3.14,
    close_vs_ema60_pct: 2.5,
    rsi_14: 61.2,
    beta: 1.18,
    alpha_annual: 7.4,
    alpha_t_stat: 1.9,
    days_to_next_earnings: 12,
    ...over,
  };
}

const spark = Array.from({ length: 60 }, (_, i) => 100 + Math.sin(i / 6) * 6 + i * 0.12);

const DEMO_ROWS: Row[] = [
  {
    symbol: "DEMO-A",
    name: "一切正常的一行（对照）",
    metrics: sample({}),
    rank: null,
    spark,
    latestClose: 187.42,
    closeIsAdjusted: false,
  },
  {
    // §10.6 之三：该行显示「数据缺失」**而非空白** —— 空白会被读成「这个值是 0」。
    symbol: "DEMO-B",
    name: "被闸门剔除 / 落后的标的",
    metrics: null,
    rank: null,
    spark: [],
    // **这个价格是故意填的，它不会被渲染出来**（整行走 colSpan 的「数据缺失」分支）。
    // 它在这里是为了让排序的一条规则可以被肉眼验收：`metrics === null` 但
    // `latestClose` 有值的行，按「最新价」降序时**必须仍然沉底**。
    // 这个组合在真实管道里出得来（当天有价格、却没有 metrics 行），而 `sortValue`
    // 一度把 `close` 取在缺失守卫之前 —— 那样这一行会顶在降序榜第一位，
    // 屏幕上却写着「数据缺失」。没有这个值，那条规则在演示页上是看不见的。
    latestClose: 92.3,
    closeIsAdjusted: false,
  },
  {
    // §10.6 之四：预热不足**出值但标灰**，与 NULL 是两回事（§3.3 软闸门）。
    symbol: "DEMO-C",
    name: "新加入、历史不够长的标的",
    metrics: sample({
      provisional_metrics: ["rsi_14", "close_vs_ema60_pct", "alpha_beta_126"],
      rsi_14: 48.9,
    }),
    rank: null,
    spark: spark.slice(0, 22),
    latestClose: 41.08,
    closeIsAdjusted: true,
  },
  {
    // **三强 + 数据缺失**：这个组合真的会出现 —— `rank` 来自 `v_strength_enriched`，
    // 与 `metrics` 是两条独立的查询，`page.tsx` 只用「价格是不是当天的」闸住了
    // `metrics`，没闸住 `rank`。于是一只价格落后、但强度视图里仍在榜的标的
    // 会同时拿到 `in_top_n` 和 `metrics === null`。
    // 它是唯一能看见 `.top3 .frozen-note`（缺失文案在三强行上的琥珀重放）的路径 ——
    // 靠等这次事故来验收，等于没验收过（§10.6）。
    symbol: "DEMO-D",
    name: "仍在榜、但价格落后的标的",
    metrics: null,
    rank: {
      date: "2026-09-18",
      symbol: "DEMO-D",
      rank: 2,
      score: null,
      in_top_n: true,
      delta_to_next: null,
      delta_to_median: null,
      days_in_top_n: null,
      rank_delta_1d: null,
    },
    spark: [],
    latestClose: null,
    closeIsAdjusted: false,
  },
];

function Block({ title, note, children }: { title: string; note: string; children: React.ReactNode }) {
  return (
    <section className="mt-10">
      <h2 className="text-sm font-medium text-zinc-200">{title}</h2>
      <p className="mt-1 text-xs leading-relaxed text-zinc-500">{note}</p>
      <div className="mt-3 rounded border border-dashed border-ink-700 p-3">{children}</div>
    </section>
  );
}

export default async function States({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  // `?throw=1` 走**真的**失败路径：抛 DataUnavailableError → HTTP 500 → error.tsx。
  // 下面那一块只是把同一段文案静态渲染出来，看得见但拿不到状态码；
  // 要验「状态码也对」，得走这条。
  if (params.throw) failClosed("演示页的手动触发（不是真故障）", 503);

  const extraColumns = metricSpecs
    .filter((m) => m.core !== true)
    .map((m) => ({ id: m.id, label: m.display.label, format: m.display.format }));

  // 演示页的表格也得能排 —— 它和首页是同一个组件，排序坏了要在这里看得见。
  const sort = parseSort(params, poolColumns(extraColumns));
  const demoRows = sortRows(DEMO_ROWS, sort);

  return (
    <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
      <header>
        <h1 className="text-lg font-medium text-zinc-100">非理想态演示</h1>
        <p className="mt-2 text-sm text-zinc-400">
          这一页的数字 <strong className="text-zinc-300">全部是编造的</strong>，标的叫 <code className="text-zinc-300">DEMO-*</code>，
          与任何真实行情无关。它的用途是让 §10.6 的四种状态可以随时看到，
          而不必等一次真实事故。
        </p>
        <p className="mt-2 text-xs text-zinc-500">
          <a href="/" className="text-zinc-400 underline underline-offset-2">← 回到仪表盘</a>
        </p>
      </header>

      <Block
        title="① 空数据（首次部署）"
        note="数据库已就绪但还没回填过。不是白屏，也不是一张空表格 —— 空表格会被读成「今天所有值都缺」。"
      >
        <EmptyDatabase />
      </Block>

      <Block
        title="② 数据陈旧（>1 个交易日未更新）"
        note="dead-man 场景。黄条要说清楚落后了多少个交易日 —— 落后 2 天和落后 40 天是完全不同的两件事，而后者才是真正要说清楚的那次。"
      >
        <StaleBanner asOf="2026-08-14" sessionsBehind={27} />
      </Block>

      <Block
        title="③ 部分标的缺失 ＋ ④ 预热不足的灰标"
        note="这张表可以点表头排序，所以下面按代号指行、不按第几行。DEMO-B 是被闸门剔除的标的：显示「数据缺失」而不是空白。DEMO-C 历史不够长：数值照出，但打灰，并在 title 里说明原因 —— 出值但标灰，与 NULL 是两回事。DEMO-D 同样缺失、但仍在三强榜上（rank 与 metrics 是两条独立查询，这个组合真的会出现）—— 横着滚，那句文案会跟着标的列一起钉住，琥珀底纹也不掉。排任一指标列，DEMO-B 与 DEMO-D 都会沉到最下面，两个方向都是 —— 注意 DEMO-B 在数据里其实有最新价，只是整行缺 metrics 所以不显示，按「最新价」排它照样沉底，那正是这一格要演示的东西。"
      >
        <PoolTable
          rows={demoRows}
          benchmark={universe.benchmark}
          extraColumns={extraColumns}
          sort={sort}
          path="/states"
          params={params}
        />
      </Block>

      <Block
        title="⑤ 数据源不可达（§12 #9 第 4 条）"
        note="授权改错或数据库不可达时的样子。下面是静态渲染的同一段文案；要连状态码一起验，点那个链接走真的失败路径。"
      >
        <UnavailableNotice />
        <p className="mt-4 text-xs">
          <a href="/states?throw=1" className="text-accent underline underline-offset-2">
            触发真实失败路径（会返回 HTTP 500）
          </a>
        </p>
      </Block>
    </main>
  );
}
