/**
 * 单页仪表盘（§10.2）。数据读取在**服务端**，页面走 ISR（§10.1）。
 *
 * `revalidate = 3600` + 按需重验证，**不是** 300：对一个一天只变一次的
 * 数据源，300 秒意味着每天 288 次重新生成，与「出站流量接近零」自相矛盾，
 * 而且烧的是 Vercel 的 ISR 额度 —— 在这个项目里它比 Supabase 的额度
 * **更可能先触顶**。
 */
import { PoolTable, type Row } from "@/components/PoolTable";
import { RepoLink } from "@/components/RepoLink";
import { TopThree } from "@/components/TopThree";
import { EmptyDatabase, StaleBanner, failClosed } from "@/components/States";
import { app, metrics as metricSpecs, strength, universe } from "@/lib/config";
import { poolColumns } from "@/lib/columns";
import { parseSort, sortRows } from "@/lib/sort";
import { MARKET_TZ_LABEL, daysBetween, todayISO } from "@/lib/market";
import { rest, type MetricRow, type PriceRow, type StrengthRow, type SymbolRow } from "@/lib/supabase";

/**
 * **页面按请求渲染，缓存放在 `fetch` 那一层。**
 *
 * 初版是页面级 ISR（`export const revalidate = 3600`）。实测证明那样在最常见的
 * 故障里比什么都不做更糟：把数据源杀掉、ISR 条目是热的，然后连打三次 ——
 * 三次都是 **200 + 故障前的价格 + 没有陈旧黄条**。Next 的 response cache 在条目
 * 过期且非 on-demand 时先把**旧页面**返给访客再去后台重生成，后台那次一抛就被
 * 吞成一行 `console.error`，旧条目留在缓存里继续发。**永远不会自愈** ——
 * 陈旧判据是生成那一刻算出来的，于是黄条被永久藏起来，
 * 页面会一直宣称自己是最新的。
 *
 * 改成按请求渲染之后：
 *
 * - **陈旧天数每次重算**，不会被冻住。`sessions` 的查询 URL 里带着
 *   当天日期，所以每跨一天缓存键就变，不可能永远命中旧答案；
 *   日历日那一侧根本不经过缓存，每次渲染现算。
 * - 一份缓存都没有时（冷启动、部署后第一次访问）照样 `failClosed` → **500**。
 * - **出站流量仍然接近零**（§10.1）：每个 `fetch` 都带 `next: { revalidate }`，
 *   命中的是 **Data Cache**。实测一次构建打数据源 **0 次**，
 *   连续 5 次请求累计只打 **6 次**（就是首次那 6 个读）。页面动态了，数据没有。
 * - 按需重验证（§10.1）照旧：`revalidatePath("/")` 清的就是这层 Data Cache。
 *
 * **但状态码这件事不在这里。** Data Cache 同样是 stale-while-revalidate：
 * 实测让数据源改口 111.11 → 222.22，过期后第一次请求拿到的仍是 111.11，
 * 把源杀掉再过期拿到的是 222.22 —— 错误一样被吞，一样是 200。
 * 而那其实是**对的**：缓存里的数字是真的，「数据截至 <date>」也是真的，
 * 页面这一刻并没有撒谎，硬改成 500 才是撒谎。
 * 会撒谎的是「监控看到一串 200 就以为管道活着」，所以状态码归给
 * `/api/health` —— 一个不带缓存、能真正设状态码的 Route Handler。
 *
 * 代价是丢掉整页的 CDN 边缘缓存。对一个加了密码门、单人使用的看板，
 * 换来「陈旧黄条永远说真话」是划算的。
 */
export const dynamic = "force-dynamic";
// `force-dynamic` 历史上会把 fetch 连带设成 no-store。显式声明，
// 免得上游默认值一变，出站流量就从「接近零」变成「每次请求全量」。
export const fetchCache = "default-cache";

