# 利率政策：加息 / 降息信息上线计划（M16）

> 一句话：在首页顶部加一条**窄条**，只展示**已发生的事实**——联邦基金目标区间、
> 最近一次加息/降息、下一次 FOMC 会议、3 个月/2 年/10 年美债收益率。
> **不做**市场预期概率（见 §6）。
>
> 撰写日期：2026-11-23。状态：**已确认范围，实现中**（分支 `m16-rate-policy`）。
> 文中凡写「落地时发现」的，是实现期对初稿的修正，理由都写在原地。
> 闸门记录：[`reviews/M16.md`](./reviews/M16.md)。

## 1. 为什么做、为什么是这个范围

利率政策是市场状态的一部分，与 [`create-project.md`](./create-project.md) §1 的
「市场当下是什么状态」一致；它是**观测**，不是信号，不违反 §1.2 的任何一条非目标
（不回测、不给建议、不做盘中、前端纯只读）。

范围只取**事实**，理由是这个仓库最怕的那一类问题——**看起来完全正常的错数**：
目标区间、决议、收益率都有官方原始发布方，能对账；
而「下次降息概率」没有免费、权威、有 API 的来源，自己用期货反推时口径一错
（取哪个月的合约、按月内天数怎么加权）读数就是错的，用户还会当事实读。

## 2. 数据源（2026-11-23 逐个实测）

| 内容 | 来源 | 实测结果 | 凭证 |
|---|---|---|---|
| 目标区间（上/下沿）+ EFFR | 纽约联储 Markets Data API<br>`markets.newyorkfed.org/api/rates/unsecured/effr/search.json?startDate=…&endDate=…` | 200。每一行自带 `targetRateFrom` / `targetRateTo` / `percentRate`；2024-08-01…2026-11-19 共 577 行，无缺失 | **无** |
| 国债收益率 | 美国财政部 Daily Treasury Par Yield Curve（CSV）<br>`home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv` | 200。列 `Date,"1 Mo",…,"30 Yr"`，日期 `MM/DD/YYYY`，单位**百分数**（`4.63` = 4.63%），新的在前 | **无** |
| FOMC 会议日程 | 美联储官网 `federalreserve.gov/monetarypolicy/fomccalendars.htm` | 200。HTML，按年份分段，无 API | **无** |

**不用 FRED。** 本来的方案是 FRED 的 `DFEDTARU/DFEDTARL`，但实测发现纽约联储
的 EFFR 接口已经带着目标区间，且它是 EFFR 的**原始发布方**。少一个 API key、
少一个 secret、少一份第三方条款（FRED 禁止爬取、有数字限流且只在第三方页面见到过）。
FRED 留作后续可选的交叉校验，本期不接。

### 2.1 实测发现，必须进设计的四件事

1. **纽约联储的数据会修订。** 每行有 `revisionIndicator`。所以不能「只写新的一行」，
   要**整窗重写**（与 `create-project.md` §3.0 规则 2 同一个原则）。
2. **美联储页面里不是每一行都是「利率决议会议」。** 实测两种例外：
   2025 年的 `August 22 (notation vote)`，以及 **2027 年 10 月 26–27 日的
   `Monetary Policy Strategy Meeting`**（没有利率决定）。把它们当决议会议，倒计时就会
   指向一个不会宣布利率的日子。
3. **日程是暂定的，而且刚改过。** 页面写着「Each meeting date is tentative」；
   美联储在 2026-11-12 修订了 2027 年日程（取消 10 月的政策决定，12 月会议提前到
   11 月 16–17 日）。所以日程**不能写死进配置**，必须从官方页面抓，
   且改期/取消必须能传导到库里（整窗删+插，见 §4.3）。
4. **目标区间的下沿可以是 0。**（落地时发现）2022-03-16 之前是零利率时期，区间是
   `0.00–0.25`，EFFR 是 `0.08`。初版解析器要求所有利率 `> 0`，而 `history_start`
   默认 `2022-01-01` 会抓到这段 —— **整份响应被拒收，纽约联储这一路永远起不来**。
   对着 2024 年以后的数据测试时碰不到它，是用 2022 年的真实响应才复现的。
   同一发现的另一半：`(0, 25)` 的范围检查**拦不住小数形式**（`0.0388` 也在范围内，
   而零利率时期的 EFFR 本来就是 0.08），所以单位错误改由**行内一致性**拦
   （EFFR 必须落在目标区间 ±0.5 个百分点之内），范围检查只负责 `≥25` 与 `≤0`。
