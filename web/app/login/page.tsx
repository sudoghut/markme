export const dynamic = "force-dynamic";

export default async function Login({
  searchParams,
}: {
  searchParams: Promise<{ e?: string; next?: string }>;
}) {
  const { e, next } = await searchParams;
  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      <h1 className="text-lg font-medium text-zinc-100">markme</h1>
      <p className="mt-1 text-sm text-zinc-500">market metrics</p>
      <form action="/api/login" method="post" className="mt-8 space-y-3">
        <input type="hidden" name="next" value={next ?? "/"} />
        <label htmlFor="password" className="block text-sm text-zinc-400">
          访问口令
        </label>
        <input
          id="password"
          name="password"
          type="password"
          autoFocus
          autoComplete="current-password"
          className="w-full rounded border border-ink-700 bg-ink-900 px-3 py-2 text-zinc-100 outline-none focus:border-accent"
        />
        {e ? <p className="text-sm text-down">口令不对。</p> : null}
        <button
          type="submit"
          className="w-full rounded bg-ink-800 px-3 py-2 text-sm text-zinc-100 hover:bg-ink-700"
        >
          进入
        </button>
      </form>
      {/* §10.5：必须诚实标注它的强度，否则将来有人会误以为「有密码 = 数据是私密的」。 */}
      <p className="mt-8 text-xs leading-relaxed text-zinc-600">
        这道口令是<strong className="text-zinc-500">象征性</strong>的，不是安全边界 ——
        它买到的是「搜索引擎索引不到」和「爬虫刷不到」。理由见方法论页。
      </p>
    </main>
  );
}
