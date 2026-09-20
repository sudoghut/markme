# Review 记录 — `create-project.md` v1 → v2

日期：2026-09-20 · 对象：设计文档（尚无代码）
闸门 A：2 个 review agent 并行（量化正确性 / 基础设施与安全）
闸门 B：codex CLI（见文末）

**两组 agent 独立收敛到同一批根因**（复权基准、NaN 写入路径、榜单事务性），
这是可信度的主要来源 —— 不是一个 reviewer 的偏好。

---

## Serious（19 项，全部已修）

| # | 发现 | 处置 |
|---|---|---|
| S1 | `close_vs_ema60_pct = close / ema_60 - 1` 混用复权与未复权单位 | 已修 → §3.0 规则 1；NVDA 拆股前历史行会读出 +932% |
| S2 | 复权因子被供应商追溯改写，增量管道只写当日 → 接缝 | 已修 → §3.0 规则 2：每日重抓整个 ≥380 根回看窗口 + `adj_factor` 比对 |
| S3 | 单日降级 Stooq 在一个指标窗口内混用两种复权口径 | 已修 → §3.0 规则 3 + §7.3：单日失败不降级；整窗降级；窗口内 source 唯一性断言 |
| S4 | yfinance 新版默认 `auto_adjust=True`，两列会被写成同一个数 | 已修 → §3.0 规则 4 + CI 不变式 |
| S5 | QQQ 自回归 → `alpha_t_stat = 0/0 = NaN`，`dim_when` **fail open** | 已修 → §3.4 基准特判 + §6.2 空值一律打灰 |
| S6 | alpha 复利年化在数学上无效且是默认值（夸大 2–4.5 倍，极端时撑爆列宽） | 已修 → §3.4 改线性默认 + §9.1 去掉定精度 |
| S7 | 「inner join」未说明 join 的是价格还是收益；前者会把多日收益当单日 | 已修 → §3.4：先各自算收益再 join + session 连续性断言 |
| S8 | 数据新鲜度只断言 QQQ 一只 | 已修 → §7.2 闸门 3 改为逐标的 |
| S9 | preliminary/final 两段式的重算与覆盖规则未定义 | **已消解** —— 用户改为收盘 +60min 单跑，整个两段式删除 |
| S10 | `strength_daily` upsert 不删旧行 → 榜单可能是两次计算的拼接 | 已修 → §9.1.2 删+插同事务 |
| I1 | 只写 policy 没写 `GRANT`，依赖 Supabase 隐式默认 | 已修 → §9.3 显式 revoke + grant |
| I2 | 同一隐式默认使 anon 仍持有 INSERT/UPDATE/DELETE 授权，RLS 是唯一一道 | 已修 → §9.3 收权，RLS 降为第二道 |
| I3 | SQL Editor 建表**不**默认开 RLS → 下一个迁移会上线一张可写表 | 已修 → §9.3.2 两条不变式查询进 CI |
| I4 | NaN 会在首次 backfill 直接炸掉写入路径（非法 JSON / 排序污染） | 已修 → §9.1.3 写入边界 sanitizer |
| I5 | `strength_daily` 无 `unique(date, symbol)`，同一标的可占两个名次 | 已修 → §9.1 主键改 `(date, symbol)` + `unique(date, rank)` |
| I6 | PostgREST 无多语句事务，整条写入链不原子 | 已修 → §9.1.2 改 psycopg 直连单事务 |
| I7 | 「兜底重试」是无条件重跑，好数据会被降级源覆盖；且无 `concurrency` | 已修 → §7.1 条件重试 + concurrency 组 |
| I8 | `service_role`（等同超管）与 80 个未锁定 PyPI 依赖同处一个 job | 已修 → §8.1.1 改 `pipeline_writer` 最小权限角色 + 依赖锁哈希 + step 级 secret |
| I9 | GitHub 60 天无活动停用 cron；失败邮件无法对「从未运行」告警 | 已修 → §7.2.1 月度 heartbeat + dead-man's switch |

## Minor（已采纳的主要几项）