5. **收益率的单位错误比 EFFR 更难拦。**（落地时发现，闸门 A 抓到）财政部 CSV 一行里没有第二个
   可对账的数，`(0, 25)` 又拦不住小数形式（`0.0432` 照样在范围内），错数表现为「10Y 0.04、
   变动 0bp、2s10s≈0」—— 看起来完全正常。所以拿**另一个来源**当锚：同一跑里用纽约联储刚解析出的
   EFFR 去核对最新一天的 3 个月期收益率（容差 ±3 个百分点，只为拦单位错）；纽约联储这一跑
   失败时没有锚，跳过这道核对，由 `invariants.sql` 的事后断言兜底。
6. **生效日 ≠ 会议日。** 实测 2026-09-17 目标区间从 3.50–3.75 变成 3.75–4.00，
   会议是 9 月 15–16 日。页面只展示**生效日**（数据里有的），不去猜会议日。

### 2.2 再分发声明

纽约联储条款要求：使用或再发布参考利率数据时，必须带上下面这段声明（原文，2026-11-23 核对）：

> The [NAME OF DATA] is subject to the Terms of Use posted at newyorkfed.org.
> The New York Fed is not responsible for publication of the [DATA NAME] by
> [NAME OF PUBLISHER], does not sanction or endorse any particular republication,
> and has no liability for your use.

写进方法论页（`NAME OF DATA` = Effective Federal Funds Rate 与 target range，
`NAME OF PUBLISHER` = markme），并在首页窄条上留一个指向它的小链接。

## 3. 展示什么

窄条一行（窄屏折成两行），从左到右：

| 项 | 例子 | 来自 |
|---|---|---|
| 目标区间 | `3.75–4.00%` | 最新一行的 `target_lower/upper` |
| 上次变动 | `加息 25bp · 2026-09-17 生效（67 天前）` | `change_kind` 非空的最近一行 |
| 下次 FOMC | `12-08 ~ 12-09 · 15 天后`（会议第一天起写「进行中」） | `fomc_meetings` 里 `end_date > 今天` 的最早一条；天数数到**开始日**（落地时发现：初版数到结束日，会议第一天会显示「1 天后」） |
| 收益率 | `3M 4.32 · 2Y 4.63 · 10Y 4.92`，括号内为较前一交易日变动（bp）；`2s10s +29bp` | `treasury_yields` |

规则：

- **变动方向与幅度只看区间序列本身**：上沿和下沿同向同幅才判加息/降息；
  不一致就写 `未知`，**不猜**。
- **库里从未记录过变动**时写「自 <库里实际最早的日期> 起无变动」，**不用配置里的 `history_start`**，更不编日期。
  （落地时发现，闸门 A 抓到：初版写死 `history_start`，目标区间表为空而另两张有数据时，页面会断言一个
  没有数据支撑的事实。）**目标区间表为空时**，该项显示「暂无数据」，且**不画**「上次变动」——
  那不是「没变过」，是「不知道」。
- **没有已知的下次会议**时写「暂无日程」，不写大数字（同 `create-project.md` §3.5(2)）。
- 日期一律是**日历日**，沿用 `lib/market.ts` 的 `daysBetween`。
- **EFFR 不上窄条**（它是目标区间内的执行利率，放上去只增加噪音）；落库，供不变式用。

## 4. 设计

### 4.1 独立管道，不碰 `run_daily`

`pipeline/run_macro.py` + `.github/workflows/macro.yml`。**这是本设计最重要的一条**：
价格管道已经有一套精密的退出语义（`run_daily.py` 顶部那张表），
而利率是一个可选的装饰性板块——它的任何故障都不该有办法
（哪怕是间接地）改变价格与指标的状态码、重试或告警。
这与 `create-project.md` §3.5(4) 对事件抓取的结论同源，只是更彻底：
事件抓取仍在同一个进程里，利率则**根本不同进程**。

- 不写 `private.runs`（它的 `session_date` 语义与条件重试属于价格管道）。
- 新增 `private.macro_fetch_state(source, last_ok_at, last_attempt_at, last_error)`，
  只记「每个来源最近一次成功/失败」。
- 三个来源**互相独立**：一个失败不影响另外两个落库。
- 失败的来源**一律不动库里的旧数据**（不清空、不部分写）。

### 4.2 表（迁移 `0004_rate_policy.sql`）

```
rate_policy_daily(date pk, target_lower, target_upper, effr, change_kind, change_bp, source, updated_at)
treasury_yields  (date, tenor, yield_pct, updated_at)            pk (date, tenor)
fomc_meetings    (id pk, start_date, end_date, fetched_at)       索引 end_date
private.macro_fetch_state(source pk, last_ok_at, last_attempt_at, last_error)
```

- 利率列一律叫 `*_pct` / `target_*` 并**存百分数**（`4.63` = 4.63%）。这是
  `create-project.md` §3.4 讲过的 `^IRX` 同一类单位坑：差一个 `/100` 看起来就是
  「市场崩盘」。**存的单位与展示的单位相同，不在中间换算。**
