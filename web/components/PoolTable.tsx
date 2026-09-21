/**
 * ★ 主体：全池速览（§10.2）。
 *
 * 布局在设计阶段被改过一次：初稿把「今日三强」做成顶部三张大卡片，
 * 按 §4 开头的定位修正 —— **本项目不是榜单项目**，全池表格才是主体，
 * 三强降为表格下方的一条紧凑横条，并在表格里用一个轻量标记把那三行标出来，
 * 而不是把同样的信息呈现两遍。
 *
 * 无障碍：真 `<table>` + `<caption>` + `scope`（§10.4）。
 */
import { Alpha, BetaScale, DivergingBar, EventChip, Missing, RsiBar, Signed, Sparkline } from "./widgets";
import { isProvisional, metricValue } from "@/lib/metricValue";
import { nextEvent } from "@/lib/nextEvent";
import { poolColumns, type ExtraColumn, type PoolColumn } from "@/lib/columns";
import { defaultDir, nextSort, sortHref, type SortState } from "@/lib/sort";
import { EM_DASH, fmt, isMissing } from "@/lib/format";
import type { MetricRow, PriceRow, StrengthRow, SymbolRow } from "@/lib/supabase";

export type Row = {
  symbol: string;
  name: string;
  metrics: MetricRow | null;
  rank: StrengthRow | null;
  spark: number[];
  latestClose: number | null;
  closeIsAdjusted: boolean;
};

/**
 * 一个指标单元格。
 *
 * **内容居中，但数字本身仍然右对齐。**
 *
 * 初版整列 `text-right`，于是「数字 + 条」那三列（距 EMA60 / RSI / β）的表头
 * 被钉在整块的最右边，而数字在最左边 —— 实测表头偏离它所标注的数字
 * 6.75–7.5rem，看上去根本不在这一列的中间。
 *
 * 光把表头改成居中还不够：那样表头会浮在数字和条之间的空处。
 * 所以两头一起改 —— 单元格内容整块居中，表头也居中，于是表头正对着内容块的中心。
 *
 * 而纯数字列不能就这么居中：**居中会让数位参差**，一列数字就没法竖着扫了。
 * 所以给它们套一个**定宽 + 右对齐**的内层（`numeric`），
 * 内层右对齐保住数位对齐，外层居中保住表头对得上。两个都要。
 */
function Cell({
  row,
  id,
  children,
  numeric,
}: {
  row: MetricRow | null;
  id: string;
  children: React.ReactNode;
  /** 纯数字列：给一个定宽，内部右对齐。留空表示内容自己是定宽块（如「数字 + 条」）。 */
  numeric?: string;
}) {
  // §3.3 软闸门 → §10.4 灰标。预热不足**出值但标灰**，与 NULL 是两回事。
  const prov = row ? isProvisional(row, id) : false;
  return (
    <td className={`px-3 py-2 text-center ${prov ? "provisional" : ""}`} title={prov ? "预热不足，数值尚不稳定" : undefined}>
      {numeric ? <span className={`inline-block ${numeric} text-right`}>{children}</span> : children}
    </td>
  );
}

/**
 * 一个可排序的表头（§10.2「17 行**可排序**表格」）。
 *
 * **它是链接，不是按钮**，因为排序状态住在 URL 里（见 `lib/sort.ts` 的文件头）。
 * 于是**不出货一个字节的客户端 JS**（实测 First Load JS 仍是 103 kB）就有了：
 * 可分享的排序、能用的后退键、以及关掉 JS 照样能排。
 *
 * 三件与 §10.4 直接相关的事：
 *
 * - **箭头不是靠颜色说话。** 当前列换的是字形（`▲`/`▼` 对 `⇅`）**和**亮度，
 *   任一单独都够用 —— §10.4「永不单靠颜色传达方向」。
 * - **强调色不能用在这里。** 琥珀是「今日三强」这一个语义的专用色（§10.4
 *   「单一强调色」），排序状态借它一用，那个语义就被稀释了。所以用中性的亮度差。
 * - **静默列的 `⇅` 压到最暗。** §10.4 说过「表格里每多一块东西都在和数字抢注意力」，
 *   而这是一次给每一列都加一块东西的改动 —— 不压暗就是十个小图标在和数字抢。
 */