RSI 的 `min_bars`/`provisional_below` 语义反了且不可达（→ §3.3 统一语义）；
`ewm(adjust=False)` 与 SMA 播种不是同一函数，golden 测试会假失败（→ §3.1）；
alpha t 值近似式在牛市高估 |t| 达 50%，而展示是 |t|=2 硬切（→ §3.4 用精确 OLS SE）；
`resid_vol` 的 ddof 应为 `n-2`（→ §3.4）；`ema60_slope_20d` / `rs_vs_qqq_20d`
定义了却无人声明无处存放（→ §6.2 + 双向 CI 校验）；行级 `provisional` 无法表达
按指标预热（→ `provisional_metrics text[]`）；`rank_delta_1d` / `days_in_top_n`
反规范化后会在回补时撒谎（→ 改视图现算）；排名的真正确定性风险是 NaN 而非并列
（→ 照抄 low-buy 的 -inf + isfinite 掩码）；`updated_at` 的 DEFAULT 在 upsert 时不触发
（→ 触发器）；枚举值只写在注释里（→ CHECK 约束）；`runs` 无 `running` 状态，
硬崩溃时一行都不留（→ 开跑即插）；`config.json` 的 CI 漂移检查拦不住 Vercel 部署
（→ 前端构建期直读 YAML，消灭而非检测）；`revalidate=300` 对日频数据是每天 288 次
（→ 3600 + 按需）；固定 0.5pp 的「名次胶着」阈值在高离散期一直触发（→ 归一化）；
M7 估时 1.5d 不可信（→ 3.5d，总量 6 → 10–14 人日）；DST 无法靠等待验证
（→ M5 冻结时钟测试）；`symbols` 同步脚本无归属（→ M4）；无 staging（→ §8.2 建两个项目）；
Yahoo 条款禁止再分发而站点公开（→ §13 新增一行）。

## 判定为误报 / 不采纳

- **无。** 两份报告中没有需要驳回的条目。
- `§2「QQQ 是成分股加权平均」` 的措辞确实不准确（QQQ 是 NDX-100 的平均，
  不是这 16 只的平均），结论正确但理由换掉了 —— 已改为「ETF 与单票动量方差不可比」。

## 待实施时核实（不阻塞设计定稿）

1. Supabase 对 `anon`/`authenticated` 的 `ALTER DEFAULT PRIVILEGES` 实际内容
2. PostgREST `merge-duplicates` 对 payload 中缺失列的行为
3. GitHub 60 天 cron 停用规则的当前措辞
4. Supabase 免费层：暂停阈值、PostgREST max-rows 默认值、备份覆盖
5. `numeric` 的 NaN 比较语义（仅影响可选的 CHECK 约束；Python 侧修复才是关键）

---

## 闸门 B — codex CLI（第 1 轮）

**8 项 serious，其中 6 项是闸门 A 的修复自己引入的回归。全部已修。**
这一轮证明了外部闸门的价值：它抓到的主要不是原设计的问题，而是**修复的问题**。

| # | 发现 | 处置 |
|---|---|---|
| C1 | **闸门 A 给出的 OLS 截距 SE 修正量级是错的**（说高估 10%–50%） | 已修 → 实算为 **+0.09% / +0.5%**。原文档照抄了错误量级，已更正并保留说明 |
| C2 | 「join 后日期相邻」的断言抓不到它声称要抓的缺 bar 错误（周一/周三缺周二，join 后仍可能相邻） | 已修 → §3.4 断言改到 join **之前**的各自序列上 |
| C3 | `running` 行若在那个「整天一个事务」里，崩溃时会一起回滚 —— 可观测性盲区没解决 | 已修 → §9.1.2 改为 T1/T2/T3 三个事务 |
| C4 | `create role ... password :'pipeline_pw'` 是 psql 变量语法，Supabase SQL Editor 不认 | 已修 → 密码在 Dashboard 手工设，迁移文件只做授权 |
| C5 | `metrics_daily` 没有 `updated_at` 列（它叫 `computed_at`），三表共用一个触发器会让迁移失败 | 已修 → §9.2.1 分开写 |
| C6 | `v_strength_enriched` 未按 `rank_pool`/`benchmark` 分区（跨配置变更编造名次变动），且没有 `grant select to anon` | 已修 → §9.1 视图按全部口径字段分区 + `security_invoker = true` + 显式 grant |
| C7 | §9.3.1 说把 `runs` 移到 `private`，但 §9.1 建表和所有授权仍写 `public.runs` | 已修 → 规范位置统一为 `private.runs` |
| C8 | 「只授 INSERT/UPDATE/SELECT」的正文与「grant ... delete on 五张表」的 SQL 自相矛盾，least privilege 被自己的修复削弱 | 已修 → 逐表动词；只有 `strength_daily` 有 DELETE |