- `change_kind`（`up` / `down` / `mixed`）与 `change_bp` 由管道在**整段历史**上算好再写（不是前端算）；
  `mixed` = 两沿变了但不同向或不同幅，bp 留空、**不猜方向**。库里有 CHECK 强制三者一致
  （`up ⇒ bp > 0`、`down ⇒ bp < 0`、`mixed ⇒ bp 为空`）。，所以前端没有第二份
  「怎么判加息」的实现，也就没有两份实现要对齐（`BACKLOG.md` 里 `dim_expr` 孪生的教训）。
- `fomc_meetings` 用代理主键 + 删+插，**不是**日期主键 —— 改期会产生孤儿行，
  与 §3.5(1) 同理。
- 权限沿用 0001 的模式：`anon` 只读 + RLS；`pipeline_writer` 才能写；
  `backup_reader` 已对 public/private 里的所有表有默认 select。

### 4.3 写入规则

| 来源 | 抓取窗口 | 写入 |
|---|---|---|
| 纽约联储 | `history_start`（默认 `2022-01-01`）…今天，**1 次请求** | 整窗 upsert；`change_bp` 在整段上重算 |
| 财政部 | 今年 + 去年，**2 次请求** | 整窗 upsert；只存 `config/macro.yaml` 里列出的期限 |
| 美联储日程 | 整页，**1 次请求** | 同一事务里：`delete from fomc_meetings where end_date >= <覆盖起点>` 再插入；**仅当解析与自检通过** |

共 4 次请求/跑，每天 2 跑。礼貌性：诚实的 User-Agent（含仓库地址）、超时 30 秒、
重试 3 次带退避，**复用 `pipeline/throttle.py`**（它已经是全仓库请求的预算与重试护栏），
只用标准库 `urllib`，**不新增任何依赖**。

### 4.4 解析必须「宁可拒收，不可猜」

美联储页面没有 API，只能解析 HTML，这是整个功能里最脆的一环，所以规则最严：

- 一行**当且仅当**日期格是 `d-d`（可带 `*`）或跨月的 `Jan/Feb  31-1` 形态才算会议。
- 明确识别并**跳过**两种非决议行：`notation vote`、`Strategy Meeting`。
- 出现**任何其他形状**的行 → **整次拒收**，旧数据原样保留，记 `last_error`。
- 自检：当年至少解析出 5 场；每场 `start <= end` 且跨度 ≤ 2 天；同一天不重复。不过 → 拒收。
- 拒收比误收便宜：前者的代价是日程停在旧版本（页面有陈旧提示），
  后者的代价是播报一场不存在的会议。

### 4.5 前端：**失败是软的**

与首页其余读取（`failClosed`）**相反**。理由有两个，都是实测过的教训：

1. 这条窄条是装饰性的。`0003` 迁移头部写过：代码先于迁移上线 → 前端多查一个不存在的
   列 → PostgREST 400 → `failClosed` → **每个访客拿到 500**。利率表若缺失/读失败，
   页面其余部分不能因此挂掉。
2. 部署顺序因此**不是硬性的**：迁移、合并、首次回填，任意先后页面都不会坏——
   表空或缺失时窄条整条隐藏。

软失败不等于静默：窄条自己要说话——
- 某来源距今超过阈值（目标区间 7、收益率 7、日程 14 个日历日；日程看 `fetched_at`，
  因为它说明「多久没重新抓过」，比会议自己的日期更说明问题）→ 该项旁标「数据已陈旧」。
  （落地时把初稿的 5/5/10 改成 7/7/14：美股最长的连续休市约 4 个日历日，7 天窗口
  不会被长周末误触发；阈值与不变式用同一组数，由测试绑在一起。）
- 整条读取失败 → 窄条位置显示一行「利率数据暂不可用」，而**不是**消失
  （消失会让「没有这个功能」与「它坏了」无法区分）。
- 但**表还不存在**（迁移未跑）与读失败在 PostgREST 看来都是错误；前者上线初期
  是预期状态。统一显示「暂不可用」，由 `/api/health` 之外的不变式负责告警（§4.6）。

### 4.6 不变式（`supabase/invariants.sql` 的 C 节，连库每天跑）

落地为 **5 条**（总数 22 → 27）。初稿列了 6 条，其中「`target_*` 与收益率在 `(0, 25)`」
已由表上的 `CHECK` 强制，再写一条断言是重复；「`change_bp` 与前一行之差一致」与
「`change_kind` 逐行一致」合并成一条。

