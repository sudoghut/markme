/**
 * 健康检查 —— **「正确 HTTP 状态码」真正的落点**（§10.6 / §12 #9 第 4 条）。
 *
 * ## 为什么这件事不能交给页面
 *
 * 实测（`next start` + 一个会断气的假数据源）把两条路都走死了：
 *
 * 1. **页面级 ISR**（`export const revalidate = 3600`）：缓存条目热着的时候
 *    把数据源杀掉，连打三次全是 **200 + 故障前的价格 + 没有黄条**。
 *    Next 的 response cache 先把旧页面返给访客再去后台重生成，
 *    后台那次一抛就被吞成一行 `console.error`，旧条目留在缓存里继续发。
 *    **永远不会自愈**，因为 `sessionsBehind` 被冻在生成那一刻的 `1` 上。
 * 2. **按请求渲染 + fetch 的 Data Cache**：Data Cache **同样**是
 *    stale-while-revalidate。让数据源改口 111.11 → 222.22，过期后第一次请求
 *    拿到的仍是 111.11（旧值先返、后台再刷）；把源杀掉再过期，拿到的是 222.22。
 *    错误一样被吞，一样是 200。
 *
 * 也就是说：**只要页面还有任何一份能用的缓存，它就不会、也不该返回 500。**
 * 那份缓存里的数字是真的，`数据截至 <date>` 那行标注也是真的 ——
 * 页面在这一刻并没有撒谎，硬把它改成 500 才是撒谎。
 *
 * 会撒谎的是「监控看到一串 200 就以为管道活着」。所以状态码这件事
 * 归给一个**不带缓存**的端点：Route Handler 能真正地设状态码并**服务端**
 * 渲染出内容，这正是 Server Component 做不到的那件事。
 *
 * 页面那边保留的是**冷缓存**那条路：一份缓存都没有时 `failClosed` 照样抛 500。
 */
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

const URL_ = process.env.SUPABASE_URL?.replace(/\/$/, "") ?? "";
const KEY = process.env.SUPABASE_ANON_KEY ?? "";

export async function GET() {
  if (!URL_ || !KEY) {
    return NextResponse.json(
      { ok: false, reason: "未配置 SUPABASE_URL / SUPABASE_ANON_KEY" },
      { status: 503 },
    );
  }
  try {
    // 最便宜的一次真读：一行 date。既验连通性，**也验授权** ——
    // §9.3 的 GRANT/RLS 被人在 SQL Editor 里改掉时，长的就是 401/403。
    const res = await fetch(`${URL_}/rest/v1/metrics_daily?select=date&order=date.desc&limit=1`, {
      headers: { apikey: KEY, Authorization: `Bearer ${KEY}` },
      cache: "no-store",
    });
    if (!res.ok) {
      const authish = res.status === 401 || res.status === 403;
      return NextResponse.json(
        {
          ok: false,
          reason: authish ? "无权读取数据（授权配置异常）" : `数据源返回 ${res.status}`,
          upstream: res.status,
        },
        { status: 503 },
      );
    }
    const rows = (await res.json()) as { date: string }[];
    // 空库不是故障 —— 第一次回填之前它就是空的（§10.6 之一）。
    return NextResponse.json({ ok: true, as_of: rows[0]?.date ?? null });
  } catch (e) {
    return NextResponse.json(
      { ok: false, reason: `无法连接数据源：${(e as Error).message}` },
      { status: 503 },
    );
  }
}
