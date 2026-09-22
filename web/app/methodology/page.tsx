/**
 * 方法论页（§10.2 / §1.3）：公式逐条列出 + 与 low-buy 的差异 + 数据源与局限。
 *
 * 公式清单**从 config 生成** —— §6.1 说前端构建期直读 YAML，
 * 而把公式再手抄一遍就是 §6.1.1 警告的「两个要对齐的地方」。
 */
import { app, metrics, strength, universe } from "@/lib/config";

export const revalidate = 3600;

export default function Methodology() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-10 text-sm leading-relaxed">
      <a href="/" className="text-xs text-zinc-500 underline underline-offset-2">← 回到仪表盘</a>
      <h1 className="mt-4 text-xl font-medium text-zinc-100">方法论与口径</h1>

      <h2 className="mt-8 text-base font-medium text-zinc-200">价格口径</h2>
      <p className="mt-2 text-zinc-400">
        <strong className="text-zinc-300">所有计算一律使用复权收盘价</strong>（<code>adj_close</code>）。
        唯一的例外是表格里的「最新价」—— 它显示未复权收盘价，因为你肉眼对照行情软件时看的是它。
        备源（Stooq）只提供已复权的价格，那种行的最新价会标一个「复权」角标。
      </p>
      <p className="mt-2 text-zinc-400">
        复权因子会被数据供应商<strong className="text-zinc-300">追溯改写</strong>：每一次分红或拆股都会重写全部历史。
        所以本站每天重抓并重写整个 {app.lookback_bars} 根 bar 的窗口，
        而不是只追加当天一行 —— 否则跨接缝的计算会悄悄错一个股息率的量级。
      </p>

      <h2 className="mt-8 text-base font-medium text-zinc-200">指标定义</h2>
      <ul className="mt-2 space-y-3">
        {metrics.map((m) => (
          <li key={m.id} className="rounded border border-ink-800 p-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-medium text-zinc-200">{m.display.label}</span>
              <code className="text-xs text-zinc-500">{m.id}</code>
            </div>
            <div className="mt-1 text-xs text-zinc-500">
              参数：{JSON.stringify(m.params ?? {})}
              {m.min_bars ? <> · 少于 {m.min_bars} 根不出值</> : null}
              {m.provisional_below ? <> · 少于 {m.provisional_below} 根标灰</> : null}
            </div>
            {(m.derived ?? []).map((d) => (
              <div key={d.id} className="mt-2 text-xs text-zinc-400">
                <span className="text-zinc-300">{d.display.label}</span>
                {" = "}
                <code className="text-zinc-500">{d.expr}</code>
              </div>
            ))}
          </li>
        ))}
      </ul>

      <h2 className="mt-8 text-base font-medium text-zinc-200">「今日三强」的尺子</h2>
      <p className="mt-2 text-zinc-400">
        排序分是 <code className="text-zinc-300">{strength.score_metric}</code>，
        排名池是 {strength.rank_pool === "all" ? "全部标的" : "个股（不含 ETF）"}，
        基准是 {universe.benchmark}。每天存<strong className="text-zinc-300">全部名次</strong>，页面只显示前 {strength.top_n} 名。
      </p>
      <p className="mt-2 text-zinc-400">
        <strong className="text-zinc-300">榜单不是本站的主题</strong> ——
        它只是加在这批标的上的又一个变量。全池表格才是主体。
      </p>

      <h2 className="mt-8 text-base font-medium text-zinc-200">两条诚实标注</h2>
      <p className="mt-2 text-zinc-400">
        <strong className="text-zinc-300">预热不足</strong>的数值会被标灰并加虚线：
        窗口里的历史不够长时，指标已经出值但还不稳定。
        <strong className="text-zinc-300">样本不足</strong>则一律显示破折号 ——
        本站绝不用 0 或上一日的值冒充一个缺失值。
      </p>
      <p className="mt-2 text-zinc-400">
        α 的 <code>|t| &lt; 2</code> 时会<strong className="text-zinc-300">降透明度</strong> —— 它与 0
        区分不开；悬停可看 <code>t</code> 的具体值。
        一个不显著的 α 在视觉上不该和一个显著的 α 长得一样。
      </p>

      <h2 className="mt-8 text-base font-medium text-zinc-200">表格排序的口径</h2>
      <p className="mt-2 text-zinc-400">
        点表头可以按任一列排序。两列的排序键不是它字面上画的东西，写在这里：
        <strong className="text-zinc-300">60 日走势</strong>按那条线的首末涨跌排；
        <strong className="text-zinc-300">事件</strong>按芯片上那个天数排，首次点击是最近的在前。
      </p>
      <p className="mt-2 text-zinc-400">
        <strong className="text-zinc-300">没有值的行永远沉到最下面，不随升降序翻转。</strong>
        否则降序时一排破折号会顶在最上面 —— 那读起来就是「这几只最高」，
        而它们其实是没有值。同值时按标的字母兜底，所以同一个链接永远给同一个顺序。
      </p>

      <h2 className="mt-8 text-base font-medium text-zinc-200">那道口令</h2>
      <p className="mt-2 text-zinc-400">
        站点入口的口令是<strong className="text-zinc-300">象征性</strong>的，<strong className="text-zinc-300">不是安全边界</strong>。
        它买到的是两件具体的事：搜索引擎索引不到（数据源的条款禁止再分发，
        一个不可被检索的私人页面与一个公开发布的数据站不是一回事），以及爬虫刷不到免费额度。
        <strong className="text-zinc-300">不要因此以为这里的数据是私密的。</strong>
      </p>

      <h2 className="mt-8 text-base font-medium text-zinc-200">已知局限</h2>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-zinc-400">
        <li>财报有盘前/盘后之分，而本站以「日」为粒度，不建模具体时刻。</li>
        <li>事件距离与顶部陈旧黄条的触发阈值用<strong className="text-zinc-300">日历日</strong>，其余所有窗口用<strong className="text-zinc-300">交易日</strong> —— 黄条亮起后报的落后天数本身仍是交易日。</li>
        <li>「下一次」的事件日期在公司正式确认前是预告值，会移动；这类值加虚线标注。</li>
        <li>数据来自免费源，仅供研究；本站不构成任何投资建议。</li>
      </ul>

      <p className="mt-8 text-xs text-zinc-600">{app.site.disclaimer}</p>
    </main>
  );
}
