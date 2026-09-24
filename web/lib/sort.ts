/**
 * 全池表格的排序（§10.2「17 行**可排序**表格」）。
 *
 * **排序状态住在 URL 里，排序本身在服务端做。** 这不是图省事 —— 换成
 * `useState` 就得把整张表变成 client component，17 行 × 10 列连同 sparkline
 * 的 SVG 全部进 hydration，而 §10.4 的性能预算（首屏 JS < 100KB gzip）现在
 * 已经贴着 103KB 了。走 URL 拿到四样：**不出货一个字节的客户端 JS**
 * （实测 `/` 的 First Load JS 仍是 103 kB）、排序可分享、后退键能用、
 * **没有 JS 也能排**（表头就是链接）。
 *
 * **表头是裸 `<a>`，不是 `next/link`** —— 后者会把路由运行时第一次拉进主包，
 * 实测 `/` 与 `/states` 从 103 kB 涨到 106 kB，正好把上面第一条掏空。
 * 所以每次点击是**整页导航**（不是软导航，别照着 Link 的语义改这里）。
 * 页面本来就是 `force-dynamic`，数据走 Data Cache，那一次请求**不打数据源**
 * （§10.1「出站流量接近零」不受影响）。
 */
import { metricValue } from "./metricValue";
import { nextEvent } from "./nextEvent";
import type { PoolColumn } from "./columns";
import type { MetricRow } from "./supabase";

export type SortDir = "asc" | "desc";
export type SortState = { key: string; dir: SortDir };

/** 不带参数时的顺序 —— 与数据库 `order=symbol` 出来的顺序一致，不是另一种排法。 */
export const DEFAULT_SORT: SortState = { key: "symbol", dir: "asc" };

/**
 * 点一个新列时的起始方向。
 *
 * **默认是从大到小**：点「RSI」想看的是谁最高，点「α」想看的是谁最强。
 * 两个例外，都是因为「大」在那一列上不是人想先看的东西：
 *
 * - `symbol`：按字母找，从 A 开始。
 * - `next_event`：那是一列**倒计时**。第一次点它想知道的是「下一件事什么时候发生」，
 *   即 3d 在最上面；默认给降序等于先给你 60 天以后的事。
 *   这一条还是 `/methodology`「表格排序的口径」那一节对用户的承诺
 *   （「事件按天数排，**首次点击是最近的在前**」）—— 改这里要连那句一起改。
 *   （初版曾把「越近越靠前」写进表头的 `sortNote`，而默认方向是 desc，
 *   表头文案和结果正好相反；后来改成方向中立的，但正确的默认仍然是 asc。）
 */
const ASC_FIRST = new Set(["symbol", "next_event"]);

export function defaultDir(key: string): SortDir {
  return ASC_FIRST.has(key) ? "asc" : "desc";
}

/** 排序只需要行的这几样，不依赖 `PoolTable` 的 `Row` —— 免得 lib 反向依赖组件。 */
export type SortableRow = {
  symbol: string;
  metrics: MetricRow | null;
  spark: number[];
  latestClose: number | null;
};

/**
 * 这条 sparkline 的首末涨跌。
 *
 * 「60 日走势」这一列画的是线，没有数字可排 —— 按首末涨跌排是这条线**自己**
 * 的净变化，不是另外编一个量出来。表头上写了这句话（`sortNote`），
 * 因为「按你看不见的东西排序」是一种安静的欺骗。
 */
export function sparkChange(points: number[]): number | null {
  if (points.length < 2) return null;
  // `noUncheckedIndexedAccess` 下这两个是 `number | undefined` —— 长度查过了也一样，
  // 而这正是它想要的：下面那个 `typeof` 守卫同时挡住了 undefined 和 NaN。
  const first = points[0];
  const last = points[points.length - 1];
  if (typeof first !== "number" || typeof last !== "number") return null;
  if (!Number.isFinite(first) || !Number.isFinite(last) || first === 0) return null;
  return last / first - 1;
}

