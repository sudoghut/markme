/**
 * 象征性密码保护（§10.5）。
 *
 * **诚实标注它的强度**：这是象征性的，**不是安全边界**。
 * 仓库是公开的，密码是弱口令，middleware 只拦页面。
 * 真正的安全边界仍然是 §9.3 的 GRANT + RLS —— anon key 本来就可公开，
 * 这道门拦不住直接打 Supabase REST 的人。
 *
 * 它买到的是两件实际的东西：搜索引擎索引不到（Yahoo 条款风险下降）、
 * 爬虫刷不到（免费额度风险归零）。
 *
 * **matcher 必须排除 `/api/*`，这不是可选项。**
 * §10.1 的按需重验证是 GitHub Actions POST `/api/revalidate?token=…`，
 * 那个请求**没有 cookie**，会拿到一个 307 跳转到 `/login` 而不是错误；
 * 于是管道报 ok、重验证从未发生、页面退回 revalidate=3600 ——
 * 恰好是 §10.1 特意设计掉的那个延迟，而且无从得知。
 */
import { NextResponse, type NextRequest } from "next/server";

export const COOKIE = "markme_auth";

/** cookie 存的是 HMAC，不是密码本身。 */
async function expected(secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode("markme-v1"));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** 恒定时间比较。这几行成本为零，且避免写出一段会被抄走的坏范例。 */
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function middleware(req: NextRequest) {
  const password = process.env.SITE_PASSWORD;
  // 没配密码就不拦 —— 本地开发不该被一道没设过的门挡住。
  if (!password) return NextResponse.next();

  const got = req.cookies.get(COOKIE)?.value;
  if (got && safeEqual(got, await expected(password))) return NextResponse.next();

  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.searchParams.set("next", req.nextUrl.pathname);
  return NextResponse.redirect(url);
}

export const config = {
  // 排除 /login（否则无限跳转）、静态资源、以及 **/api/***（见文件头）。
  matcher: ["/((?!login|api|_next/static|_next/image|favicon.ico|robots.txt).*)"],
};
