/**
 * 管道写完之后的按需重验证（§10.1）。
 *
 * **自带 token 鉴权，不依赖 cookie** —— middleware 的 matcher 排除了
 * `/api/*`，理由见 middleware.ts 的文件头。
 */
import { revalidatePath } from "next/cache";
import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const token = process.env.REVALIDATE_TOKEN;
  const given = new URL(req.url).searchParams.get("token");
  if (!token || given !== token) {
    return NextResponse.json({ ok: false }, { status: 401 });
  }
  revalidatePath("/");
  return NextResponse.json({ ok: true, revalidated: "/" });
}
