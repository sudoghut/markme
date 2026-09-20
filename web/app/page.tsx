/**
 * 单页仪表盘（§10.2）。数据读取在**服务端**，页面走 ISR（§10.1）。
 *
 * `revalidate = 3600` + 按需重验证，**不是** 300：对一个一天只变一次的
 * 数据源，300 秒意味着每天 288 次重新生成，与「出站流量接近零」自相矛盾，
 * 而且烧的是 Vercel 的 ISR 额度 —— 在这个项目里它比 Supabase 的额度
 * **更可能先触顶**。
 */
import { PoolTable, type Row } from "@/components/PoolTable";
import { TopThree } from "@/components/TopThree";
import { EmptyDatabase, StaleBanner, Unavailable } from "@/components/States";
import { app, metrics as metricSpecs, strength, universe } from "@/lib/config";
import { rest, type MetricRow, type PriceRow, type StrengthRow, type SymbolRow } from "@/lib/supabase";

export const revalidate = 3600;

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000);
}

export default async function Dashboard() {
  const R = app.revalidate_seconds;

  const symbols = await rest<SymbolRow>("symbols?select=symbol,name,type,enabled&enabled=eq.true&order=symbol", R);
  if (!symbols.ok) return <Unavailable reason={symbols.reason} status={symbols.status} />;

  const latest = await rest<{ date: string }>("metrics_daily?select=date&order=date.desc&limit=1", R);
  if (!latest.ok) return <Unavailable reason={latest.reason} status={latest.status} />;
  const asOf = latest.rows[0]?.date;
  if (!asOf) return <EmptyDatabase />;

  const [metrics, ranks, prices, sessions] = await Promise.all([
    rest<MetricRow>(`metrics_daily?select=*&date=eq.${asOf}`, R),
    rest<StrengthRow>(`v_strength_enriched?select=*&date=eq.${asOf}&order=rank`, R),
    rest<PriceRow>(
      `prices_daily?select=symbol,date,close,adj_close&date=lte.${asOf}&order=date.desc&limit=${app.sparkline_bars * 20}`,
      R,
    ),
    rest<{ date: string }>(`trading_sessions?select=date&date=lte.${todayISO()}&order=date.desc&limit=5`, R),
  ]);
  if (!metrics.ok) return <Unavailable reason={metrics.reason} status={metrics.status} />;

  const metricBySymbol = new Map(metrics.rows.map((m) => [m.symbol, m]));
  const rankBySymbol = new Map((ranks.ok ? ranks.rows : []).map((r) => [r.symbol, r]));

  const sparkBySymbol = new Map<string, number[]>();
  const latestPrice = new Map<string, PriceRow>();
  if (prices.ok) {
    for (const p of prices.rows) {
      const arr = sparkBySymbol.get(p.symbol) ?? [];
      if (arr.length < app.sparkline_bars) arr.push(p.adj_close);
      sparkBySymbol.set(p.symbol, arr);
      if (!latestPrice.has(p.symbol)) latestPrice.set(p.symbol, p);
    }
  }

  const rows: Row[] = symbols.rows.map((s) => {
    const p = latestPrice.get(s.symbol);
    // **只有当天的价格才算「最新价」。**
    //
    // 价格是按 `date <= asOf` 取的最近一行，所以一个被闸门 3 判为落后、
    // 或被闸门 4 剔除的标的，会拿回**昨天**那一行 —— 然后被渲染在
    // 「数据截至 <今天>」的表格里。那是一个静默的错数：页面在说
    // 「这是今天的收盘价」，而它不是。
    //
    // 管道对这些标的写的是一行全 NULL 的指标（§7.2 闸门 3），
    // 所以这里跟着把整行按「数据缺失」处理，与 §10.6 的那一条一致。
    const fresh = p !== undefined && p.date === asOf;
    const m = metricBySymbol.get(s.symbol) ?? null;
    return {
      symbol: s.symbol,
      name: s.name,
      metrics: fresh ? m : null,
      rank: rankBySymbol.get(s.symbol) ?? null,
      // 倒序取回来的，画图要正序。
      spark: (sparkBySymbol.get(s.symbol) ?? []).slice().reverse(),
      // §3.0 规则 3：Stooq 行的 close 是 NULL → 显示复权价并加角标。
      latestClose: fresh ? (p.close ?? p.adj_close) : null,
      closeIsAdjusted: fresh ? p.close === null : false,
    };
  });

  // 顶部黄条：>1 个交易日未更新（§10.6）。
  //
  // `findIndex` 给的是「往回数第几个 session」：最新那个是 0，
  // 上一个是 1。而「落后几个交易日」的自然读法是：最新 = 1 个交易日（不落后）。
  // 所以是 index + 1，且 index === 0 时结果为 1 —— StaleBanner 在 <= 1 时不显示。
  const idx = sessions.ok ? sessions.rows.findIndex((s) => s.date === asOf) : 0;
  const behind = idx < 0 ? 99 : idx + 1;
  const top3 = (ranks.ok ? ranks.rows : []).filter((r) => r.in_top_n).slice(0, strength.top_n);

  return (
    <>
      <StaleBanner asOf={asOf} sessionsBehind={behind} />
      <main className="mx-auto max-w-6xl px-4 py-6 sm:px-6">
        <header className="flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h1 className="text-lg font-medium text-zinc-100">{app.site.title}</h1>
            <p className="text-xs text-zinc-500">{app.site.subtitle}</p>
          </div>
          <p className="num text-xs text-zinc-500">数据截至 {asOf} 收盘</p>
        </header>

        <section className="mt-6" aria-labelledby="pool">
          <h2 id="pool" className="sr-only">全池速览</h2>
          <PoolTable
            rows={rows}
            benchmark={universe.benchmark}
            extraColumns={metricSpecs
              .filter((m) => m.core !== true)
              .map((m) => ({ id: m.id, label: m.display.label, format: m.display.format }))}
          />
        </section>

        <TopThree
          rows={top3}
          all={ranks.ok ? ranks.rows : []}
          scoreMetric={strength.score_metric}
          squeakK={strength.squeak_k ?? 0.1}
          topN={strength.top_n}
        />

        <footer className="mt-10 border-t border-ink-800 pt-4 text-xs leading-relaxed text-zinc-600">
          <a href="/methodology" className="text-zinc-400 underline underline-offset-2">方法论与口径</a>
          <p className="mt-2">{app.site.disclaimer}</p>
        </footer>
      </main>
    </>
  );
}

/**
 * **交易所当地日期，不是 UTC 日期。**
 *
 * 用 UTC 时，从 UTC 午夜到下一个美股 session 之间的那几个小时里，
 * 「今天」会提前跨到下一天，于是查询把一个**预填的未来 session** 也算进来，
 * 顶部黄条就会谎报「已落后 2 个交易日」—— 一条在每天固定时段自动出现、
 * 而数据其实完全正常的告警。
 */
function todayISO(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}
