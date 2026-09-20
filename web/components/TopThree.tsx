/**
 * 今日三强 —— 表格**下方**的一条紧凑横条，不是三张 hero 卡片（§10.3）。
 *
 * 风险上下文（RSI / 距EMA60 / β）**不在这里重复**，它们就在上方表格的同一行里。
 *
 * 每格四行：名次+主分 / 排名动能+持续性 / 领先厚度+条形 / 超池内中位。
 * 条形长度就是 `delta_to_next`，一眼看出这个名次稳不稳。
 */
import { fmt, isMissing } from "@/lib/format";
import type { StrengthRow } from "@/lib/supabase";

/**
 * 「名次胶着」的阈值**不能是固定的 0.5pp**。
 *
 * 把算术摆出来：这 16 只大型科技股的 20 日动量离散度在平静期约 2pp，
 * 相邻间隔约 0.125pp，远小于 0.5pp → **一直触发**；
 * 高离散期铺开 25pp，间隔约 1.5pp，大于 0.5pp → **永不触发**。
 * 于是它在最不需要提醒的时候最吵，而在真正该提醒时沉默。
 *
 * 归一化阈值是尺度无关的，语义也更合理：
 * **同样的绝对间隔，在密集的池里是真分开了，在铺得很开的池里只是噪声。**
 */
export function isSqueaky(rows: StrengthRow[], k: number, topN: number): boolean {
  const scores = rows.map((r) => r.score).filter((s): s is number => s !== null && Number.isFinite(s));
  if (scores.length < 3) return false;
  const mean = scores.reduce((a, b) => a + b, 0) / scores.length;
  const sd = Math.sqrt(scores.reduce((a, b) => a + (b - mean) ** 2, 0) / (scores.length - 1));
  const last = rows.find((r) => r.rank === topN);
  if (!last || isMissing(last.delta_to_next)) return false;
  return last.delta_to_next < k * sd;
}

function Card({ r, maxDelta }: { r: StrengthRow; maxDelta: number }) {
  const width = isMissing(r.delta_to_next) ? 0 : Math.max(4, (r.delta_to_next / (maxDelta || 1)) * 100);
  const streak = r.days_in_top_n;
  const move = r.rank_delta_1d;
  return (
    <div className="rounded border border-ink-700 bg-ink-900/60 p-3">
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-medium text-zinc-100">
          <span className="text-accent">{"①②③".charAt(r.rank - 1) || r.rank}</span> {r.symbol}
        </span>
        <span className="num text-sm text-up">{fmt(r.score, "pct:2")}</span>
      </div>

      <div className="mt-1 text-xs text-zinc-500">
        {isMissing(move) ? "新晋" : move > 0 ? `↑${move}` : move < 0 ? `↓${Math.abs(move)}` : "—"}
        {" · "}
        {isMissing(streak) ? "新晋" : `在榜 ${streak} 天`}
      </div>

      <div className="mt-2 text-xs text-zinc-400">
        领先下一名 <span className="num">{fmt(r.delta_to_next, "pct:2")}</span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded-sm bg-ink-800" role="img" aria-label={`领先厚度 ${fmt(r.delta_to_next, "pct:2")}`}>
        <div className="h-full rounded-sm bg-accent/70" style={{ width: `${width}%` }} />
      </div>

      <div className="mt-2 text-xs text-zinc-500">
        超池内中位 <span className="num">{fmt(r.delta_to_median, "pct:2")}</span>
      </div>
    </div>
  );
}

export function TopThree({
  rows,
  all,
  scoreMetric,
  squeakK,
  topN,
}: {
  rows: StrengthRow[];
  all: StrengthRow[];
  scoreMetric: string;
  squeakK: number;
  topN: number;
}) {
  if (rows.length === 0) return null;
  const maxDelta = Math.max(...rows.map((r) => r.delta_to_next ?? 0), 0);
  const squeaky = isSqueaky(all, squeakK, topN);
  return (
    <section className="mt-8" aria-labelledby="top3">
      <div className="flex items-baseline justify-between">
        <h2 id="top3" className="text-sm font-medium text-zinc-300">
          今日三强 <span className="text-zinc-500">（{scoreMetric} · 收盘口径）</span>
        </h2>
        <span className="text-xs text-zinc-600">尺子：{scoreMetric}</span>
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        {rows.map((r) => (
          <Card key={r.symbol} r={r} maxDelta={maxDelta} />
        ))}
      </div>
      {squeaky ? (
        <p className="mt-2 text-xs text-accent">
          ⚠ 第 {topN} 名与第 {topN + 1} 名胶着 —— 这个名次今天不稳，别把它当结论。
        </p>
      ) : null}
    </section>
  );
}