Minor / nit 一并采纳：`above_ema60` 的「三个都要声明」自相矛盾（→ 改两个 + 说明边界）；
`check (close > 0)` 缺失使坏零值能绕过 `adj_factor` 比对（→ 补上）；
`alter default privileges` 只对执行者未来创建的对象生效（→ 加 `FOR ROLE postgres` 并固定迁移属主）；
NVDA 10:1 拆股的例子是错的 —— yfinance `auto_adjust=False` 的历史 `Close` **已做拆股调整**，
两列真正的差是**累计分红调整**（→ 改例子，并指出分红偏移「更危险，因为不会一眼看出来」）；
`FORCE ROW LEVEL SECURITY` 对非属主的 `pipeline_writer` 不增加防护只增加维护面（→ 删除）。

**判定为部分不成立的一条**：C4 里「identity 列需要单独 `grant usage` 序列权限」——
不成立。`generated always as identity` 的内部序列与 `serial` 不同，
表上的 INSERT 权限即可，无需单独授权。已在 §9.3 写明这一点
（这恰好是 §9.1 选 identity 而非 `bigserial` 的收益之一）。

## 闸门 B — codex CLI（第 2–6 轮）

codex 一共跑了 6 轮，直到返回 **"clean, no serious findings"**。
第 2 轮之后的每一条 serious **都是前一轮修复自己带出来的连锁后果** ——
这正是「重跑到干净为止」而不是「修完就走」的理由。

| 轮次 | serious | 主要发现 |
|---|---|---|
| 2 | 2 | `pipeline_writer` 的创建责任在 §8.2 与 §9.3 互相矛盾（迁移会直接失败）；`v_strength_enriched` 要求「按上一个交易 session 断开」，但 schema 里没有任何东西能让 SQL 判断 session 相邻（只有 `date`，Postgres 不知道感恩节）→ 新增 `trading_sessions(date, ordinal, is_half_day, close_et)` |
| 3 | 3 | 新表引发的连锁：它没进 §9.3 的 revoke/grant/RLS 与 §9.3.2 不变式范围；它缺运维契约（`ordinal unique` 不保证无缺口；谁填未来日期；日历修订怎么办）；视图仍是带省略号的伪 SQL 却声称要进 `0001_init.sql` |
| 4 | 2 | `pipeline_writer` 对新表没有任何权限 → 第一次生产运行会在闸门之前就失败；日历修订只重算派生层、**没有对账价格层** → 新增一个历史 session 却没有价格行，重算会在有缺口的序列上数「20 个 session」，全程无报警 |
| 5 | 1 | §8.1.1 的「只有 `strength_daily` 需要 DELETE」与 §9.1.4 的日历删+重插矛盾 |
| 6 | 0 | **clean** |

### 这一轮里 review 真正改变了结论的三处

1. **闸门 A 给出的 OLS 截距 SE 修正量级是错的**（声称高估 10%–50%）。
   实算 `sqrt(1 + 126×0.0005²/(125×0.012²)) = 1.0009`，即 **+0.09%**；
   第二组是 **+0.5%**。codex 独立复算并确认。**外部闸门抓到的是内部闸门的错误**，
   如果只跑闸门 A，这个错误会直接写进方法论页。
2. **拆股例子是错的**。yfinance `auto_adjust=False` 的历史 `Close` 已做拆股调整，
   两列真正的差是**累计分红调整** —— 量级小得多，但**因此更危险**：
   它足以把「距 EMA60 +0.8%」变成「-1.5%」而不会让任何人起疑。
3. **`trading_sessions` 这张表是被 review 逼出来的**。原设计里
   「相邻 session」这个判据在 Python 侧有（`pandas_market_calendars`），
   在 SQL 侧却无从计算 —— 而 `rank_delta_1d` / `days_in_top_n` 恰恰在 SQL 侧算。

### 未采纳（并说明理由）

