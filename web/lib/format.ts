/**
 * 数字排版是第一性的（§10.4）。
 *
 * - 全站 `tabular-nums`，小数点对齐（在 CSS 里）。
 * - 正负号**常驻**，且用真减号 U+2212 而非连字符 —— 连字符比数字窄，
 *   会让负数列比正数列短一截，那正是 tabular-nums 要消灭的东西。
 * - **永不用 0 或上一日的值冒充缺失**（§10.4「诚实优先于漂亮」）。
 */

/** 真减号，不是连字符。 */
const MINUS = "−";

export const EM_DASH = "—";

function signed(n: number, body: string): string {
  if (n > 0) return `+${body}`;
  if (n < 0) return `${MINUS}${body}`;
  return body;
}

/** `null` / `undefined` / 非有限值一律显示为破折号 —— 不是 0，不是空白。 */
export function isMissing(v: unknown): v is null | undefined {
  return v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v));
}

export function fmt(value: unknown, spec: string): string {
  if (isMissing(value)) return EM_DASH;
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);

  const [kind, argRaw] = spec.split(":");
  const digits = argRaw === undefined ? undefined : Number(argRaw);

  switch (kind) {
    case "pct": {
      const d = digits ?? 2;
      return signed(n, `${Math.abs(n * 100).toFixed(d)}%`);
    }
    case "number": {
      const d = digits ?? 2;
      return Math.abs(n).toFixed(d) === Math.abs(n).toFixed(d) && n < 0
        ? `${MINUS}${Math.abs(n).toFixed(d)}`
        : n.toFixed(d);
    }
    case "signed": {
      const d = digits ?? 2;
      return signed(n, Math.abs(n).toFixed(d));
    }
    case "price":
      return n.toFixed(2);
    case "days":
      return `${Math.round(n)}d`;
    case "int":
      return String(Math.round(n));
    default:
      return String(value);
  }
}

/** 方向：给 class 用。**颜色之外始终另有符号或箭头**（§10.4）。 */
export function direction(v: unknown): "up" | "down" | "flat" | "none" {
  if (isMissing(v)) return "none";
  const n = Number(v);
  if (!Number.isFinite(n)) return "none";
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "flat";
}

export function arrow(v: unknown): string {
  const d = direction(v);
  return d === "up" ? "▲" : d === "down" ? "▼" : d === "flat" ? "·" : "";
}
