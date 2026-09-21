/**
 * 全池表格的列定义 —— **一份真相，三个消费者**。
 *
 * 表头按它渲染、排序的白名单按它生成、`colSpan`（「数据缺失」那一行）按它计数。
 * 之前 `colSpan={8 + extraColumns.length}` 里那个 `8` 是手数出来的：
 * 加一列而忘了改它，缺失行会短一格或长一格，且**只在有标的缺数据时才看得见**。
 *
 * `key` 同时是排序键。除 `symbol` / `close` / `spark_change` / `next_event` 四个
 * 之外，其余都直接是 `config/metrics.yaml` 里的指标 id —— 于是 §3.6 那句
 * 「新增一个指标 = 写函数 + 改 config，不改前端代码」对**排序**也成立：
 * 自动追加的列自带排序，不用回来登记。
 */
export type PoolColumn = {
  /**
   * 列 id，同时就是排序键。**叫 `id` 不叫 `key`** 有两个原因：与 `ExtraColumn.id`
   * 和 `config/metrics.yaml` 里的指标 id 同名同义；而且 gitleaks 的
   * `generic-api-key` 规则会把「`key` 这个词后面跟一个长指标 id」整体当成高熵密钥
   * 报出来（实测 entropy 3.57，CI 的 secrets 这一步直接红）。与其加 allowlist
   * 把扫描削弱，不如让这个字段本来就该叫的名字把触发词去掉。
   * **这段注释也不要把那个模式原样写回来** —— 写回来就又踩一次。
   */
  id: string;
  label: string;
  /** 只有「标的」这一列左对齐（§10.4：表头对准内容块的中心）。 */
  align?: "left";
  /**
   * 这一列**按什么排序**，仅当它与画出来的东西不是逐字对应时才写。
   * 写出来是因为「按你看不见的量排序」是一种安静的欺骗。
   */
  sortNote?: string;
};

/** 手工策展的那几列（§10.4：不是所有数字都该是数字）。 */
const CURATED: PoolColumn[] = [
  { id: "symbol", label: "标的", align: "left" },
  { id: "close", label: "最新价" },
  { id: "mom_20", label: "20日动量" },
  { id: "spark_change", label: "60日走势", sortNote: "按这条线的首末涨跌排序" },
  { id: "close_vs_ema60_pct", label: "距 EMA60" },
  { id: "rsi_14", label: "RSI(14)" },
  { id: "beta", label: "β" },
  { id: "alpha_annual", label: "α(年化)" },
  { id: "next_event", label: "事件", sortNote: "按芯片上那个天数排序" },
];

export type ExtraColumn = { id: string; label: string; format: string };

export function poolColumns(extraColumns: ExtraColumn[] = []): PoolColumn[] {
  return [...CURATED, ...extraColumns.map((c) => ({ id: c.id, label: c.label }))];
}
