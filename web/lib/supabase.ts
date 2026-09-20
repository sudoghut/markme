/**
 * 服务端读取 Supabase（§10.1）。
 *
 * **用户浏览器不直连 Supabase** —— anon key 不进浏览器包，
 * 全球 CDN 命中，Supabase 出站流量接近零，首屏是纯 HTML 无 loading 闪烁。
 *
 * 读失败**不抛到页面之外**：§10.6 要求「数据库不可达时显示『数据暂不可用』
 * + 正确 HTTP 状态码，不是白屏」。所以这里把失败折叠成一个带原因的结果，
 * 由页面决定怎么呈现。
 */

const URL_ = process.env.SUPABASE_URL?.replace(/\/$/, "") ?? "";
const KEY = process.env.SUPABASE_ANON_KEY ?? "";

export type Fetched<T> =
  | { ok: true; rows: T[] }
  | { ok: false; reason: string; status: number };

export async function rest<T>(path: string, revalidate: number): Promise<Fetched<T>> {
  if (!URL_ || !KEY) {
    return { ok: false, reason: "未配置 SUPABASE_URL / SUPABASE_ANON_KEY", status: 503 };
  }
  try {
    const res = await fetch(`${URL_}/rest/v1/${path}`, {
      headers: { apikey: KEY, Authorization: `Bearer ${KEY}` },
      next: { revalidate },
    });
    if (!res.ok) {
      // 403 在这里是一个**有意义的**状态：§9.3 的 GRANT/RLS 配错就长这样。
      // §12 #9 第 4 条要求这个态必须真的实现，而不是掉进通用错误里。
      return {
        ok: false,
        reason: res.status === 403 || res.status === 401 ? "无权读取数据（授权配置异常）" : `数据源返回 ${res.status}`,
        status: res.status === 403 || res.status === 401 ? 403 : 503,
      };
    }
    return { ok: true, rows: (await res.json()) as T[] };
  } catch (e) {
    return { ok: false, reason: `无法连接数据源：${(e as Error).message}`, status: 503 };
  }
}

/** 一行 `metrics_daily`，**显式列 + extra 双路**（§9.2 / M6 验收）。 */
export type MetricRow = {
  symbol: string;
  date: string;
  extra: Record<string, number | null> | null;
  provisional_metrics: string[] | null;
  [k: string]: unknown;
};

export type StrengthRow = {
  date: string;
  symbol: string;
  rank: number;
  score: number | null;
  in_top_n: boolean;
  delta_to_next: number | null;
  delta_to_median: number | null;
  days_in_top_n: number | null;
  rank_delta_1d: number | null;
};

export type SymbolRow = { symbol: string; name: string; type: string; enabled: boolean };
export type PriceRow = { symbol: string; date: string; close: number | null; adj_close: number };