- codex 第 2 轮建议把 §11.5 的双闸门收窄到「schema / 权限 / 指标 / 自动化」四类高风险变更，
  普通前端与文案降为「一次 review + CI」。
  **不采纳，因为这是用户自己定的流程，不由 review 来改。**
  已在 §11.5 原样记录该建议，供用户决定。

### 结论

设计稿 v2 通过闸门 A 与闸门 B。可以进入 M0。


---

# 第二批：用户决策 + 事件距离（v2 → v3）

闸门 A 1 组 agent（19 项）+ 闸门 A 1 组 agent（8 serious / 15 minor）
+ 闸门 B codex 第 7–10 轮（5 + 3 项），最终 **clean**。

## 用户决策（全部已落地）

SPY 不入池（17 标的）· 榜单降级为「又一个变量」而非全站主题 ·
Vercel 密码保护（Pro 付费，改自己用 middleware 实现）· 不建 staging ·
不做个股页 · 图表窗口半年 · 抓取要尊重限流 · 项目全称 market metrics。

## 这一批里 review 真正改变结论的五处

1. **`rs_vs_qqq_20d` 被删掉。** 它减的是当天全体共同的常数 → 排名与 `mom_20` **恒等**。
   作为尺子是空操作；而 §4.2 规定「换尺子必须断开连续计数」，
   于是切换它的**唯一可观测效果**就是把所有 `days_in_top_n` 清零 ——
   一个数学上的空操作造成一次用户可见的撒谎。
   同一论证下 §4.3 复合分里 `w1`/`w2` 不可分辨（z-score 对平移不变）。
   `delta_to_median` 有同样性质，故保留为展示列但禁止作排序键。
2. **事件列改为只写最新一行。** §3.0 规则 2 每天重写 400 行对复权因子是对的
   （追溯改写**追溯为真**），对事件**恰好相反** —— 财报改期不会让
   「10-01 那天的预告是 28 天后」变成假的。每天重写会让同一历史行
   今天显示 28、明天显示 35，永不稳定。改成只写最新行后，
   一次性消掉：614 天的垃圾值、滚动改写、以及「历史行带前视信息」那条免责声明本身。
3. **`period_key` 被推翻。** 我发明了一个供应商根本不提供的身份标识
   （月度分派、同季两次分红、改期跨季都会让它碰撞或漂移）。
   改用文档已用过两次的模式：**整窗删+插**，代理主键，无唯一约束
   （常规分红与特别分红可同一除息日）。
4. **`partial` 差点让一个装饰性指标污染核心数据。** 事件抓取失败原记 `partial`，
   而 `partial` 是 exit 1 且不写 `ok` 行 → §7.1 的条件重试**不跳过** →
   夏令时四跑全执行 → 重新武装了 §7.1 自己用粗体警告过的
   「好数据被 Stooq 静默覆盖」。改为 `ok_events_stale`（exit 0、不告警）。
5. **`days_since_last_earnings` 根本没有数据源。** `Ticker.calendar` 只给未来、
   `Ticker.dividends` 只给历史 —— 缺 `Ticker.earnings_dates`。
   它本会永远是 NULL 而没人发现。请求数也从 17 更正为约 51。

## 我自己引入又被抓回来的回归

- 触发器块一度插在 `prices_daily` / `metrics_daily` 建表**之前**（顺序错，迁移会失败）。
- `close` 改可空后，§7.2 闸门 4 的 `low <= close <= high` 与除法对 Stooq 行全是
  拿 NULL 运算 —— 会把一条设计好的降级路径变成**永久 `partial`**。
- 整窗删+插的窗口一度由「本次返回了什么」定义 →
  供应商返回**空集**时窗口未定义 → 已取消的未来事件永远留在库里。
  改为由端点契约给出 `coverage_start`。
- §9.3 的 writer RLS 策略一度只写了 `prices_daily` 一张 + 省略号。
  而本节就是 `0001_init.sql` 的正文 —— 照抄会导致其余五张表全部被 RLS 拒绝写入。
  现已六张全部写全。

## 结论

设计稿 v3 通过两道闸门。表从四张增至六张（+ `trading_sessions` + `symbol_events`
+ `private.fetch_state`），实现量 11–14 人日。可以进入 M0。