function SortHeader({
  col,
  frozenCorner,
  sort,
  href,
}: {
  col: PoolColumn;
  /** 左上角那一格同时属于冻结的行与列。 */
  frozenCorner: boolean;
  sort: SortState;
  href: string;
}) {
  const active = sort.key === col.id;
  const nextWord = (active ? (sort.dir === "asc" ? "desc" : "asc") : defaultDir(col.id)) === "asc" ? "升序" : "降序";
  return (
    <th
      scope="col"
      // `aria-sort` 是屏幕阅读器读到「这一列正被排序」的**唯一**途径 ——
      // 箭头是 `aria-hidden` 的装饰，它自己不说明任何事。
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      className={`frozen-head ${frozenCorner ? "frozen-col " : ""}px-3 py-2 ${col.align === "left" ? "text-left" : "text-center"}`}
    >
      <a
        href={href}
        // **裸 `<a>`，不是 `next/link`。** 初版用了 Link，实测它把路由运行时
        // 第一次拉进主包：`/` 与 `/states` 的 First Load JS 从 103 kB 涨到
        // **106 kB**（多出来的 chunk 8.5 KB raw / 3.4 KB gzip），而这个方案
        // 全部的卖点就是「不加客户端 JS」—— 用 Link 等于一边说零成本一边收 3.4KB。
        // 换成整页导航之后回到 103 kB，并且与站内其余链接（方法论、演示态）写法一致。
        // 代价是每次排序一次整页加载：页面本来就是 `force-dynamic`，
        // 数据仍走 Data Cache，那一次请求**不打数据源**。
        // **整页加载真正的代价不是那次请求，是新文档会把表格框的横向位置和
        // 键盘焦点清零**（软导航时 React 复用同一个 DOM 节点，两样都保得住）。
        // 手机上于是要多滑两下才能看到自己刚排的那一列。**这个代价是选来的**：
        // 试过用 `#` 锚点把位置要回来，它确实修好了横向，但同时把整个文档往下
        // 拖了 141px，§10.6 的陈旧黄条和「数据截至」双双出视野 —— 而排序链接
        // 可分享正是这套方案的卖点，收到链接的人第一屏就看不到「管道停了」。
        // 详细的测量与为什么 `scroll-margin-top` 救不了它，见 §10.2 的取舍块。
        title={col.sortNote}
        // `whitespace-nowrap`：加了箭头之后「20日动量」「60日走势」会折成两行，
        // 把表头撑高、也把上一轮刚对齐好的「表头对准内容块中心」又搞歪。
        className={`inline-flex items-baseline gap-1 whitespace-nowrap underline-offset-4 hover:underline ${active ? "text-zinc-200" : ""}`}
      >
        <span>{col.label}</span>
        <span aria-hidden="true" className={`text-[9px] ${active ? "" : "text-zinc-700"}`}>
          {/* **不要用 `↕`（U+2195）**：它在 emoji 集里，实测被渲染成一个蓝色
              emoji 方块 —— 在一个「单一强调色、其余全中性」的表里格外刺眼，
              而且它比它标注的数字还抢眼。`⇅`（U+21C5）不在 emoji 集里，
              和 `▲`/`▼` 一样走文本渲染。 */}
          {active ? (sort.dir === "asc" ? "▲" : "▼") : "⇅"}
        </span>
        <span className="sr-only">
          {`：按此列${nextWord}排列${col.sortNote ? `（${col.sortNote}）` : ""}`}
        </span>
      </a>
    </th>
  );
}

/**
 * 非 core 的指标住在 `metrics_daily.extra` 里（§9.2），表格为它们**自动**追加列。
 *
 * 这是 M6 的一条验收标准，而它背后是 §3.6 的承诺：
 * 「新增一个指标 = 写函数 + 改 config，**不需要改前端代码**」。
 * §6.3 的脚注说得很直白：那个承诺**只有在前端从第一天起就同时读显式列和
 * `extra->>'<id>'` 时才成立**；如果 M6 只接了显式列，这个承诺是假的，
 * 而且要到第一次加指标时才会发现。
 *
 * 上面那几列是**手工策展**的 —— §10.4 花了整节论证「不是所有数字都该是数字」，
 * RSI 该是分段条、β 该是刻度条。自动列拿不到那个待遇，只有数值，
 * 但它保证了那条承诺是真的。
 */
