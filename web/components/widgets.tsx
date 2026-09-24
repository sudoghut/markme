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
 * α → 带符号数值；`|t| < 2` **只降透明度**，不加文字标签。
 *
 * 这是 §10.4 里最要紧的一个诚实标注：一个不显著的 α 在视觉上
 * 不该和一个显著的 α 长得一样。
 *
 * 曾经这里还挂一个「未显著」微标签。**拿掉了** —— 用户的判断是
 * 「两倍一眼就能看出来」：α 是否跨过 |t|≥2 这条线，调暗本身已经说清楚了，
 * 再加三个字是把同一件事讲两遍，而表格里每多一块东西都在和数字抢注意力。
 *
 * 信息没有丢：`t` 的具体值移到了数字自己的 tooltip 上，悬停即见。
 * 文档 §10.4 已同步改掉那句「并加『未显著』微标签」。
 *
 * （另记一笔：标签在的时候它其实是**看不见**的 —— `opacity-60` 加在外层，
 * 把标签一起压到 1.99:1 的对比度。先修好了它才发现它根本不该在。）
 */
export function Alpha({ value, t }: { value: number | null; t: number | null }) {
  if (isMissing(value)) return <Missing />;
  const weak = isMissing(t) || Math.abs(t as number) < 2;
  if (!weak) return <Signed value={value} spec="pct:1" />;
  const why = isMissing(t) ? "无 t 值" : `t = ${(t as number).toFixed(2)}`;
  return (
    <span className="opacity-60" title={`${why}；|t| < 2，这个 α 与 0 区分不开`}>
      <Signed value={value} spec="pct:1" />
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

/**
 * 事件倒计时标签。
 *
 * `estimated` 只进 `title`，**不进样式**：几乎每一枚标签都是预告值（下一次财报
 * 在公司确认前一直是估计值），标满整列等于没标（§3.5(2)）。
 */
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
      className={`inline-block rounded px-1.5 py-0.5 text-xs ${soon ? "bg-accent/15 text-accent" : "bg-ink-800 text-zinc-400"}`}
      title={estimated ? "预告值，尚未由公司确认" : "已确认"}
    >
      {kind} {days}d
    </span>
  );
}

export { arrow };