export default async function Dashboard({
  searchParams,
}: {
  // 排序状态住在 URL 里（`lib/sort.ts`）。页面本来就是 `force-dynamic`，
  // 读 `searchParams` 不改变任何缓存语义 —— 数据仍然走 Data Cache。
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const R = app.revalidate_seconds;
  const params = await searchParams;
  // 取一次就固定下来：查询与落后天数必须用**同一个**「今天」，
  // 否则一次跨午夜的渲染会让两者差一天。
  const today = todayISO();

  const symbols = await rest<SymbolRow>("symbols?select=symbol,name,type,enabled&enabled=eq.true&order=symbol", R);
  if (!symbols.ok) failClosed(symbols.reason, symbols.status);

  const latest = await rest<{ date: string }>("metrics_daily?select=date&order=date.desc&limit=1", R);
  if (!latest.ok) failClosed(latest.reason, latest.status);
  const asOf = latest.rows[0]?.date;
  if (!asOf) return <EmptyDatabase />;

  const [metrics, ranks, prices, sessions] = await Promise.all([
    rest<MetricRow>(`metrics_daily?select=*&date=eq.${asOf}`, R),
    rest<StrengthRow>(`v_strength_enriched?select=*&date=eq.${asOf}&order=rank`, R),
    rest<PriceRow>(
      `prices_daily?select=symbol,date,close,adj_close&date=lte.${asOf}&order=date.desc&limit=${app.sparkline_bars * 20}`,
      R,
    ),
    // **数 asOf 之后还有几个 session**，而不是取最近 N 个再去里面找。
    // 取 5 个时，只要落后超过 4 个交易日就找不到 asOf，于是显示一个
    // 编造的数字 —— 而「落后很多」恰恰是 dead-man 场景，
    // 是这条黄条唯一真正要说清楚的时刻。
    rest<{ date: string }>(
      `trading_sessions?select=date&date=gt.${asOf}&date=lte.${today}&order=date.asc&limit=500`,
      R,
    ),
  ]);

  // **四个读都必须 fail closed。**
  //
  // 之前只有 metrics 失败会走 Unavailable：ranks 失败 → 三强横条空着、
  // 表格里的 ★ 全没了；prices 失败 → 每一行都「数据缺失」；
  // sessions 失败 → 陈旧黄条**不显示**。
  // 于是一次瞬时故障会发布一个 HTTP 200、看起来完全正常、
  // 而内容是错的或残缺的页面 —— 那正是 §10.6 要防的白屏的另一种形态：
  // 不是白屏，是**看起来没事**。
  // 逐个写，不用循环 —— TypeScript 的收窄跟不过异构数组，
  // 而这四行的全部价值就在于每一行都真的挡住了一种失败。
  if (!metrics.ok) failClosed(metrics.reason, metrics.status);
  if (!ranks.ok) failClosed(ranks.reason, ranks.status);
  if (!prices.ok) failClosed(prices.reason, prices.status);
  if (!sessions.ok) failClosed(sessions.reason, sessions.status);

  const metricBySymbol = new Map(metrics.rows.map((m) => [m.symbol, m]));
  const rankBySymbol = new Map(ranks.rows.map((r) => [r.symbol, r]));

  const sparkBySymbol = new Map<string, number[]>();
  const latestPrice = new Map<string, PriceRow>();
  {
    for (const p of prices.rows) {
      const arr = sparkBySymbol.get(p.symbol) ?? [];
      if (arr.length < app.sparkline_bars) arr.push(p.adj_close);
      sparkBySymbol.set(p.symbol, arr);
      if (!latestPrice.has(p.symbol)) latestPrice.set(p.symbol, p);
    }
  }

  const unsorted: Row[] = symbols.rows.map((s) => {
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

  // 排序在**取完数之后、渲染之前**做一次。`symbols` 已经是按 symbol 升序取回来的，
  // 所以默认排序（`DEFAULT_SORT`）排出来与不排完全一致 —— 不带参数的页面没有变化。
  const extraColumns = metricSpecs
    .filter((m) => m.core !== true)
    .map((m) => ({ id: m.id, label: m.display.label, format: m.display.format }));
  const sort = parseSort(params, poolColumns(extraColumns));
  const rows = sortRows(unsorted, sort);

  // 顶部黄条（§10.6）。亮不亮只看 `daysBehind`，理由见 `lib/market.ts`
  // 的 `STALE_AFTER_DAYS`。
  //
  // `sessions` 装的是 asOf 之后、今天（含）以内的 session，行数就是没有数据的
  // 交易日数。**以前这里是 `+ 1`**，把「一行都没有 = 完全最新」记成 1，
  // 整条刻度系统性高报一天；`invariants.sql` 的「管道不得静默停摆」数的
  // 正是不带 `+1` 的同一个量，所以去掉之后页面与自动断言才在同一把尺子上。
  //
  // 仍有一处不精确：查询是 `date <= today`，所以交易日当天**收盘前**，
  // 今天这根尚未收盘的 session 已经被算进去了，盘中会高报 1。
  // 比原来「永远高报 1」好，但不是零误差。
  const sessionsBehind = sessions.rows.length;
  const daysBehind = daysBetween(asOf, today);
  const top3 = ranks.rows.filter((r) => r.in_top_n).slice(0, strength.top_n);

  return (
    <>
      <StaleBanner asOf={asOf} sessionsBehind={sessionsBehind} daysBehind={daysBehind} />
      <main className="mx-auto max-w-6xl px-4 py-6 sm:px-6">
        <header className="flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h1 className="text-lg font-medium text-zinc-100">{app.site.title}</h1>
            <p className="text-xs text-zinc-500">{app.site.subtitle}</p>
          </div>
          <p className="num text-xs text-zinc-500">
            数据截至 {asOf}（{MARKET_TZ_LABEL}）收盘
          </p>
        </header>

        <section className="mt-6" aria-labelledby="pool">
          <h2 id="pool" className="sr-only">全池速览</h2>
          <PoolTable
            rows={rows}
            benchmark={universe.benchmark}
            extraColumns={extraColumns}
            sort={sort}
            path="/"
            params={params}
          />
        </section>

        <TopThree
          rows={top3}
          all={ranks.rows}
          scoreMetric={strength.score_metric}
          squeakK={strength.squeak_k ?? 0.1}
          topN={strength.top_n}
        />

        <footer className="mt-10 border-t border-ink-800 pt-4 text-xs leading-relaxed text-zinc-600">
          <a href="/methodology" className="text-zinc-400 underline underline-offset-2">方法论与口径</a>
          <span className="mx-2 text-zinc-700">·</span>
          <a href="/states" className="text-zinc-400 underline underline-offset-2">非理想态演示</a>
          <span className="mx-2 text-zinc-700">·</span>
          <RepoLink />
          <p className="mt-2">{app.site.disclaimer}</p>
        </footer>
      </main>
    </>
  );
}
