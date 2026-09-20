"use client";

/** 最小错误边界（M6 验收）：**不是白屏**。 */
export default function Error({ reset }: { error: Error; reset: () => void }) {
  return (
    <main className="mx-auto max-w-2xl px-6 py-24">
      <h1 className="text-lg font-medium text-zinc-100">页面出了点问题</h1>
      <p className="mt-2 text-sm text-zinc-400">
        这不是数据问题 —— 数据要么显示出来，要么显示「数据暂不可用」。
      </p>
      <button onClick={reset} className="mt-6 rounded bg-ink-800 px-3 py-2 text-sm text-zinc-100 hover:bg-ink-700">
        重试
      </button>
    </main>
  );
}