export function PoolTable({
  rows,
  benchmark,
  extraColumns = [],
  sort,
  path,
  params,
}: {
  rows: Row[];
  benchmark: string;
  extraColumns?: ExtraColumn[];
  /** 当前排序状态（由页面从 URL 解析，见 `lib/sort.ts`）。 */
  sort: SortState;
  /** 表头链接的基准路径与要原样带走的其余查询参数。 */
  path: string;
  params: Record<string, string | string[] | undefined>;
}) {
  // 列定义只有一份（`lib/columns.ts`）：表头、排序白名单、缺失行的 colSpan 都读它。
  const columns = poolColumns(extraColumns);
  return (
    // **纵向也必须是滚动容器**，不只是横向。
    // sticky 钉的是「最近的滚动容器」—— 初版这里只有 `overflow-x-auto`，
    // 那个框在纵向从不滚动，于是 `top: 0` 的表头会跟着整个框一起滚出屏幕。
    // 给一个 `max-height` 把纵向滚动收进这个框，两个方向的冻结才同时成立。
    // 用 `dvh` 不用 `vh`：手机上地址栏收起时 `vh` 是错的，表格会被切掉一截。
    // 一个可滚动的框必须自己可聚焦，否则只有鼠标/手指能滚它（WCAG 2.1.1）。
    <div
      role="region"
      aria-label="全池速览（表格可横向与纵向滚动）"
      tabIndex={0}
      className="max-h-[calc(100dvh-7rem)] overflow-auto"
    >
      <table className="w-full min-w-[56rem] border-collapse text-sm">
        <caption className="sr-only">
          全池速览：每只标的的最新价、20 日动量、距 EMA60、RSI(14)、β、年化 α 与最近事件
        </caption>
        <thead>
          {/* 表头那条下边框改由 `.frozen-head` 的 inset shadow 画 ——
              `border-collapse` 的表里，sticky 单元格的 border 会跟着表滚走。 */}
          <tr className="text-xs uppercase tracking-wide text-zinc-500">
            {columns.map((c, i) => (
              <SortHeader
                key={c.id}
                col={c}
                frozenCorner={i === 0}
                sort={sort}
                href={sortHref(path, params, nextSort(sort, c.id))}
              />
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const m = r.metrics;
            const top3 = r.rank?.in_top_n === true;
            return (
              <tr
                key={r.symbol}
                // 琥珀底纹本身在 `globals.css` 的 `.top3`（components 层）里 ——
                // 同一个值要在行、冻结列、缺失文案三处出现，写成三份就会漂。
                className={`border-b border-ink-800/60 ${top3 ? "top3" : ""}`}
              >
                <th scope="row" className="frozen-col px-3 py-2 text-left font-normal">
                  <span className="flex items-center gap-2">
                    {/* 轻量强调标记 —— 不是另起一块（§10.2） */}
                    {top3 ? (
                      <span className="text-accent" title={`今日三强 第 ${r.rank?.rank} 名`}>
                        ★
                      </span>
                    ) : (
                      <span className="w-[1ch]" />
                    )}
                    <span className="font-medium text-zinc-100">{r.symbol}</span>
                    <span className="hidden text-xs text-zinc-500 sm:inline">{r.name}</span>
                    {r.symbol === benchmark ? (
                      <span className="rounded bg-ink-800 px-1 text-[10px] text-zinc-500">基准</span>
                    ) : null}
                  </span>
                </th>

                {m === null ? (
                  // §10.6「部分标的缺失」：该行显示「数据缺失」而非空白。
                  // 文案外面那层 `.frozen-note` 不是装饰：横滚时它必须跟着冻结列一起钉住，
                  // 否则这一行会在屏幕上变成「钉住的标的 + 钉住的指标名 + 一整行空白」。
                  <td className="py-2 pr-3 text-left text-zinc-600" colSpan={columns.length - 1}>
                    <span className="frozen-note pl-3">数据缺失（该标的本次未能取得可信行情）</span>
                  </td>
                ) : (
                  <>
                    <td className="num px-3 py-2 text-center">
                      {isMissing(r.latestClose) ? (
                        <Missing />
                      ) : (
                        <span className="inline-block w-20 text-right" title={r.closeIsAdjusted ? "备源只提供复权价" : "未复权收盘价"}>
                          {fmt(r.latestClose, "price")}
                          {r.closeIsAdjusted ? <sup className="ml-0.5 text-[10px] text-zinc-500">复权</sup> : null}
                        </span>
                      )}
                    </td>
                    <Cell row={m} id="mom_20" numeric="num w-16">
                      <Signed value={metricValue(m, "mom_20")} spec="pct:2" />
                    </Cell>
                    <td className="px-3 py-2 text-center">
                      <Sparkline points={r.spark} label={`${r.symbol} 近 ${r.spark.length} 个交易日复权收盘走势`} />
                    </td>
                    <Cell row={m} id="close_vs_ema60_pct">
                      <DivergingBar value={metricValue(m, "close_vs_ema60_pct")} spec="pct:2" />
                    </Cell>
                    <Cell row={m} id="rsi_14">
                      <RsiBar value={metricValue(m, "rsi_14")} />
                    </Cell>
                    <Cell row={m} id="alpha_beta_126">
                      <BetaScale value={metricValue(m, "beta")} />
                    </Cell>
                    <Cell row={m} id="alpha_beta_126" numeric="num w-20">
                      <Alpha value={metricValue(m, "alpha_annual")} t={metricValue(m, "alpha_t_stat")} />
                    </Cell>
                    <td className="px-3 py-2 text-center">
                      <NextEvent row={m} />
                    </td>
                    {extraColumns.map((c) => (
                      <Cell key={c.id} row={m} id={c.id} numeric="num w-16">
                        {fmt(metricValue(m, c.id), c.format)}
                      </Cell>
                    ))}
                  </>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * 「事件」列只显示**最近的一个即将发生的**事件倒数芯片（§10.2）。
 * 四个距离的完整值放行内展开。
 *
 * 挑哪个事件的逻辑搬到了 `lib/nextEvent.ts` —— **这一列的排序键读的是同一个函数**。
 * 各算各的话，按天数排出来的顺序会和芯片上印的天数对不上，而那种错每一行
 * 单独看都是对的。
 */
function NextEvent({ row }: { row: MetricRow }) {
  const pick = nextEvent(row);
  if (!pick) return <span className="text-zinc-600">{EM_DASH}</span>;
  return <EventChip days={pick.days} kind={pick.kind} estimated={pick.estimated} />;
}
