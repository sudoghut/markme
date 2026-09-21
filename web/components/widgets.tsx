/**
 * **指标各自的最佳形态**（§10.4）—— 不是所有数字都该是数字。
 *
 * 每个 widget 都遵守同一条规矩：缺失就是破折号，**绝不用 0 或上一日的值冒充**。
 */
import { EM_DASH, arrow, direction, fmt, isMissing } from "@/lib/format";

export function Missing({ hint = "数据缺失" }: { hint?: string }) {
  return (
    <span className="text-zinc-600" title={hint}>
      {EM_DASH}
    </span>
  );
}

/** 带符号数值。**颜色之外始终另有符号**（§10.4）。 */
export function Signed({ value, spec }: { value: unknown; spec: string }) {
  if (isMissing(value)) return <Missing />;
  const d = direction(value);
  return (
    <span className={d === "up" ? "text-up" : d === "down" ? "text-down" : "text-zinc-400"}>
      {fmt(value, spec)}
    </span>
  );
}

/** RSI → 0–100 分段条，30/70 带着色，指针标注当前值。 */
export function RsiBar({ value }: { value: number | null }) {
  if (isMissing(value)) return <Missing />;
  const pct = Math.max(0, Math.min(100, value));
  return (
    <span className="inline-flex items-center gap-2">
      <span className="num tabular-nums w-10 text-right">{fmt(value, "number:1")}</span>
      <span
        className="relative h-2 w-20 overflow-hidden rounded-sm bg-ink-800"
        role="img"
        aria-label={`RSI ${value.toFixed(1)}，30 以下超卖、70 以上超买`}
      >
        <span className="absolute inset-y-0 left-0 w-[30%] bg-up/20" />
        <span className="absolute inset-y-0 right-0 w-[30%] bg-down/20" />
        <span
          className="absolute inset-y-0 w-0.5 bg-zinc-200"
          style={{ left: `calc(${pct}% - 1px)` }}
        />
      </span>
    </span>
  );
}

/** 距 EMA60 → 以 0 为中心的双向发散条。 */
export function DivergingBar({ value, spec }: { value: number | null; spec: string }) {
  if (isMissing(value)) return <Missing />;
  const clamped = Math.max(-0.2, Math.min(0.2, value));
  const half = (Math.abs(clamped) / 0.2) * 50;
  const up = clamped >= 0;
  return (
    <span className="inline-flex items-center gap-2">
      <span className={`num w-16 text-right ${up ? "text-up" : "text-down"}`}>
        {fmt(value, spec)}
      </span>
      <span className="relative h-2 w-20 rounded-sm bg-ink-800" role="img" aria-label={fmt(value, spec)}>
        <span className="absolute inset-y-0 left-1/2 w-px bg-ink-700" />
        <span
          className={`absolute inset-y-0 ${up ? "bg-up/60" : "bg-down/60"}`}
          style={up ? { left: "50%", width: `${half}%` } : { right: "50%", width: `${half}%` }}
        />
      </span>
    </span>
  );
}

/** β → 以 1.0 为中心的刻度条（基准位置常驻参考线）。 */
export function BetaScale({ value }: { value: number | null }) {
  if (isMissing(value)) return <Missing />;
  const pos = Math.max(0, Math.min(100, ((value - 0) / 2.5) * 100));
  return (
    <span className="inline-flex items-center gap-2">
      <span className="num w-12 text-right">{fmt(value, "number:2")}</span>
      <span className="relative h-2 w-20 rounded-sm bg-ink-800" role="img" aria-label={`β ${value.toFixed(2)}，基准为 1.00`}>
        {/* 1.0 的参考线常驻 —— β 的意义完全来自「相对基准」。 */}
        <span className="absolute inset-y-0 left-[40%] w-px bg-zinc-500" />
        <span className="absolute inset-y-0 w-0.5 bg-zinc-200" style={{ left: `calc(${pos}% - 1px)` }} />
      </span>
    </span>
  );
}

/**
 * α → 带符号数值 + 显著性角标。
 *
 * 这是 §10.4 里最要紧的一个诚实标注：一个不显著的 α 在视觉上
 * 不该和一个显著的 α 长得一样。
 *
 * **`opacity-60` 只加在数字上，不加在角标上。**
 *
 * 初版把它加在外层 span 上，于是整个单元格一起变暗 —— 包括那个角标。
 * 实测对比度：角标文字 1.99:1、角标底色对页面 1.09:1
 * （zinc-500 与 ink-800 在 60% 不透明度下压到 ink-950 上）。
 * 也就是说**它在 DOM 里，但人眼看不见**，读起来只是一行普通文字。
 *
 * 而这个角标的全部职责就是说「这个数字是噪声，别当真」——
 * 让「不可信」这个样式把「不可信」这个标记本身也抹掉，
 * 恰好是 §10.4「诚实优先于漂亮」要防的那种反讽。
 *
 * 现在：数字变暗（它才是信不过的那个），角标保持不透明并提亮到 11.21:1，
 * 加一圈边框让它在深色背景上真的像一块角标。
 */
export function Alpha({ value, t }: { value: number | null; t: number | null }) {
  if (isMissing(value)) return <Missing />;
  const weak = isMissing(t) || Math.abs(t as number) < 2;
  const why = isMissing(t) ? "无 t 值" : `t = ${(t as number).toFixed(2)}`;
  return (
    <span className="inline-flex items-center justify-end gap-1 whitespace-nowrap">
      <span className={weak ? "opacity-60" : ""}>
        <Signed value={value} spec="pct:1" />
      </span>
      {weak ? (
        <span
          className="shrink-0 rounded border border-ink-700 bg-ink-800 px-1 text-[10px] leading-4 text-zinc-300"
          title={`${why}；|t| < 2，这个 α 与 0 区分不开`}
        >
          未显著
        </span>
      ) : null}
    </span>
  );
}

/** 20 日动量 → 数值 + 60 日 sparkline（自绘 SVG，不引重型图表库）。 */
export function Sparkline({ points, label }: { points: number[]; label: string }) {
  if (points.length < 2) return <Missing hint="历史不足" />;
  const lo = Math.min(...points);
  const hi = Math.max(...points);
  const span = hi - lo || 1;
  const w = 64;
  const h = 18;
  const d = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i / (points.length - 1)) * w},${h - ((p - lo) / span) * h}`)
    .join(" ");
  const rising = (points.at(-1) ?? 0) >= (points[0] ?? 0);
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label} className="overflow-visible">
      <path d={d} fill="none" strokeWidth="1.25" className={rising ? "stroke-up" : "stroke-down"} />
    </svg>
  );
}

/** 事件倒计时芯片。估计值加**虚线下划线**（§3.5(2) / §10.4）。 */
export function EventChip({
  days,
  kind,
  estimated,
}: {
  days: number | null;
  kind: string;
  estimated: boolean | null;
}) {
  if (isMissing(days)) return <Missing hint="没有已知的未来事件" />;
  const soon = days <= 10;
  return (
    <span
      className={`inline-block rounded px-1.5 py-0.5 text-xs ${soon ? "bg-accent/15 text-accent" : "bg-ink-800 text-zinc-400"} ${estimated ? "estimated" : ""}`}
      title={estimated ? "预告值，尚未由公司确认" : "已确认"}
    >
      {kind} {days}d
    </span>
  );
}

export { arrow };