/** 这一行在这一列上的可比值。`null` = 这一格没有值（**不是 0**）。 */
function sortValue(row: SortableRow, key: string): number | string | null {
  // 标的列是唯一一个在缺失行上**照样显示**的列，所以它在守卫之前。
  if (key === "symbol") return row.symbol;

  // 「数据缺失」的行整行只有一个 colSpan 单元格 —— 它在**除标的以外的每一列**上
  // 都没有值，不是「值为 0」也不是「值很小」。给 null，让下面的规则把它沉到底。
  //
  // **`close` 必须在这条守卫之后。** 它一度排在前面，于是一行
  // `metrics === null` 但 `latestClose` 有值的标的，会按价格排到降序榜的**第一位**，
  // 而屏幕上那一行写的是「数据缺失」—— 读者读到的是「这只最贵」，
  // 而页面一个数字都没给它。类型上这两个字段互相独立：`page.tsx` 现在靠同一个
  // `fresh` 把它们绑在一起，但 `fresh` 只覆盖「价格不是当天的」这一个成因；
  // 「当天有价格、却没有 metrics 行」（管道少写了一只，见 `store.py`）照样会落进来。
  const m = row.metrics;
  if (m === null) return null;

  if (key === "close") return row.latestClose;
  if (key === "spark_change") return sparkChange(row.spark);
  if (key === "next_event") return nextEvent(m)?.days ?? null;
  return metricValue(m, key);
}

/** 确定性的字符串比较 —— 不用 `localeCompare`：它的结果依赖运行时的 ICU 数据。 */
function cmpStr(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * **缺失永远沉底，不随方向翻转。**
 *
 * 这是这个文件里唯一一条会被读错的规则，所以说清楚：如果空位跟着方向跑，
 * 降序时一排「—」会顶在最上面 —— 那读起来就是「这几只的 RSI 最高」，
 * 而它们其实是没有值。§10.4「诚实优先于漂亮」在排序上的形态就是这一条。
 *
 * 同值时一律按标的字母升序兜底（**不跟着 `dir` 翻**），于是同一个 URL
 * 永远给同一个顺序 —— 否则刷新一次行就换个位置，没人能确认自己没看错。
 */
export function sortRows<T extends SortableRow>(rows: T[], sort: SortState): T[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = sortValue(a, sort.key);
    const vb = sortValue(b, sort.key);
    if (va === null && vb === null) return cmpStr(a.symbol, b.symbol);
    if (va === null) return 1;
    if (vb === null) return -1;

    const c =
      typeof va === "string" || typeof vb === "string"
        ? cmpStr(String(va), String(vb))
        : va - vb;
    return c !== 0 ? sign * c : cmpStr(a.symbol, b.symbol);
  });
}

/** 点这一列之后应该变成什么：点当前列 = 翻方向，点别的列 = 用那一列的默认方向。 */
export function nextSort(current: SortState, key: string): SortState {
  if (current.key !== key) return { key, dir: defaultDir(key) };
  return { key, dir: current.dir === "asc" ? "desc" : "asc" };
}

type Params = Record<string, string | string[] | undefined>;

function first(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

/**
 * 从 URL 参数读排序状态。
 *
 * **key 必须对着真实的列白名单校验**：URL 是用户可写的，一个不认识的 key
 * 会让每一行都取到 `null`，于是整张表按字母兜底排 —— 表头却没有任何一列
 * 显示箭头，看起来像「没排序」，而顺序其实变了。不认识就退回默认。
 */
export function parseSort(params: Params, columns: PoolColumn[]): SortState {
  const key = first(params.sort);
  if (!key || !columns.some((c) => c.id === key)) return DEFAULT_SORT;
  const dir = first(params.dir);
  return { key, dir: dir === "asc" || dir === "desc" ? dir : defaultDir(key) };
}

/**
 * 某一列表头链接的 href。
 *
 * 其余查询参数原样带走（`/states` 的 `?throw=1` 之类），而**回到默认排序时
 * 把 `sort` / `dir` 删掉** —— 默认状态就该是干净的 URL，不然分享出去的链接
 * 里永远挂着两个噪音参数。
 */
export function sortHref(path: string, params: Params, target: SortState): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (k === "sort" || k === "dir") continue;
    const s = first(v);
    if (s !== undefined) q.set(k, s);
  }
  if (target.key !== DEFAULT_SORT.key || target.dir !== DEFAULT_SORT.dir) {
    q.set("sort", target.key);
    q.set("dir", target.dir);
  }
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}