| 断言 | 防什么 |
|---|---|
| 最新一行的 `effr` 在 `[lower − 0.5, upper + 0.5]` | `388` 或 `0.0388` 那种合法但离谱的单位错；故意放宽，因为资金面紧张时 EFFR 真会越出区间（2019-09 就越过） |
| `change_kind` / `change_bp` 必须能从相邻两行的区间**重新推出来** | 判方向的逻辑被改坏：区间没变却标了加息，或变了却没标 |
| 目标区间、收益率、日程都必须在陈旧阈值内（7 / 7 / 14 天）；**表空也算违规** | **管道静默停摆**（`create-project.md` §7.2.1：沉默才是最危险的状态）；「宏观管道从没跑过」与「它停了」一样需要有人看一眼 |
| 最新一天的 3 个月期收益率与最新 EFFR 相差不得超过 3 个百分点 | 财政部 CSV 悄悄改成小数形式（`0.0432`）—— 范围检查拦不住，且看起来完全正常 |
| `fomc_meetings` 必须有 `end_date >= 今天` 的会议，且无重复 | 日程耗尽（想要的：该去看一眼页面了）；整窗删+插只删不插；删没生效留下孤儿行 |

另外，「必需的授权不得消失」那条断言补了新表的 anon 读与 `pipeline_writer` 的动词。

### 4.7 排期

```
macro.yml
  cron "30 14 * * *"     # 美东 09:30/10:30：纽约联储 09:00 ET 发布前一营业日 EFFR
  cron "30 22 * * 1-5"   # 美东 17:30/18:30：财政部当日收益率曲线已出
  workflow_dispatch      # 手动，首次回填用
```

全部是幂等整窗写，多跑无害；`concurrency` 排队不取消（同 `daily.yml` 的理由）。
secret 只需要已有的 `MARKME_DB_URL`，挂在 step 级。

## 5. 实现清单

| 项 | 文件 |
|---|---|
| 配置 | `config/macro.yaml`（`history_start`、期限列表、陈旧阈值）+ `pipeline/config.py` 的 pydantic 模型 |
| 抓取与解析 | `pipeline/macro/`：`nyfed.py`、`treasury.py`、`fomc.py`（纯解析函数，不碰网络）、`config.py`、`errors.py` |
| 写库 | `pipeline/store.py` 增函数；`pipeline/run_macro.py` 入口 |
| 迁移 | `supabase/migrations/0004_rate_policy.sql` + `0004_rollback.sql` |
| 不变式 | `supabase/invariants.sql` 增 §4.6 的断言；`pipeline/schema.py` 的解析覆盖新迁移 |
| 前端 | `web/components/RatesStrip.tsx`（只画）、`web/lib/rates.ts`（全部判断逻辑，纯函数）、`web/lib/ratesData.ts`（读三张表共五个读、软失败）、`web/app/page.tsx` 接入、`web/lib/rates.test.ts`（`npm test`，CI 的 web job 跑） |
| 页面文案 | `web/app/methodology/page.tsx` 增「利率与政策」一节（来源、单位、日程暂定、生效日、§2.2 的声明、不含预期概率） |
| workflow | `.github/workflows/macro.yml` |
| 文档 | 根 `README.md` 计划清单登记；`docs/reviews/M16.md` |

### 测试

- **夹具取自实测响应**（裁短）：纽约联储 JSON、财政部 CSV、美联储 HTML——
  HTML 夹具必须含 `notation vote` 与 `Strategy Meeting` 两种例外行，以及一次改期。
- 解析：正常、跨月会议、两种例外行被跳过、**未知形状整次拒收**、空响应、列缺失、
  日期格式错、单位错（`388`）。
- 加息/降息判定：加息、降息、持平、上下沿不一致→未知、窗口首行无前值。
- 写库：整窗 upsert 幂等；日程改期**不留孤儿**；**抓取失败不清旧数据**；
  一个来源失败不影响另外两个。
- 前端：`rates.test.ts`（Node 内置 `node:test`，前端此前没有任何自动化测试）覆盖陈旧、
  加息/降息文案、倒计时、利差、bp 的浮点舍入；另外用本地假 PostgREST 跑过构建产物，
  四种态（正常 / 陈旧 / 表为空 / **表不存在**）页面都是 HTTP 200，价格表照常渲染。
- 变异测试：每条修复逐条还原，确认被对应测试抓到。

## 6. 明确不做

- **市场预期概率**（CME FedWatch 一类）。没有免费、权威、有 API 的来源；
  自己用联邦基金期货反推的口径风险见 §1。要做，作为单独里程碑，先实测口径。
- FRED 接入（见 §2）。
- 通胀、就业等其他宏观指标。
- 任何「因此应该买/卖」的文案。

## 7. 部署

1. 合并后不需要特定顺序（§4.5）。建议：先跑 `0004_rate_policy.sql`（SQL Editor，owner 身份）；
2. 合并 PR；
3. 手动触发一次 `macro` workflow，回填整段历史；
4. 刷新首页，窄条出现。

回滚：撤代码，再跑 `0004_rollback.sql`。
