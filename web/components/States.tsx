/** §10.6 的四种非理想态。**每一种都要真的实现**，不能掉进白屏。 */

export function Unavailable({ reason, status }: { reason: string; status: number }) {
  return (
    <main className="mx-auto max-w-2xl px-6 py-24">
      <h1 className="text-lg font-medium text-zinc-100">数据暂不可用</h1>
      <p className="mt-2 text-sm text-zinc-400">{reason}</p>
      {status === 403 ? (
        <p className="mt-4 rounded border border-ink-700 bg-ink-900 p-3 text-xs leading-relaxed text-zinc-500">
          这是一个**授权**问题，不是网络问题 —— 数据库的 GRANT 或 RLS 被改动过。
          页面本身是好的；在授权修复之前它不会显示任何数字，
          <strong className="text-zinc-400">而不是显示一个可能是错的数字</strong>。
        </p>
      ) : null}
      <p className="mt-6 text-xs text-zinc-600">
        站点每个交易日收盘后约一小时更新一次。若这个提示持续超过一天，说明管道停了。
      </p>
    </main>
  );
}

export function EmptyDatabase() {
  return (
    <main className="mx-auto max-w-2xl px-6 py-24">
      <h1 className="text-lg font-medium text-zinc-100">还没有数据</h1>
      <p className="mt-2 text-sm text-zinc-400">
        数据库已就绪，但还没有跑过一次回填。第一次 backfill 之后这里会出现全池表格。
      </p>
    </main>
  );
}

/** 数据陈旧（>1 交易日未更新）→ 顶部黄条。 */
export function StaleBanner({ asOf, sessionsBehind }: { asOf: string; sessionsBehind: number }) {
  if (sessionsBehind <= 1) return null;
  return (
    <div className="border-b border-accent/30 bg-accent/10 px-6 py-2 text-xs text-accent">
      数据截至 {asOf}，已落后 {sessionsBehind} 个交易日 —— 管道可能停了。
      页面显示的是最后一次成功写入的结果。
    </div>
  );
}
