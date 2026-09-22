/**
 * 构建期直读 `config/*.yaml`（§6.1）——**没有中间产物**。
 *
 * 前端表头、格式化、方法论页的公式清单全部从这里来。
 * 生成一个 JSON 中间产物会制造 §6.1.1 说的那种「两个要对齐的地方」。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import yaml from "js-yaml";

const CONFIG_DIR = join(process.cwd(), "..", "config");

export type DisplaySpec = {
  label: string;
  format: string;
  widget: string;
  dim_when?: string | null;
  estimated_flag?: string | null;
};

export type MetricSpec = {
  id: string;
  fn?: string;
  core?: boolean;
  params?: Record<string, unknown>;
  min_bars?: number;
  provisional_below?: number | null;
  display: DisplaySpec;
  derived?: { id: string; expr: string; display: DisplaySpec }[];
};

export type AppConfig = {
  site: { title: string; subtitle: string; disclaimer: string; repo_url: string };
  revalidate_seconds: number;
  sparkline_bars: number;
  lookback_bars: number;
  settle_minutes: number;
};

function read<T>(name: string): T {
  return yaml.load(readFileSync(join(CONFIG_DIR, name), "utf8")) as T;
}

export const app = read<AppConfig>("app.yaml");
export const universe = read<{ benchmark: string; symbols: { symbol: string; name: string; type: string; enabled: boolean }[] }>("universe.yaml");
export const strength = read<{ score_metric: string; top_n: number; rank_pool: string; squeak_k?: number }>("strength.yaml");
const metricsFile = read<{ metrics: MetricSpec[] }>("metrics.yaml");

/** 所有指标（含 derived 展开），按 config 顺序。 */
export const metrics: MetricSpec[] = metricsFile.metrics;

/**
 * 表格要显示的列 —— **显式列与 derived 一视同仁**。
 *
 * §3.6 承诺「新增一个指标 = 改 config，不改前端代码」，
 * 而那条承诺只有在表格**从 config 生成列**时才成立。
 */
export function displayColumns(): { id: string; display: DisplaySpec }[] {
  const out: { id: string; display: DisplaySpec }[] = [];
  for (const m of metrics) {
    out.push({ id: m.id, display: m.display });
    for (const d of m.derived ?? []) out.push({ id: d.id, display: d.display });
  }
  return out;
}
