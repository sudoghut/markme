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
}: {
  rows: Row[];
  benchmark: string;
  extraColumns?: { id: string; label: string; format: string }[];
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[56rem] border-collapse text-sm">
        <caption className="sr-only">
          全池速览：每只标的的最新价、20 日动量、距 EMA60、RSI(14)、β、年化 α 与最近事件
        </caption>
        <thead>
          <tr className="border-b border-ink-700 text-xs uppercase tracking-wide text-zinc-500">
            <th scope="col" className="px-3 py-2 text-left">标的</th>
            <th scope="col" className="px-3 py-2 text-center">最新价</th>
            <th scope="col" className="px-3 py-2 text-center">20日动量</th>
            <th scope="col" className="px-3 py-2 text-center">60日走势</th>
            <th scope="col" className="px-3 py-2 text-center">距 EMA60</th>
            <th scope="col" className="px-3 py-2 text-center">RSI(14)</th>
            <th scope="col" className="px-3 py-2 text-center">β</th>
            <th scope="col" className="px-3 py-2 text-center">α(年化)</th>
            <th scope="col" className="px-3 py-2 text-center">事件</th>
            {extraColumns.map((c) => (
              <th key={c.id} scope="col" className="px-3 py-2 text-center">
                {c.label}
              </th>
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
                className={`border-b border-ink-800/60 ${top3 ? "bg-accent/[0.06]" : ""}`}
              >
                <th scope="row" className="px-3 py-2 text-left font-normal">
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
                  <td className="px-3 py-2 text-left text-zinc-600" colSpan={8 + extraColumns.length}>
                    数据缺失（该标的本次未能取得可信行情）
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
 */
function NextEvent({ row }: { row: MetricRow }) {
  const e = metricValue(row, "days_to_next_earnings");
  const d = metricValue(row, "days_to_next_dividend");
  const pick =
    e !== null && (d === null || e <= d)
      ? { days: e, kind: "财报", est: row["next_earnings_is_estimated"] as boolean | null }
      : d !== null
        ? { days: d, kind: "除息", est: row["next_dividend_is_estimated"] as boolean | null }
        : null;
  if (!pick) return <span className="text-zinc-600">{EM_DASH}</span>;
  return <EventChip days={pick.days} kind={pick.kind} estimated={pick.est} />;
}
