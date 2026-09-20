# markme

**market metrics** —— 一个只读的、每日美股收盘后自动更新的指标看板。

观测 QQQ 与 16 只纳斯达克个股（共 17 个标的），计算 **RSI(14)**、**EMA(60)**、
**半年 alpha & beta（基准 QQQ）**、**财报与分红的前后距离**，
并给出当日**三强股**及其强度衡量参数。

**架构**：GitHub（代码 + Actions 调度）→ Supabase（Postgres 存储）→ Vercel（Next.js 前端）

> 状态：M0–M7 已实现并对生产库实跑。管道每个交易日收盘后约一小时自动更新。

*[English summary below](#english)*

## 快速开始

```bash
uv sync                      # Python 管道（需要 Python 3.13）
uv run pytest                # 全部测试
uv run ruff check . && uv run mypy

cd web && npm ci && npm run dev   # 前端
```

跑一次管道需要 `MARKME_DB_URL`（见 `.env.example`）：

```bash
uv run python -m pipeline.run_daily      # 每日增量（带四重闸门）
uv run python -m pipeline.backfill       # 全量回填整个窗口
uv run python -m pipeline.check_invariants   # 线上数据库的 19 条不变式
```

## 这个项目在防什么

整套设计围绕**一类特定的故障**：**看起来完全正常的错数**。
几个具体例子，每一条都在实现过程中真实发生过：

| 如果不管它 | 会怎样 |
|---|---|
| 只重写当天一行价格 | 供应商在除息日**追溯改写**全部历史复权因子，跨接缝的 `mom_20` 误差恰好等于股息率 —— 足以让第 3、4 名换位，而所有闸门全绿 |
| 降级时只补缺的那一行 | 那一行会落进 60 根 EMA 窗口与 126 日收益窗口，污染其后半年的 β 与 α |
| 闸门只断言基准有新 bar | 某只个股拿到昨天的 bar，于是三强榜是一个**混合日期的横截面**，并被「在榜 N 天」永久烤进历史 |
| 事件表用日期做主键 | 财报改期会留下一条作废的预告行，倒计时走向一个不存在的日子，到期后**播报一场从未发生的财报** |
| 事件抓取失败记成 `partial` | 条件重试不再跳过，夏令时那几跑全部重跑，而降级到备源的那一跑会**静默覆盖好数据** |

所以库里有 **19 条不变式**，每天连库跑一次；所有非有限值一律转 `NULL`
（绝不用 0 或上一日的值冒充）；任何一个指标窗口内的数据源必须唯一。

## 文档

| 文档 | 说明 |
|---|---|
| [AGENTS.md](AGENTS.md) | 仓库约定：目录规范、skills 位置、review 闸门、独立性 |
| [docs/create-project.md](docs/create-project.md) | 项目从零到上线的建设计划（指标口径、架构、数据库与 RLS、前端设计、里程碑） |
| [docs/reviews/](docs/reviews/) | 各里程碑的 review 闸门记录 |

### 计划文档清单

计划文档一律放在 `docs/`，每份用一个说明其内容的**具体文件名**
（小写 kebab-case，如 `create-project.md`、`add-sector-metrics.md`），
不使用 `workplan.md` / `plan.md` 这类通用名 —— 仓库里迟早会有多份计划，
通用名会立刻失去指向性。新增计划后在下表登记一行。

| 文档 | 说明 | 状态 | 更新 |
|---|---|---|---|
| [create-project.md](docs/create-project.md) | 项目建设计划 | v3 + 实现期按实测修正 | 2026-09-21 |

## 为什么站点有一道密码

站点前面有一道象征性的密码。**它不是安全措施，是合规与成本措施。**

- **数据源的再分发条款。** 行情数据来自 Yahoo（经 `yfinance`）。
  Yahoo 的条款禁止再分发其市场数据，`yfinance` 自身也把用途定位为个人研究。
  一个**可被公开检索的**数据站，和一个**只有我自己打开的**私人页面，
  在这件事上不是一回事。加一道密码让页面不被搜索引擎索引，
  这个（概率不高但真实的）暴露面就显著下降了。
- **免费额度。** 爬虫进不来，Vercel 与 Supabase 的免费额度就不会被刷爆。

需要说清楚的三件事，免得误解：

1. **这不是安全边界。** 真正的访问控制在数据库侧。
2. **密码不在仓库里**，它是 Vercel 的环境变量 `SITE_PASSWORD`；
   `.env.example` 里只有占位符。
3. 如果将来去掉密码、或站点产生实质流量，就该换到**许可允许再分发**的数据源
   （Tiingo / Polygon / EODHD），而不是继续靠「没人看得到」来规避。

## 免责声明

本站展示的是公开行情数据的统计派生量，**不构成任何投资建议**。
数据来源与口径见站内方法论页。


---

## 目录

```
config/       四个 YAML —— 标的池、指标、强度、运行参数（前端构建期直读）
pipeline/     Python 管道：抓取 → 闸门 → 计算 → 三事务写入
supabase/     0001_init.sql + 回滚脚本 + invariants.sql（19 条）
web/          Next.js 前端（Server Components + ISR）
.github/      五个 workflow：daily / backfill / ci / keepalive / heartbeat
```

## 一件值得单独说的事

这个项目的每个里程碑都过两道 review 闸门（见 `docs/reviews/`）。
到 M7 为止，闸门一共抓到 **18 条 SERIOUS**，而其中**大部分不在功能代码里，
而在验证手段里** —— 断言换个角色跑就失明、守卫写在 `WHERE` 里对无 `GROUP BY`
的聚合是死代码、缺凭证时整组测试静默跳过而 CI 全绿、解析器答错而不是报错。

它们的共同点是：**坏掉时的症状是「一切正常」。**
这也是本项目为什么在不变式、闸门和诚实标注上花掉了远多于「画图表」的篇幅。

---

<a id="english"></a>

## English

**markme** (market metrics) is a read-only US-equity indicator dashboard that
updates automatically about an hour after each market close.

It tracks QQQ plus 16 Nasdaq stocks (17 symbols) and computes **RSI(14)**,
**EMA(60)**, **half-year alpha & beta against QQQ**, and **days to/since the next
and last earnings and dividend**, along with a daily **top-three** ranking and the
parameters that describe how solid that ranking is.

**Stack**: GitHub (code + scheduled Actions) → Supabase (Postgres) → Vercel (Next.js).

### What this project is defending against

The whole design is organised around one failure class: **numbers that are wrong
but look completely normal.** Every example below actually happened during
implementation, not in theory:

- Vendors **retroactively rewrite** adjustment factors on every dividend, so an
  incremental pipeline that only writes today's row produces a seam whose error
  equals the dividend yield — enough to swap ranks 3 and 4 with every gate green.
- A single row patched in from the fallback source lands inside a 60-bar EMA window
  and a 126-day return window, corrupting beta and alpha for the next six months.
- yfinance's `Adj Close` is only stable to float32, so naive change-detection
  rewrites ~8% of rows forever and the adjustment-factor alarm fires every run.
- `Ticker.calendar` and `Ticker.earnings_dates` describe *the same* next earnings
  one day apart; storing both makes the countdown announce an earnings that never happens.

Hence **19 database invariants** run against production daily, a hard rule that
non-finite values become `NULL` (never 0, never yesterday's value), and a
requirement that any one metric window uses exactly one data source.

### The password

The site sits behind a symbolic password. **It is not a security boundary** —
the repository is public and the real boundary is Postgres GRANT + RLS. It buys
two concrete things: the page is not indexable (which matters because the data
vendor's terms forbid redistribution) and crawlers cannot burn the free tier.

### Licence

MIT — see [LICENSE](LICENSE).
