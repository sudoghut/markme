/** §10.6 的四种非理想态。**每一种都要真的实现**，不能掉进白屏。 */

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

/**
 * 「数据暂不可用」的正文。没有 hook、没有事件，所以**服务端和客户端都能渲染** ——
 * 构建期走 `page.tsx` 的兜底，运行期走 `error.tsx`，两条路只有一份文案。
 */
export function UnavailableNotice({ children }: { children?: React.ReactNode }) {
  return (
    <main className="mx-auto max-w-2xl px-6 py-24">
      <h1 className="text-lg font-medium text-zinc-100">数据暂不可用</h1>
      <p className="mt-2 text-sm text-zinc-400">
        站点读不到数据源。页面本身是好的 —— 在恢复之前它不会显示任何数字，
        <strong className="text-zinc-300">而不是显示一个可能是错的数字</strong>。
      </p>
      <p className="mt-4 rounded border border-ink-700 bg-ink-900 p-3 text-xs leading-relaxed text-zinc-500">
        可能是数据库不可达，也可能是授权（GRANT / RLS）被改动过。
        具体原因在服务端日志里 —— 生产模式下 Next 只把一个 digest 传到浏览器。
      </p>
      {children}
      <p className="mt-6 text-xs text-zinc-600">
        站点每个交易日收盘后约一小时更新一次。若这个提示持续超过一天，说明管道停了。
      </p>
    </main>
  );
}

/**
 * 数据源不可达。**抛出去，不是 return 一个组件。**
 *
 * §10.6 要的是「显示『数据暂不可用』**+ 正确 HTTP 状态码**，不是白屏」。
 * 而 App Router 的 Server Component **拿不到 response 对象**，设不了任意状态码 ——
 * 之前那版直接 `return <Unavailable/>`，于是一次真实的数据库/RLS 故障
 * 对外是一个 **HTTP 200**：监控和 CDN 都看不出任何异常，
 * 而那正是这条验收标准要防的东西（§12 #9 第 4 条）。
 *
 * 抛异常会让 Next 返回 **500**，同时 `error.tsx` 把这个态渲染出来 ——
 * 「有状态码」和「不是白屏」两个要求同时满足。
 *
 * 实测（`next start` + 一个必抛的路由）：
 *
 * - 状态码 **500**。想要更贴切的 503 是做不到的，App Router 只给这一个。
 * - 响应体是 Next 的 `__next_error__` 文档：它 **preload 了 `app/error-*.js`
 *   并带上 digest**，浏览器水合后渲染出「数据暂不可用」。
 *   也就是说正文是**客户端**渲染的 —— `curl` 或关掉 JS 会拿到一个空的 500。
 *   这是残留的取舍，写在这里以免下一个人以为它是 SSR 的。
 *
 * 详细原因写进**服务端日志**：生产模式下 Next 会把错误 message 抹掉，
 * 只留 digest，所以不能指望它传到浏览器。
 */
export class DataUnavailableError extends Error {
  readonly status: number;

  constructor(reason: string, status: number) {
    super(`数据源不可达（${status}）：${reason}`);
    this.name = "DataUnavailableError";
    this.status = status;
  }
}

export function failClosed(reason: string, status: number): never {
  // 服务端日志是唯一能拿到具体原因的地方。
  console.error(`[markme] 数据暂不可用 status=${status} reason=${reason}`);
  throw new DataUnavailableError(reason, status);
}
