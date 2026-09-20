"use client";

import { UnavailableNotice } from "@/components/States";

/**
 * 错误边界（M6 验收：**不是白屏**）。
 *
 * 数据源不可达时 `page.tsx` 会抛 `DataUnavailableError`，于是 Next 返回
 * **500**（一个真实的非 200，监控看得见）而这里把页面渲染出来。
 *
 * 这里**不试图**显示具体原因：生产模式下 Next 会抹掉 message 只留 digest。
 * 宁可说一句准确的话，也不要显示一个可能是错的数字 —— 和整个站点的口径一致。
 */
export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <UnavailableNotice>
      {error.digest ? <p className="mt-3 text-xs text-zinc-600">digest: {error.digest}</p> : null}
      <button
        onClick={reset}
        className="mt-6 block rounded bg-ink-800 px-3 py-2 text-sm text-zinc-100 hover:bg-ink-700"
      >
        重试
      </button>
    </UnavailableNotice>
  );
}
