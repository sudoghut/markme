import { NextResponse } from "next/server";
import { COOKIE } from "@/middleware";

async function hmac(secret: string): Promise<string> {
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

/**
 * `next` 只接受**站内**路径。
 *
 * `new URL(next, req.url)` 对 `https://evil.com` 和 `//evil.com` 都会解析成
 * 外站绝对地址，于是 `/login?next=https://evil.com` 在**登录成功之后**
 * 把人弹到外站 —— 一个挂在自家域名下的开放重定向。
 * 触发它要先知道口令，所以危害有限；但这是一行的事，而且这段代码
 * 就是别人会照着抄的那种。
 */
function safeNext(raw: FormDataEntryValue | null): string {
  // **先归一化，再校验，并返回归一化后的那一个。**
  //
  // 初版只做了下面三条检查就 `return v`，于是 `/<TAB>/evil.com` 整个穿过去了：
  // WHATWG 的 URL 解析器在解析**之前**会把输入里所有的 tab(0x09)、
  // LF(0x0A)、CR(0x0D) 先删掉，所以 `startsWith` 看到的是删之前的串
  // （以单个 `/` 开头，放行），而 `new URL()` 看到的是删之后的 `//evil.com`
  // —— 一个 authority。实测三种都能跳到 `https://evil.com/`。
  //
  // 只加检查不归一化等于没修：必须让校验和解析看到**同一个**字符串。
  const v = String(raw ?? "").replace(/[\t\n\r]/g, "");
  // 以单个 `/` 开头才算站内：`//host` 和 `/\host` 都是协议相对地址。
  if (!v.startsWith("/") || v.startsWith("//") || v.startsWith("/\\")) return "/";
  return v;
}

export async function POST(req: Request) {
  const password = process.env.SITE_PASSWORD;
  const form = await req.formData();
  const given = String(form.get("password") ?? "");
  const next = safeNext(form.get("next"));

  if (!password || given !== password) {
    return NextResponse.redirect(new URL(`/login?e=1&next=${encodeURIComponent(next)}`, req.url), 303);
  }
  const res = NextResponse.redirect(new URL(next, req.url), 303);
  res.cookies.set(COOKIE, await hmac(password), {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
  });
  return res;
}
