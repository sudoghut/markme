/**
 * 首页顶部的利率窄条（docs/add-rate-policy.md §3）。**只画，不判断** —— 判断在 `lib/rates.ts`。
 *
 * 三种画法（`RatesStatus`）：
 *
 * - `hidden`：表还是空的（宏观管道还没回填过）。整条不画 —— 此时「没有这个板块」
 *   与「它坏了」在访客看来没有区别，也没什么可说的。
 * - `unavailable`：**读失败**，说一句话，**不消失**。消失会让「没有这个功能」与
 *   「它坏了」无法区分。这与首页其余读取（`failClosed`）**刻意相反**：这条窄条是装饰性的，
 *   它的故障不该让整页变成 500（0003 迁移头部记着代码先于迁移上线时 `failClosed` 的后果）。
 * - `ok`：画。每一项各有自己的陈旧标记 —— 三个来源的发布节奏不同，一项陈旧不连累另外两项。
 *
 * 缺失就是缺失：没有未来会议写「暂无日程」，窗口内无变动写「自（库里最早的数据日期）起无变动」，
 * **不用 0、大数字或编造的日期冒充**（§10.4）。
 */
import { arrow } from "@/lib/format";
import {
  rangeText,
  signedBp,
  type RatesStatus,
  type RatesModel,
} from "@/lib/rates";

function Item({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-zinc-500">{label}</span>
      <span className="num text-zinc-200">{children}</span>
    </div>
  );
}

function Stale() {
  return (
    <span className="ml-1 text-accent" title="这一项的数据源最近一次成功抓取已超过阈值，显示的是最后一次成功的结果">
      （数据已陈旧）
    </span>
  );
}

function Body({ m }: { m: RatesModel }) {
  return (
    <>
      <Item label="联邦基金目标区间">
        {m.range ? (
          <>
            {rangeText(m.range.lower, m.range.upper)}
            {m.range.stale ? <Stale /> : null}
          </>
        ) : (
          <span className="text-zinc-400">暂无数据</span>
        )}
      </Item>

      {/* 没有目标区间数据时**不写**「上次变动」：那不是「没变过」，是「不知道」。 */}
      {m.range ? (
        <Item label="上次变动">
        {m.lastChange ? (
          <>
            <span aria-hidden="true">
              {m.lastChange.kind === "up" ? "▲ " : m.lastChange.kind === "down" ? "▼ " : ""}
            </span>
            {m.lastChange.text}
            <span className="text-zinc-500">
              {" "}
              · {m.lastChange.date} 生效（{m.lastChange.daysAgo} 天前）
            </span>
          </>
        ) : (
          <span className="text-zinc-400">自 {m.noChangeSince} 起无变动</span>
        )}
        </Item>
      ) : null}

      <Item label="下次 FOMC">
        {m.meeting ? (
          <>
            {m.meeting.text}
            <span className="text-zinc-500">
              {" "}
              · {m.meeting.inProgress ? "进行中" : `${m.meeting.daysUntil} 天后`}
            </span>
            {m.meeting.stale ? <Stale /> : null}
          </>
        ) : (
          <span className="text-zinc-400">暂无日程</span>
        )}
      </Item>

      {m.yields ? (
        <Item label="美债收益率">
          {m.yields.cells.map((c, i) => (
            <span key={c.key}>
              {i > 0 ? <span className="text-zinc-700"> · </span> : null}
              {c.label} {c.value.toFixed(2)}
              {c.changeBp !== null ? (
                <span className="text-zinc-500">
                  {" "}
                  ({arrow(c.changeBp)}
                  {signedBp(c.changeBp)})
                </span>
              ) : null}
            </span>
          ))}
          {m.yields.spread2s10sBp !== null ? (
            <span className="text-zinc-500"> · 2s10s {signedBp(m.yields.spread2s10sBp)}</span>
          ) : null}
          {m.yields.stale ? <Stale /> : null}
        </Item>
      ) : null}
    </>
  );
}

export function RatesStrip({ status }: { status: RatesStatus }) {
  if (status.kind === "hidden") return null;

  if (status.kind === "unavailable") {
    return (
      <section
        aria-label="利率与政策"
        className="mt-4 rounded border border-ink-800 bg-ink-900/40 px-3 py-2 text-xs text-zinc-500"
        title={status.reason}
      >
        利率数据暂不可用
      </section>
    );
  }

  return (
    <section
      aria-label="利率与政策"
      className="mt-4 rounded border border-ink-800 bg-ink-900/40 px-3 py-2 text-xs"
    >
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <Body m={status.model} />
      </div>
      <p className="mt-1.5 text-[11px] text-zinc-600">
        来源：纽约联储、美国财政部、美联储 ·{" "}
        <a href="/methodology#rates" className="underline underline-offset-2">
          口径与声明
        </a>
      </p>
    </section>
  );
}
