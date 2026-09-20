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

export async function POST(req: Request) {
  const password = process.env.SITE_PASSWORD;
  const form = await req.formData();
  const given = String(form.get("password") ?? "");
  const next = String(form.get("next") ?? "/") || "/";

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
