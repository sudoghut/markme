# markme (market metrics) — 项目建设计划 (Create Project)

> 计划文档一律放在 `docs/`，每份计划用一个说明其内容的具体文件名
> （本文件 = `docs/create-project.md`，即项目从零到上线的建设计划）。
> 索引见根目录 [`README.md`](../README.md)。

> **一句话定位**：一个只读的、每日美股收盘后自动更新的指标看板。
> 观测 low-buy 的同一批标的，计算 RSI(14) / EMA(60) / 半年 alpha & beta (vs QQQ)，
> 并给出当日「三强股」及其强度衡量参数。
>
> 架构：**GitHub (代码 + Actions 调度) → Supabase (Postgres 存储) → Vercel (Next.js 前端)**
>
> 文档状态：设计稿 **v2**（v1 经两组 review agent 复核，19 项 serious 已全部处置，
> 记录见 [`reviews/plan-v1.md`](./reviews/plan-v1.md)）。待 §12 决策确认后进入实施。
> 撰写日期：2026-09-20

---

## 1. 范围与非目标

### 1.1 做什么
- 每个交易日收盘后自动抓取全 universe 的日线数据（收盘价口径）。
- 计算并存储一组**可配置的**指标：RSI(14)、EMA(60)、半年 alpha / beta（基准 QQQ）。
- 计算并存储**当日三强股**及其强度衡量参数（见 §4）。
- 在公开网页上以专业金融看板的形式展现，并提供完整的口径说明页。

### 1.2 明确不做（本期）
- **不做历史研究、不做回测、不做长期历史展示。**
  本项目回答的是「**市场当下是什么状态**」，不是「过去发生过什么」。
  这条定位是所有取舍的源头：它决定了回填只到「够算出今天」为止（§12 #5）、
  决定了不做个股详情页（§10.2）。
  注意它约束的是**回填深度**（只到够算出今天为止），**不是保留策略** ——
  既然存量可忽略（§8.6），历史行一律不删，写入角色对价格/指标表也没有 DELETE 权限。
- 不做策略信号、不做下单建议。本项目是**观测层**，不是决策层。
- 不做盘中/实时刷新。只有 EOD（收盘）一个数据点。
  周末与假日显示最近一个交易日的数据（周日看到的是周五）。
  陈旧告警的**触发点**是日历日、**文案**报的是交易日数，两个单位各管一件事
  （触发看「过了多久」，文案说「漏了几根」）—— 理由见 §10.6。
- 不做用户系统、不做自选股。前端**纯只读** ——
  唯一的写入型交互是 §10.5 的象征性密码表单，它不产生任何用户数据、也不写数据库。
- 不做个股详情页。单页仪表盘一屏看完当下状态（§10.2）。
- **不依赖 low-buy 的任何代码、数据或运行环境。** 两个仓库完全独立，
  markme 自己抓数据、自己算指标、自己存储。唯一的「借用」是标的清单这一
  份事实，而它会被硬拷贝进 `config/universe.yaml`（见 §2）。

### 1.3 与 low-buy 的口径差异（必须写进方法论页）
low-buy 是「在**今日开盘**做决策」的框架，所以它的排名列是 `Mom_20_prev`
（`prev_C.pct_change(20)` = `C[i-1] / C[i-21] - 1`）。markme 是「**今日收盘后**观测」，
按你的要求全部用收盘价，所以 markme 的 `mom_20 = adjC_t / adjC_{t-20} - 1`。

两者跨越**相同的 20 个交易日**，只差一天的偏移。但差异不止这一条：

| 维度 | low-buy | markme |
|---|---|---|
| 决策时点 | 今日开盘（截至昨收的信息） | 今日收盘 |
| 价格口径 | 原始 vendor `C`（已拆股调整，**分红调整存疑**） | 完整 `adj_close` |
| 数据源 | `../financial-data/` CSV | yfinance / Stooq |

low-buy 自己的归档审计（`stocks/studies/archive_inventory/`）明确写了
「Dividend adjustment is a weaker claim」并记录了 QQQ 对另一供应商 **17.0%** 的偏离
—— 那是缺失分红调整的典型特征。20 个交易日尺度上这个差约 0–15bp，
听着很小，但和 §4.2 里那个 0.1pp 的 `delta_to_next` 是同一个数量级。

→ **不要只说「会对不上」，要给一条可证伪的等价命题**（方法论页写这句）：
**markme 第 t 日的榜单，应当等于 low-buy 第 t+1 日 `live_today` 的榜单**，
因为两者都在对 `C_t / C_{t-20}` 排序。
这把「为什么对不上」从一次开放式排查变成一个两行的测试。

---

## 2. Universe（标的池）

来自 low-buy `core/data.py` 的事实快照（拷贝，非依赖）：

| 类别 | 标的 | 数量 |
|---|---|---|
| ETF（基准） | `QQQ` | 1 |
| 个股 | `AAPL AMD AMZN ASML AVGO COST GOOGL INTC META MSFT MU NFLX NVDA ORCL TSLA TSM` | 16 |
| **合计** | | **17** |

设计要点：
- `QQQ` 同时是 **universe 成员**和 **alpha/beta 基准**，两个角色在 config 里分开声明。
- **`SPY` 不入池**（§12 #2）。它在 low-buy 里本来就是 backup-only，
  而本项目的基准是 QQQ —— 放一个既不当基准、又不参与排名、也没人看的 S&P ETF 进来，
  只会让全池表格多一行噪音。想要的话 `universe.yaml` 加一行就回来了。
- **三强股的排名池默认只含 16 只个股，不含 ETF。** 理由不是「QQQ 排不进前三」
  （QQQ 是 NDX-100 的加权平均，不是这 16 只的平均，这 16 只只是它的子集），
  而是 **ETF 与单只个股的动量方差不可比** —— 把一个天然低波动的组合和 16 只单票
  放进同一个降序排名，等于用一把刻度不同的尺子量两样东西。
  这一点在 `config/strength.yaml` 里用 `rank_pool: stocks` 表达，可改成 `all`。

---

## 3. 指标定义（精确口径）

### 3.0 价格口径与复权因子（**本章最重要的一节**）

**所有计算使用日线收盘价，且一律使用复权收盘价 `adj_close`。**
唯一的例外是「最新价」这一个展示字段用未复权的 `close`（用户肉眼对照行情软件时看的是它）。
下文所有公式一律写作 `adjC`，不写 `C`，避免抄到方法论页时产生歧义。

三条必须写死的不变式 —— 违反任何一条都会产出**看起来正常但静默错误**的数字：

1. **不得混用两种价格单位。** `close_vs_ema60_pct` 这类派生列的分子分母必须同源：
   `adjC / ema_60(adjC) - 1`。**绝不能**写成 `close / ema_60 - 1`。
   > 口径澄清（初稿这里举错了例子）：yfinance 在 `auto_adjust=False` 下返回的
   > 历史 `Close` **通常已经做过拆股调整**，`Adj Close` 额外做的是**分红调整**。
   > 所以两列的差不是拆股造成的 10× 断崖，而是**累计分红调整**造成的持续性偏移 ——
   > 对 AAPL / MSFT / COST 这类长期分红股，三年窗口上可达几个百分点。
   > 这个量级不会一眼看出来，**因此比 10× 断崖更危险**：它足以把
   > 「距 EMA60 +0.8%」变成「-1.5%」，却不会让任何人起疑。
   > **已实测确认（M0）**：NVDA 在 2024-06-10 的 10:1 拆股前后，
   > 原始 `Close` 为 120.99 → 121.79，**没有 10× 断崖** —— 供应商的 `Close` 确实已做拆股调整。
   > 两列的差只剩分红调整（AAPL 约 0.9%），正如上文所说，量级小反而更危险。
   → 单元测试：合成一条含分红调整的序列，断言 `close_vs_ema60_pct`
   与「全程用 adj_close 计算」的结果逐点相等；再合成一条 2:1 拆股序列，
   断言该列**跨拆股日连续**（这条测的是我们自己的实现，与供应商口径无关）。

2. **复权因子会被供应商追溯改写。** `adj_factor_t = adj_close_t / close_t`，
   而 `adj_close_t = close_t × Π(t 之后所有事件的因子)`。每一次分红或拆股都会
   **重写全部历史**。所以「只写今天这一行」的增量管道必然在某天产生一道接缝：
   MSFT 除息当日，新抓的近期行带新因子，库里三年老行还是旧因子，
   跨接缝算出的 `mom_20` 误差恰好等于股息率 —— 足以让第 3、4 名换位，
   而 §7.2 的任何状态码都不会触发。
   → **规则：每次日常运行都重抓并重写整个回看窗口（`lookback_bars = 400`，
   见 §12 #5），而且是价格层、指标层、榜单层**三层一起**，不是只写当日一行。**

   | 层 | 每日重写范围 |
   |---|---|
   | `prices_daily` | 窗口内全部 400 行 × 17 标的 |
   | `metrics_daily` | **同样 400 行**（否则这条规则买不到任何东西） |
   | `strength_daily` | 同样日期范围，**按日期删+插，全池重排** |

   > 只重写价格层是无效的：损害发生在指标层。老的 `metrics_daily` 行仍然带着
   > 除息前的复权基准，而 `ema60_slope_20d = ema_60 / lag(ema_60, 20) - 1`
   > 会拿一个新基准的 EMA 去比一个 20 天前旧基准的 EMA —— 所有闸门全绿。

   同时把 `adj_factor` 落库，日常运行比对「昨日行的因子是否变了」，变了就触发修复。
   > **这个探测器只对 yfinance 有效。** Stooq 行的 `close` 是 `NULL`，
   > 生成列 `adj_factor` 随之为 `NULL` —— 不会报错，但也比不出任何东西。
   > 实现时不得把「`NULL` 比 `NULL`」当成「没变」。
   > 用备源期间改用 `adj_close` 逐行比对。
   > **注意这只是诊断，不是正确性机制** —— 正确性来自上面那条「每天重写整窗」，
   > 它与探测器是否工作无关。
   > **修复必须是全池的，不能按标的。** `backfill.yml` 虽然接受 `symbols` 参数，
   > 但排名是**横截面**的：改写 AVGO 400 个历史日的 `mom_20`，
   > 同时改变的是 AVGO 的**名次**以及**其余每一只**在那些日期上的
   > `delta_to_next` / `delta_to_median` / `in_top_n`。
   > 单标的回填在物理上修不好 `strength_daily`。
   > 所以：价格/指标层可以按标的修，**榜单层必须对受影响日期范围整体重排**。
   > （§9.1.4 的日历修订路径已经写对了这一点，复权因子路径要继承同一条规则。）

3. **一个指标窗口内不得混用数据源，且两个源写入哪一列必须写死。**
   Stooq 的 `close` 本身就是已复权的、且没有独立的复权列；
   yfinance（`auto_adjust=False`）给的是未复权 `close` + `Adj Close`。
   schema 有两个列，而这两个源给的列数不一样 —— **映射不定义清楚，就会同时坏三件事**：

   | 源 | `close` | `adj_close` |
   |---|---|---|
   | yfinance | 供应商 `Close`（未做分红调整） | 供应商 `Adj Close` |
   | **Stooq** | **`NULL`** | 供应商 `close`（已复权） |

   → `prices_daily.close` 必须**去掉 `NOT NULL`**（`adj_close` 保留 `NOT NULL`，
   因为所有指标都用它，它一定有值）。
   前端的「最新价」在 `close IS NULL` 时显示为「复权价（备源）」并加角标。

   > 若图省事把 Stooq 的价格同时写进两列，会同时发生：
   > ① §3.0 开头那句唯一的例外（「最新价用未复权 `close`，因为用户要对照行情软件」）
   > 对每一只分红股都悄悄变成复权价 —— 这就是 §13 的「口径对不上 → 信任崩塌」，
   > 只不过是从备源这条路走进来的；
   > ② 生成列 `adj_factor = adj_close / close` 恒等于 1.0，
   > 于是规则 2 的「因子是否变了」探测器在每次换源时**误报**、在用备源期间**失明**；
   > ③ 规则 4 的 CI 不变式（某分红股某行 `close != adj_close`）会失败 ——
   > 所以那条不变式必须限定 `source = 'yfinance'`，否则它测的是「哪个源跑了」，
   > 而不是「`auto_adjust` 有没有设对」。
   若 yfinance 某天限流、只有那一行降级到 Stooq，这一行会落进 60 根 EMA 窗口和
   126 日收益窗口里，接缝两侧的日收益各自错一整个累计复权差，
   并污染其后 126 个交易日的 `beta` / `resid_vol` / alpha t 值。
   → **规则：降级时必须用备源重抓整个窗口，绝不单行拼接。**
   运行时断言：任一指标窗口内 `prices_daily.source` 必须唯一，否则该指标写 `NULL` +
   标 `provisional` + 状态 `partial`。

4. **yfinance 必须显式 `auto_adjust=False`。** 新版 yfinance 默认 `auto_adjust=True`，
   此时返回的 `Close` 就是复权价且没有 `Adj Close` 列 —— 两个 `NOT NULL` 列会被写入
   同一个数字，「最新价」对任何有分红/拆股历史的股票都对不上券商软件，
   正好落进 §13 的「指标口径与行情软件对不上 → 信任崩塌」。
   → CI 不变式：至少一只分红股在某个 `source = 'yfinance'` 的历史行上
   必须满足 `close != adj_close`。
   > **已实测确认（M0，yfinance 1.7.0）**：`auto_adjust` 默认确为 `True`；
   > 显式传 `False` 后仍返回独立的 `Adj Close` 列；
   > AAPL 在 2024-05-28–2024-06-13 的 13 行上 `Close` 与 `Adj Close` **全部不同**。
   （`check (close > 0)` 这类约束抓不到它，约束必须是关于两者**差值**的。）

### 3.1 RSI(14) — Wilder 平滑
```
delta_t  = adjC_t - adjC_{t-1}
gain_t   = max(delta_t, 0);   loss_t = max(-delta_t, 0)
seed     : AvgGain_14 = SMA(gain_1..gain_14),  AvgLoss_14 同理
recursion: AvgGain_t = (AvgGain_{t-1} * 13 + gain_t) / 14   (loss 同理)
RS_t     = AvgGain_t / AvgLoss_t
RSI_t    = 100 - 100 / (1 + RS_t)
```
边界与约定：
- `delta_0` 不存在，所以第一个 gain 是 `gain_1`，**第一个 RSI 落在第 15 根收盘价上**
  （需 15 根 bar）。这是每一次 RSI 重实现都会犯的经典 off-by-one，显式写出来。
- `AvgLoss = 0 且 AvgGain > 0` → `RSI = 100`；`AvgGain = 0 且 AvgLoss > 0` → `RSI = 0`。
- **两者皆 0（窗口内连续平盘 / 停牌）→ 写 `NULL`，不写 50。**
  一个合成的 50 是在「没有信息」的地方画一个中性且自信的数字，
  与 §10.4「绝不用 0 或上一日的值冒充」是同一类错误。
- **预热**：Wilder 递归是无限脉冲响应，理论上永不收敛。语义见 §3.3：
  `min_bars: 15`（低于此写 NULL），`provisional_below: 250`（低于此出值但标灰）。
- 常见坑 ①：`ewm(span=14)` 算出来的**不是** Wilder RSI（那是 alpha=2/15，
  Wilder 是 alpha=1/14）。
- 常见坑 ②：`ewm(alpha=1/14, adjust=False)` 用**首值播种**，与上式的 SMA(14) 播种
  **不是同一个函数**。差异按 `(13/14)^k` 衰减（236 根后约 2.5e-8，生产中可忽略），
  但一条针对短夹具、对标手算 SMA 播种值的 golden 测试**一定会失败**，
  且失败看起来像公式错而非播种错。
  → **实现与测试都以显式递归为准**，`ewm` 形式仅作为交叉验证且只在 ≥250 根时比对。

### 3.2 EMA(60)
```
alpha  = 2 / (60 + 1) = 0.032787
seed   : EMA_59 = SMA(adjC[0:60])
EMA_t  = alpha * adjC_t + (1 - alpha) * EMA_{t-1}
```
- **展示策略**：`min_bars: 60`，`provisional_below: 180`（某一行灰不灰）。
- **喂入要求**：计算时需 ≥ 360 根（约 6×N）使 SMA seed 的影响衰减到可忽略。
  **这个 360 是 `lookback_bars` 的真正下界**（§12 #5 标准 B），比 RSI 的 250 更紧。
  它与上面的 180 不冲突 —— 一个说「喂多少」，一个说「灰不灰」，是两件事。
- 派生展示列（这些才是人真正要看的，裸 EMA 数值没有可比性）。
  **下面两个必须在 `metrics.yaml` 里声明并入库**，否则会出现
  「§3 定义了但没人算、也没地方存」的空洞：
  - `close_vs_ema60_pct = adjC / ema_60 - 1` ← **前端主展示列**（注意是 `adjC`，见 §3.0 规则 1）
  - `ema60_slope_20d = ema_60_t / ema_60_{t-20} - 1` （趋势方向，需要独立列）

  `above_ema60` **既不入库也不进 config**，它是纯前端的 `close_vs_ema60_pct > 0`。
  （§6.2 的双向 CI 校验只覆盖「入库的指标」，所以这个边界要在这里说清楚。）

### 3.3 预热字段的统一语义（`min_bars` / `provisional_below`）

两个字段在全项目只有一种含义，pydantic schema 强制 `min_bars ≤ provisional_below`：

| 字段 | 含义 |
|---|---|
| `min_bars` | **低于此根数一律写 `NULL`**（硬闸门，不出值） |
| `provisional_below` | 低于此根数**出值但标 `provisional`**（软闸门，前端灰标） |

（初稿里 RSI 写成 `min_bars: 250` / `provisional_below: 150`，两者反了：
250 的硬闸门会让 150 的软闸门永远不可达，且 150–250 区间无任何规定。
这条不变式进 §11 M1 的配置校验验收标准。）

**`provisional` 是按指标而非按行的**：一个只有 170 根 bar 的标的，
RSI 可信而 EMA 尚未收敛。所以 `metrics_daily` 存 `provisional_metrics text[]`
（而非一个行级 boolean），前端按单元格打灰标。

### 3.4 半年 alpha / beta（基准 QQQ）
```
窗口 W   = 126 个日收益（因此需要 127 根 bar），以最新收盘日为右端点
r_s,t    = adjC_s,t / adjC_s,t-1 - 1        (标的日简单收益)
r_b,t    = adjC_QQQ,t / adjC_QQQ,t-1 - 1    (基准日简单收益)
rf_daily = 见下「无风险利率」            (config，默认常数 0.0)

OLS:  (r_s - rf) = alpha_d + beta * (r_b - rf) + eps
beta         = Cov(r_s - rf, r_b - rf) / Var(r_b - rf)
alpha_d      = mean(r_s - rf) - beta * mean(r_b - rf)     [日频 Jensen alpha]
alpha_annual = alpha_d * 252                              [线性年化 —— 默认]
```

**年化必须用线性，不能用复利**（初稿默认写反了）。`alpha_d` 是回归**残差的均值**，
不是一条真实的复利收益路径；把算术均值 252 次方是把算术均值当几何均值用，
且高估幅度随估计值单调放大。Jensen / Carhart / Fama-French 的标准做法都是把
回归截距**线性**乘以期数。

> 量级：`alpha_d = 0.5%/日` → 线性 **+126%**，复利 **+252%**（翻倍）；
> `alpha_d = 1.0%/日`（NVDA 在 2023 某个 126 日窗口可达）→ 线性 **+252%**，
> 复利 **+1130%**（4.5 倍）。前者只是乐观，后者是把散点图的纵轴变成胡说。
> 极端情况下复利式还会**直接写崩**：`alpha_d ≳ 0.04` 时结果超过
> `numeric(10,6)` 的上限 9999.999999，Postgres 中止整批 upsert → 全天 `failed`。
>
> `compound` 保留为 config 可选项但默认关闭；方法论页注明当前生效的是哪一种。
> 另注：无论哪种，|t|>2 的显著性带是在 `alpha_d` 上算的，
> 所以复利变换下展示出来的置信区间是**不对称**的 —— 这也要写一句。

同时存储的伴随统计量（没有它们，alpha/beta 是不可解读的裸数字）：
- `r2` — 决定系数。R² < 0.3 时 beta 本身就不可信，前端需降权显示。
- `corr` — 相关系数。**免费不变式：`r2 == corr²`**（简单回归下精确成立），
  写成一条断言就能白捡一整类符号 / 索引错误的检测。
- `resid_vol_annual` — 残差波动率年化（= tracking error）。
  `resid_vol_daily = sqrt(SSR / (n - 2))` —— **`ddof` 必须是 `n-2`**（估了两个参数），
  用 `n-1` 会把标准误低估 `sqrt(124/125) ≈ 0.4%`，方向上和下面的 t 值高估叠加。
- `alpha_t_stat` — **关键**：没有 t 值的 alpha 基本等于噪音。
  精确的 OLS 截距标准误是
  ```
  SE(alpha_d) = s * sqrt(1/n + x̄² / ((n-1) * s_x²))
              = (s / sqrt(n)) * sqrt(1 + n·x̄² / ((n-1) * s_x²))
  其中 s = resid_vol_daily = sqrt(SSR / (n-2))
  ```
  常见的简化式 `alpha_d / (s / sqrt(n))` 漏掉了后面那个修正因子。
  **这个因子在本项目的取值范围内其实很小**：`n=126, x̄≈0.0005, s_x≈0.012` →
  `sqrt(1 + 0.00175) ≈ 1.0009`（+0.09%）；即便强趋势半年
  `x̄≈0.001, s_x≈0.010` → `≈ 1.005`（+0.5%）。
  > 这一条在上一轮 review 里被说成「高估 10%–50%」并写进了文档，**那个量级是错的**，
  > 已按实际算术更正。保留在这里是因为它正是 |t|=2 硬切时最容易被夸大的那类论证。
  - 所以：结论不是「近似式会毁掉显著性判定」，而是「精确式不花钱，没有理由用近似式」。
    → 实现直接用 `statsmodels.OLS(...).tvalues[0]`；方法论页贴精确式。
  - **真正会实质影响 |t| 的是下面的 `ddof` 与多重比较两条，不是这个修正因子。**
- `n_obs` — 实际参与计算的样本数。

约定：
- **日期对齐：先各自在自己的连续收盘序列上算日收益，再对「收益序列」做 inner join。**
  绝不能先 inner join 价格再做差分 —— 若某标的缺一天（供应商漏数据、停牌、新上市），
  跨缺口的那一段会被当成单日收益，量级约 √2 倍偏大，推高 `Var(r_s)` 并拖动
  `beta` 与 `resid_vol`；而 `n_obs` 仍会读到 125，安全地高于 `min_obs: 120`，
  没有任何东西会报警，接下来半年所有人看到的都是一个貌似合理的错 beta。
- **断言必须打在 join 之前的各自序列上，不能打在 join 之后。**
  设某股票有周一和周三、缺周二：它「周三的日收益」实际是两个 session 的收益。
  join 之后剩下的日期可能是周三、周四 —— 在交易所日历上**相邻**，
  于是一条「join 后日期相邻」的断言会**放行**，而那个两日收益照样进了 OLS。
  → 正确的做法：**对每一条收益，校验它的价格日与其前一个价格日在交易所日历上相邻**
  （§7.2 已加载 `pandas_market_calendars`，直接复用）；
  不合格的收益在 join **之前**剔除（或直接把该标的该指标判为 `NULL`）。
  同一条校验也保护 `mom_20` —— `adjC_t / adjC_{t-20}` 只有在「20 行前确实是 20 个
  session 前」时才配叫「20 个交易日动量」。
- `n_obs < 120`（126 的 95%）→ 该行 alpha/beta 写 `NULL`，不写近似值。
- **基准对自己必须特判。** QQQ 在 universe 里（§2），所以这个退化回归每天都会在生产里算一次：
  残差恒为 0 → `resid_vol_daily = 0` → `alpha_t_stat = 0/0 = NaN`。
  NaN 会一路漏下去并**朝失败开放的方向**出错：`dim_when: "abs(alpha_t_stat) < 2"`
  对 NaN 求值为 false，于是 QQQ 那一行**不打灰**，即 UI 宣称一个「统计显著」的
  0.00% alpha —— 恰好是真相的反面，还发生在全站的基准行上；
  而 Postgres 的 `NaN` 排序**大于一切数字**，`order by alpha_t_stat desc` 会把 QQQ 顶到第一。
  → 规则：`symbol == benchmark` 时直接写
  `beta=1, alpha=0, r2=1, corr=1, resid_vol=0, alpha_t_stat=NULL`，不跑回归。
  同时这也是那条硬单元测试（QQQ 对自己 → β=1、α=0、R²=1）。
- **多重比较**：16 个非基准标的 × 双侧 |t|>2，纯靠运气每天就会产生约 **0.8 个**
  「显著」的 alpha；且日频股票残差有波动率聚集，普通 OLS 标准误偏乐观（未上 HAC）。
  → 方法论页写一句多重比较说明，或把展示阈值提到 |t| > 2.8（16 次检验的 Bonferroni 量级）。
  §10.4 承诺了「诚实优先于漂亮」，一个平均每天靠运气亮一次的「显著」徽章会侵蚀它。
- **无风险利率**：默认 `rf = 0`，方法论页注明「本站 alpha 为 rf=0 口径的 Jensen alpha」。
  注意 `rf` 为**常数**时它在 `Cov`/`Var` 里完全抵消，对 beta 毫无影响 ——
  所以将来 §12 #4 切到 `^IRX` 时 beta 会变，那是正常的，不是数据 bug，要提前写明。
  config 的形状**现在就要按时间序列设计**，否则到时候得改结构：
  ```yaml
  risk_free: {mode: constant, annual: 0.0, symbol: ^IRX, units: percent}
  ```
  （`^IRX` 报的是**百分数**，如 `5.25` = 5.25%，必须先 `/100` 再 `/252`。
  漏掉 `/100` 会得到 2.08%/日 的 rf 和一个灾难性的负 alpha ——
  看起来像市场崩盘，实际是单位 bug。）

### 3.5 事件距离：财报与分红的前后距离（4 个参数）

```
days_to_next_earnings     距下一次财报
days_since_last_earnings  距上一次财报
days_to_next_dividend     距下一次分红（除息日）
days_since_last_dividend  距上一次分红（除息日）
```

这四个与前面所有指标**不是一类东西**：它们不是收盘价的函数，而是**日历事件**的函数。
由此有五条必须提前写死的规则，缺一条都会变成静默错数。

#### (1) 存事件日期，天数由它派生；**主键是事件身份，不是事件日期**

表定义在 §9.1（它属于 `0001_init.sql`，本节只解释为什么）。关键是主键：

```
id 代理主键 + (symbol, event_type, event_date) 索引   -- 注意：索引，不是唯一约束
```

**不要给它找自然主键。** 供应商不提供稳定的事件 id，
而 `(symbol, event_type, event_date)` 也不唯一 —— 常规分红与特别分红可以同一个除息日。
（初稿曾想用 `period_key` = 财季度，那是**发明一个数据源根本不给的身份** ——
月度分派、同季两次分红、或改期跨季度，都会让它碰撞或漂移。）

正确的做法是本文档已用过两次的那个模式：**抓取成功后整窗删+插**
（同 `trading_sessions` §9.1.4 与 `strength_daily` §9.1.2）。
表用代理主键 `id`，**幂等性来自删+插而不是主键冲突**：

**覆盖窗口必须由端点本身定义，不能由「本次返回了什么」定义。**
这是一个容易漏的边界：若供应商本次返回**空集**（财报被取消、或该标的本季不报），
「本次最早日期」是未定义的 —— 于是删除语句无从下手，
那条**已作废的未来事件会永远留在库里**，倒计时继续走向一个不存在的日子。

```sql
-- coverage_start 由端点契约给出，与本次返回了几行无关：
--   earnings ：current_date - interval '400 days'   （earnings_dates 约覆盖前后各 4 次）
--   dividend ：sessions_start_date                  （Ticker.dividends 给全历史）
-- 在同一事务里，且仅当该标的该端点的抓取**成功**时执行 ——
-- 即便本次要插入 0 行，delete 也照常执行：
delete from symbol_events
 where symbol = $1 and event_type = $2
   and event_date >= $coverage_start;
insert into symbol_events ... ;        -- 本次全量，可能是 0 行
```
改期于是根本不会产生两行：旧的那行在插入前已被删。
**抓取失败时整个 delete+insert 都不执行**，所以半次失败的抓取不会抹掉好数据；
`coverage_start` 之前的历史也永远不动。

初稿用日期入主键的危险仍然值得记下来 —— 它是本节的原始痛点：

> AAPL 预告 10-29（估计）→ 公司确认改到 11-05 → 库里出现**两行**。
> `days_to_next_earnings = min(未来 event_date) - 今天` 会取到**已作废的 10-29**。
> 更糟的是 `≤10 天` 的每日刷新会被这个错误值触发，而刷新**只写不删**，
> 所以它**永远修不好自己**；倒计时走到「财报 0d」，第二天翻成「财报后 1 天」——
> **播报一场从未发生的财报**。再过几天数字又自己对了，于是事后更难发现。

改期于是成为一次 UPDATE，结构上不可能产生孤儿 ——
这与 §9.1.1 给 `strength_daily` 做的 `(date, rank)` → `(date, symbol)` 是同一招。

**探测器**（进 `invariants.sql`）：
`select symbol, event_type from symbol_events where event_date >= current_date group by 1,2 having count(*) > 1`
对季报公司必须返回 0 行。
再加一条可复现性断言：最新一行的 `days_to_next_earnings`
必须精确等于 `(min 未来 event_date) - date`。

#### (2) 「下一次」是预告，不是事实

未来的财报日在公司正式确认前是估计值、会移动；分红除息日同理。

- `is_estimated` 落库，前端对估计值给 **tooltip**，**不做视觉区分**：
  下一次财报在公司确认前一直是估计值，一个铺满整列的标记等于没标
  —— 与下一条同源，只不过成因在另一头。
  `metrics_daily` 同时存 `next_earnings_date` / `next_dividend_date`，
  否则前端要为一个 tooltip 再查一次 `symbol_events`。
- **`is_estimated` 必须有生命周期，不能只有 `default true`。**
  从 `Ticker.dividends` 取回的历史除息日是**已发生的事实**，必须写 `false`；
  把settled 的事实标成「估计」是另一种不诚实，而且会让标记到处都是、从而失去意义。
- 没有任何已知未来事件时，`days_to_next_*` 写 **`NULL`**，不写一个大数字。

#### (3) 只写最新一行 —— 历史行的事件列一律 `NULL`

**这是本节最重要的一条规则，它一次性消掉三个问题。**

§3.0 规则 2 每天重写全部 400 行 `metrics_daily`。那条规则对复权因子是对的
（追溯改写的因子**追溯为真**，老行本来就是错的）；但对事件**恰好相反**：
AAPL 把财报从 10-29 挪到 11-05，并不会让「10-01 那天当时公布的预告是 28 天后」
这件事变成假的。每天重写会让**同一条历史行今天显示 28、明天显示 35**，
永不稳定，且没有任何记录。

同时，库里只有「下一次」财报时，一条 2025-03-01 的历史行会算出
`days_to_next_earnings = 614` —— 正是 (2) 里禁止的那个「大数字」。

→ **规则：事件距离只在最新一个 session 的行上计算，其余行写 `NULL`。**

代价为零：§1.2 已排除历史展示，§10.2 唯一的消费者是当天表格里的倒计时标签。
收益是三个：不再有 614、不再有滚动改写、而且**「历史行带前视信息」这条说明可以直接删掉**
—— 不需要在方法论页解释一个不存在的泄漏。真要看历史，`symbol_events` 就在那里，
这正是 (1) 存日期而不存天数的理由。

#### (4) 抓取：不可批量、三个端点、按周刷新、**失败不得惊动主管道**

OHLCV 一次 `download` 拿 17 个标的（§7.3.1），但事件必须**逐标的**调用，
且需要**三个**端点（初稿只写了两个，于是 `days_since_last_earnings` 根本没有数据源）：

| 端点 | 给什么 | 供给哪个参数 |
|---|---|---|
| `Ticker.calendar` | 下一次财报日（常为估计）、**最近一次**除息日（见下） | `days_to_next_earnings` / `days_*_dividend` |
| **`Ticker.earnings_dates`** | 最近若干次**已发生**的财报日 | `days_since_last_earnings` |
| `Ticker.dividends` | 历史除息日与金额 | `days_since_last_dividend` |

> **M4 实测确认两件事**（初稿把它们列为「要实测确认一次」）：
>
> 1. **`calendar['Ex-Dividend Date']` 给的是「最近一次」，不是「下一次」。**
>    实测 AAPL 在 2026-09-21 返回 `2026-08-10` —— 已经发生的那一次。
>    所以不能把它无条件当作未来事件：实现按「严格晚于观测日才算未来」判定，
>    于是两种口径下都不会产出一个指向过去的「下一次」。
> 2. **`calendar` 与 `earnings_dates` 会对同一件事给出相差一天的日期。**
>    实测 AAPL：前者说 `2026-10-30`，后者说 `2026-10-29`。
>    两条都入库时，17 个标的里有 **14 个**触发了 §3.5(1) 的孤儿行不变式
>    —— 而那条不变式要防的正是这个后果：倒计时取 `min(未来 event_date)`
>    会指向更早的那个，到期后翻成「财报后 1 天」，**播报一场从未发生的财报**。
>    → 每类事件只保留**最早的那条未来行**，与 `min(未来 event_date)` 完全一致，
>    下游无任何信息损失。

→ **每标的 3 次请求 × 17 = 约 51 次**（不是 17 次）。
> **M4 实测：整跑 52 次请求**（51 次事件 + 1 次价格批量），占 200 预算的 26%。
> 初稿估的「最坏 153 次、77%」是把每次都重试满三次算出来的上界，实际远低于它。
按 `request_interval_seconds: 2` 串行约 100 秒；`retry_max_attempts: 3` 最坏约 153 次，
占 `max_requests_per_run: 200` 的 77% —— 预算够，但没有富余，M4 要实测核对。

刷新频率（只抓 `enabled = true` 的标的）：

| 情形 | 频率 |
|---|---|
| 常态 | **每周一次**（固定周三，避开美国假日集中的周一/周五；若当周该日非交易日则顺延到下一个交易日） |
| 某标的最近未来事件 `≤ 10 天` | 该标的**每天**刷新 |
| 手动 dispatch | 随时全量 |

> 触发条件直接查 `symbol_events`（`min(event_date) - current_date <= 10`），
> **不要**读 `metrics_daily` —— 后者在管道里是**在抓取之后**才算的（§9.1.4 的顺序），
> 首次运行时还是空的，那样这个触发器永远不会生效。
> 「上次抓取时间」存在 `private.fetch_state(symbol, last_event_fetch_at)`；
> 不要依赖 `symbol_events.updated_at` —— 那张表走整窗删+插（§3.5(1)），
> 每次重写后它都是「最近一次写入」而不是「最近一次尝试抓取」，两者语义不同：
> 抓取成功但内容没变时，你仍然需要知道「我今天查过了」。

**失败处理 —— 这里有一个必须避开的陷阱：**

事件是增量信息，抓取失败**不得**记 `partial`。
因为 §7.2 的 `partial` 是 **exit 1 且不写 `ok` 行**，于是 §7.1 的条件重试
「本 session 已有 `ok` 就跳过」**不会跳过** —— 夏令时那四跑会全部执行完整管道。
而 §7.1 自己用粗体警告过那条路径：
「若 17:00 那跑用 yfinance 成功、18:40 那跑撞上限流降级到 Stooq，
**好数据会被更粗的源静默覆盖**」。
→ **一个可选的装饰性指标，就这样获得了静默污染核心价格序列的能力。**
同时它还会让 §7.2.1 的 dead-man's switch 在那些天不 ping（沉默 = 告警），
把唯一的「管道还活着」信号训练成噪音。

→ **规则：事件抓取失败记 `ok_events_stale`，exit 0，不告警**，
仅在 `runs.message` 里留痕；核心价格与四个核心指标照常写入，事件列写 `NULL`。
数据完整性的告警交给 (5) 的新鲜度不变式（它跑在 `keepalive.yml` 里，
那才是慢变量该待的地方）。
> 注意 `runs.status` 有 `check` 枚举约束（§9.1）。本项目从零建库，
> 该值已包含在 `0001_init.sql` 里，**无需额外迁移**。
> （若将来在已部署的库上再加状态值，那才是一次编号迁移，
> 且必须先替换 CHECK 再让代码开始发出该状态。）

#### (5) 新鲜度不变式：必须能容忍「ETF 没有财报」和「刚报完财报」

初稿写的是「每个启用标的至少有一条 `event_date >= 今天` 的 earnings 行」。
这条断言**从第一天起就是红的**，而且有三个独立缺陷：

1. **QQQ 在 universe 里且 `enabled = true`，而 ETF 永远没有财报。** 永久失败。
2. **每次财报之后有空窗**：公司周二发完财报，下一季的预告往往要几天后才出现；
   叠加每周刷新，某标的可能连续 **7 天**没有未来 earnings 行。
   16 只股票季报 → 一年四次集中飘红。
3. **「被显式标记为无预告」在 schema 里不存在** —— 逃生舱是造不出来的。

而 `invariants.sql` 同时跑在 `ci.yml` 和 `keepalive.yml`（§7.4）里，
一条长期飘红的断言会把 §12 #9 整套补偿策略训练成「反正它总是红的」。

→ 修正后的断言：
```
对 symbols 中 expects_earnings = true 且 enabled = true 的标的：
  存在 event_date >= current_date 的 earnings 行
  或 max(earnings event_date) >= current_date - 10 天   -- 刚报完，预告还没出
```
配套：`symbols` 增加 `expects_earnings boolean not null default true`，
ETF 同步时置 `false`（`sync_symbols.py` 按 `type` 推断）。

#### (6) 单位：**日历日**，且与全项目的交易日约定显式隔离

人说「还有 3 天财报」指的是自然日，所以这四个用**日历日**，
而全项目其余窗口都用交易日（§6.1.1）。防混用靠列名写死：`days_*` vs `sessions_*`。

**本期不提供 `sessions_to_next_*`。** 因为 `trading_sessions` 只向未来延伸
`sessions_horizon: 60` 个交易日（约 3 个日历月），而刚报完财报时下一次通常在 3 个月后
—— 那个日期在表里根本没有行，`ordinal()` 无从计算；若用 INNER join 实现，
**会把整行连同 `days_to_next_earnings` 一起无声吞掉**（§9.1.4 用整段警告过这个模式）。

**今天的边界要写死**（这是这四个参数一年中唯一真正被人盯着看的那一天）：

- `next`：`event_date > 观测日`（严格大于）
- `last`：`event_date <= 观测日`（含当天）
- 于是财报当天：`days_since_last_earnings = 0`，`days_to_next_earnings` 指向下一季。
  不会出现「距财报 0 天」和「财报后 0 天」同时显示的自相矛盾。
- 已知局限：财报有盘前/盘后之分，而管道 17:00 ET 才跑 ——
  盘后财报在跑的时候其实已经公布。本期**不建模 `time_of_day`**，
  在方法论页注明这一天的读数以「日」为粒度。

### 3.6 指标是可插拔的
见 §6「配置驱动」。新增一个指标 = 写一个纯函数 + 在 `config/metrics.yaml`
加 6 行，**不需要改数据库 schema，不需要改前端表格代码**。

---

## 4. 「今日三强股」与强度衡量参数

> **摆正它的位置：本项目不是一个榜单项目。**
> 三强股只是加在 §2 这批个股与 ETF 上的**又一个变量**，
> 与 RSI(14)、EMA(60)、α/β 平级 —— 它回答「当下谁在跑赢」，
> 就像 RSI 回答「当下是否超买」一样，都是同一批标的的一个横截面属性。
> 它**不是**全站的主题，前端也不该让它占据头条（§10.2 的布局按此修正）。
> 本章之所以篇幅长，是因为「怎么排」比「RSI 怎么算」有更多容易出错的边角
> （NaN 排序、并列、口径变更、连续天数），不是因为它更重要。

### 4.1 主排序分（可配置）
默认沿用 low-buy 已注册策略的那把尺子，但改为收盘口径：

```
mom_20 = adjC_t / adjC_{t-20} - 1        # 20 个交易日收盘-收盘动量
```
- 排名池：16 只个股（`rank_pool: stocks`）。`min_bars: 21`。
- 降序排名；并列时按 symbol 字典序。
- config 可切换主排序分为 `mom_20 | composite`（见 4.3）。

**NaN 才是真正的确定性风险，不是并列。** 浮点数精确并列是零测度事件；
而一个刚加入、只有 8 根 bar 的标的，`mom_20` 就是 NaN，
`sorted(pool, key=..., reverse=True)` 会把它放到哪里取决于比较链的顺序 ——
**跨输入顺序不确定**。直接照抄 low-buy `relative_strength.py` 已经写对的规则：

```
① 非有限值（NaN / ±Inf）一律替换为 -inf，使其永不获胜
② 再用一个 isfinite 掩码硬性排除，保证非有限值不可能进入 top-N
③ 若有限值的标的不足 N 个，只有这些有限的能上榜，其余名次为空
```

### 4.2 强度衡量参数（你问的「delta 参数或其他」）
只给一个动量数值是没有信息量的——它无法回答「这个第三名有多稳」。
以下六个参数一起构成一张可读的强度画像，**全部在卡片上展示**；
其中前四个落 `strength_daily` 表，后两个由视图现算（原因见本节末）：

| 字段 | 定义 | 回答了什么问题 |
|---|---|---|
| `score` | 主排序分，默认 `mom_20` | 绝对强度有多少 |
| **`delta_to_next`** | `score(#n) - score(#n+1)` | **领先优势有多厚**。第 3 名对第 4 名的 delta 若只有 0.1pp，说明这个名单明天极可能换人 —— low-buy 里记录过的「#3 vs #4 squeak」正是这个痛点 |
| `delta_to_median` | `score - median(pool)` | 相对全池的超额强度（剔除「全体普涨」的假强） |
| `rank_delta_1d` | `rank_上个交易日 - rank_今` | 排名动能：新晋 / 稳守 / 下滑 |
| `days_in_top_n` | 连续在榜天数 | 持续性。连续 8 天在榜 ≠ 今天刚冲进来 |

后两个字段有三个必须写死的约定，否则会静默产出 NULL 或错误的连续天数：

- **「昨」= 表里 `max(date) < today` 的那个交易日**，不是日历昨天。
  否则每个周一和每个假日后 `rank_delta_1d` 都是 NULL —— 一年约 60 天，且悄无声息。
- **新晋者必须有一个可知的前一名次** → 见 §4.4：每天存**全部 16 名**，不只存前 5。
- **换尺子必须断开连续计数**：`score_metric` 变了，跨配置变更累计的
  `days_in_top_n` 没有意义，计数归零。
  > 反过来说：**一个不会改变任何名次的尺子不配叫换尺子。**
  > 这正是 `rs_vs_qqq_20d` 被删掉的原因（§12 #3）：它减的是当天全体共同的常数，
  > 排名与 `mom_20` 恒等，切换它的**唯一可观测效果**就是把所有连续天数清零
  > —— 一个数学上的空操作造成一次用户可见的撞谎。

**这两个字段不入库，在读取时用窗口函数现算**（见 §9.1 的视图）。
它们是「其他行的函数」，一旦反规范化进每一行，§3.0 规则 2 的回补重算改写了
某个历史日的名次后，其后每一天的连续计数链就全错了，而没有任何东西会重算它们
—— 卡片上那个「在榜 5 天」会悄悄撒谎。约 4000 行/年的规模下窗口函数便宜到可以忽略。

> **`delta_to_median` 也是常数平移，所以它同样只能当展示列，不能当排序尺子。**
> 池内中位数当天对所有标的是同一个数 —— 按它排名与按 `mom_20` 排名恒等。
> 它之所以保留而 `rs_vs_qqq_20d` 被删，是因为**参照系留一个就够**：
> 两者回答的是同一个问题（「这个强是真强，还是全体都在涨」），
> 而中位数比 QQQ 更贴近「这 16 只里的相对位置」，且 QQQ 的 `mom_20`
> 本来就在上方表格里能直接看到。
> `strength.yaml` 的排序尺子白名单里不得出现任何常数平移量。

风险上下文（同卡片显示，避免「强 = 该买」的误读）：
`rsi_14`（是否超买）、`close_vs_ema60_pct`（离中期均线多远）、`beta`（波动放大倍数）。

### 4.3 可选的复合分（config 切换，本期先不默认开）
```
composite = w1 * z(mom_20) - w2 * z(resid_vol)
```
权重进 `config/strength.yaml`。留着，但**默认关闭** —— 复合分一旦引入就必须
论证权重来源，否则只是把任意性藏进小数点里。

> 初稿这里写的是 `w1*z(mom_20) + w2*z(rs_vs_qqq_20d) - w3*z(resid_vol)`，
> 而 z-score 对常数平移不变 → `z(rs_vs_qqq_20d) ≡ z(mom_20)`，
> 两个动量项完全共线，`w1` 与 `w2` **不可分辨**，只有 `w1+w2` 有意义。
> 这恰好是上一句警告里「把任意性藏进小数点」最锐利的一个例子。

### 4.4 每天存**全部 16 名**的完整排名，而不只是前 3 或前 5

初稿只存前 5（「多存两名让 delta_to_next 对第 3 名有意义」）。存全部 16 名更好，
一次解决四个问题，代价约 **4000 行/年**（对照 §8.6 的容量估算等于零）：

1. `delta_to_next` 对最后一名之外的**每一名**都有定义（只存 5 名时第 5 名必然 NULL）。
2. `rank_delta_1d` 对**任何**新晋者都算得出（昨天第 9 今天第 2，只存前 5 时无解，
   且分不清「新晋」和「无数据」）。
3. `days_in_top_n` 的 N 可以在 config 里改而不需要回补历史。
4. §3.0 规则 2 的修复是**横截面**的（改一只标的的历史 `mom_20` 会改变所有人的名次），
   重排必须对全池进行 —— 全池本来就得存着。

同时 `strength_daily` 必须记录**当时生效的全部口径**，否则历史行不可比且无迹可循：
`score_metric`（哪把尺子）、`rank_pool`（`stocks` 还是 `all`）、`benchmark`
（α/β 的基准是谁）。
初稿已经想到了 `score_metric`，另外两个是同一个论证的直接推论。

---

## 5. 系统架构

```
                    ┌──────────────────────────────────────┐
                    │  GitHub (public repo)                │
                    │  ├─ 代码 (pipeline + web + config)    │
                    │  └─ Actions: 每交易日 17:00 ET 触发    │
                    └───────────────┬──────────────────────┘
                                    │  ① 抓日线 (vendor API)
                                    ▼
                    ┌──────────────────────────────────────┐
                    │  Python pipeline (跑在 Actions runner)│
                    │  ├─ 交易日闸门 (是否开盘?)             │
                    │  ├─ fetch  → prices_daily             │
                    │  ├─ compute→ metrics_daily            │
                    │  └─ rank   → strength_daily           │
                    └───────────────┬──────────────────────┘
                                    │  ② 单事务写入 (pipeline_writer 最小权限角色)
                                    ▼
                    ┌──────────────────────────────────────┐
                    │  Supabase Postgres (free tier)        │
                    │  RLS 开启：anon 只读, 无写权限          │
                    └───────────────┬──────────────────────┘
                                    │  ③ SELECT (anon key, 服务端读取)
                                    ▼
                    ┌──────────────────────────────────────┐
                    │  Vercel — Next.js App Router          │
                    │  Server Components + ISR (revalidate) │
                    └──────────────────────────────────────┘
```

三条数据边界，三种凭证，互不越界（详见 §8）。

### 5.1 为什么把计算放在 Actions 而不是 Supabase Edge Function
- 指标计算是 pandas/numpy 的天然主场，Deno/TS 重写不划算。
- Actions 对 public repo 免费额度极其宽裕；Edge Function 有调用配额。
- Actions 有完整日志、重跑按钮、手动 dispatch，排障成本低一个数量级。
- 代价：Actions 的 cron 不保证准时（高峰期可延迟数分钟到十几分钟）。
  对 EOD 看板这是可接受的——我们用「数据日期核对」而不是「时钟」来判断成功（§7.2）。

---

## 6. 配置驱动（你要求的「股票和参数都能单独设计并可改」）

**单一事实来源**：`config/` 下的 YAML 同时驱动 **pipeline、数据库写入、前端表头、方法论页**。

```
config/
├─ universe.yaml      # 标的池
├─ metrics.yaml       # 指标及其参数
├─ strength.yaml      # 三强股排名规则
└─ app.yaml           # 站点标题、免责声明、settle_minutes、lookback_bars、图表窗口
```

> **前端在构建期用 `js-yaml` 直接读这几个 YAML，不生成中间的 `config.json`。**
> 初稿设计的是「脚本导出 `config.json` 并提交 + CI 检查 `git diff --exit-code`」，
> 但那个检查**拦不住事故**：Vercel 从 git 部署，与 CI 状态无关。
> 直接推 `main` 改了 `metrics.yaml` 却忘了重新生成，陈旧的 `config.json` 会先上线，
> CI 事后才变红 —— 它什么都没拦住。
> 直接读 YAML 把这一整类漂移**消灭**而不是**检测**，还顺手少了一个生成文件。

### 6.1 `universe.yaml` 草案
```yaml
benchmark: QQQ                 # alpha/beta 的基准，必须同时在 symbols 中
symbols:
  - {symbol: QQQ,   name: "Invesco QQQ Trust",   type: etf,   enabled: true}
  - {symbol: AAPL,  name: "Apple",               type: stock, enabled: true}
  - {symbol: AMD,   name: "AMD",                 type: stock, enabled: true}
  # ... 其余 14 只
```
增删标的 = 改这一个文件 + 跑一次 backfill workflow。**不改任何代码。**

### 6.1.1 `app.yaml` 里的运行参数

```yaml
site:                         # 站点文案与地址（前端读；pipeline 侧只做 schema 校验）
  title:      'markme'
  subtitle:   'market metrics'
  disclaimer: '…'             # 页脚免责声明
  repo_url:   'https://github.com/sudoghut/markme'   # 页脚源码链接（本部署的属性）

settle_minutes:        60     # §7.2 闸门 2：收盘后多久才认为收盘价已定稿（分钟）
lookback_bars:        400     # §12 #5：回填深度 = 日常滚动重抓窗口（交易日）
sparkline_bars:        60     # 全池表格每行的迷你走势（交易日）

# 抓取礼仪（§7.3.1）—— 我们不追求速度
request_interval_seconds:      2   # 日常运行：每次供应商请求之间的间隔
backfill_interval_seconds:     5   # 回填：更慢
max_requests_per_run:        200   # 硬上限，超出即中止并记 partial
retry_max_attempts:            3   # 退避有上限，等下一跑比死重试便宜
sessions_start_date:  '2024-01-02'   # §9.1.4：trading_sessions 的左端点，**固定，永不前移**
sessions_horizon:      60     # §9.1.4：向未来预填多少个**交易日**

# 事件抓取节奉（§3.5(4)）
events_refresh_weekday:     3   # 周三；非交易日则顺延到下一个交易日
events_refresh_within_days: 10  # 事件临近此数时该标的改为每日刷新

revalidate_seconds:     3600  # §10.1：ISR / Data Cache 的重验证间隔（秒）
```
> 单位规则：除 `settle_minutes`（分钟）与 `events_refresh_within_days`（日历日）外，
> 其余窗口一律是**交易日**。
> 陈旧黄条的 7 个日历日**不在这里** —— 它由市场结构决定（最长休市 ≈ 4 天）
> 而不是部署偏好，所以和时区一样是代码常量（`web/lib/market.ts`），
> 不做成旋钮；放进来还会把一个纯前端的数字拖进 pipeline 的 `_Strict` schema。
> 初稿写过一个 `chart_window: 126` 和一个 `session_horizon_days: 90` ——
> 前者在个股页删掉后没有任何消费者（真正被用到的 60 藏在注释里），
> 后者是日历日而其余都是交易日。同一个 YAML 块里混单位，就是将来那个
> 「90 天还是 90 个交易日」bug 的出生地。
> `lookback_bars` 只有一个，回填与日常运行共用 —— 这不是巧合而是设计：
> §3.0 规则 2 要求每天重抓整个窗口，于是「首次回填」不过是这个操作的第一次执行。
> 两个数字要对齐的地方，就是将来会不对齐的地方。

**`lookback_bars` 是 bar 数，而供应商 API 只认日期 —— 这个转换必须走日历，不能拿日历日硬换。**
400 个交易日 ≈ 574 个自然日；随手写 `start = today - 400 days` 只会拿到约 275 根，
EMA 的喂入量直接掉到 360 以下（§12 #5 标准 B），而**没有任何东西会报警**。
唯一正确的换算是查交易日历：

```
from_date = trading_sessions 中 ordinal = (今天的 ordinal - lookback_bars + 1) 的那一天
```
`backfill.yml` 的 `from_date` 参数只作为**人工覆盖**存在，默认值由上式推出 ——
否则 §6.1.1 自己那句「两个数字要对齐的地方就是将来会不对齐的地方」当场就被违反了。

> 一个例外要说清楚：**删除不是真删**。`prices_daily.symbol` 对 `symbols` 有外键，
> `delete from symbols` 会被挡住。同步脚本对移出 config 的标的执行
> `enabled = false` 软删除，历史数据保留（否则回补与历史榜单都会断）。
> 这个「config → `symbols` 表」的同步脚本归属 **M4**（见 §11）——
> 它是所有写入的前置条件，初稿的里程碑表漏了它的归属。

### 6.2 `metrics.yaml` 草案
```yaml
metrics:
  - id: rsi_14
    fn: rsi_wilder                 # pipeline/metrics/registry.py 中注册的函数名
    core: true                     # true → 占 metrics_daily 的显式列
    params: {period: 14}
    min_bars: 15                   # 低于此写 NULL（§3.3 语义）
    provisional_below: 250         # 低于此出值但标灰；必须 ≥ min_bars
    display: {label: "RSI(14)", format: "number:1", widget: rsi_bar}

  - id: ema_60
    fn: ema
    core: true
    params: {period: 60}
    min_bars: 60
    provisional_below: 180
    display: {label: "EMA(60)", format: "price", widget: plain}
    derived:
      - {id: close_vs_ema60_pct, expr: "adj_close / ema_60 - 1",   # 注意是 adj_close
         display: {label: "距 EMA60", format: "pct:2", widget: diverging_bar}}
      - {id: ema60_slope_20d, expr: "ema_60 / lag(ema_60, 20) - 1",
         display: {label: "EMA60 斜率(20d)", format: "pct:2", widget: signed}}

  - id: alpha_beta_126
    fn: alpha_beta
    core: true
    params: {window: 126, benchmark: QQQ, min_obs: 120,
             annualization: linear,                      # 默认线性，见 §3.4
             risk_free: {mode: constant, annual: 0.0, symbol: ^IRX, units: percent}}
    outputs: [alpha_annual, beta, r2, corr, resid_vol_annual, alpha_t_stat, n_obs]
    display:                                             # 每个 output 都必须有一条
      alpha_annual:     {label: "α (年化)", format: "pct:2", widget: signed,
                         dim_when: "abs(alpha_t_stat) < 2"}
      beta:             {label: "β", format: "number:2", widget: beta_bar,
                         dim_when: "r2 < 0.3"}
      r2:               {label: "R²", format: "number:2", widget: plain}
      corr:             {label: "相关", format: "number:2", widget: plain}
      resid_vol_annual: {label: "残差波动(年化)", format: "pct:1", widget: plain}
      alpha_t_stat:     {label: "α t 值", format: "number:2", widget: plain}
      n_obs:            {label: "样本数", format: "int", widget: plain}

  - id: event_distances
    fn: event_distances            # 依赖 symbol_events 表，不依赖收盘价（§3.5）
    core: true
    params: {unit: calendar_days, latest_row_only: true}
    # 刷新节奉是**运行参数**，住在 app.yaml（§6.1.1），不在这里 ——
    # 否则节奉旋钮有两个家，而那正是 §6.1.1 自己警告过的那种开头。
    outputs: [days_to_next_earnings, days_since_last_earnings,
              days_to_next_dividend, days_since_last_dividend,
              next_earnings_date, next_dividend_date,
              next_earnings_is_estimated, next_dividend_is_estimated]
    display:
      days_to_next_earnings:    {label: "距财报", format: "days", widget: countdown_chip,
                                 estimated_flag: next_earnings_is_estimated}
      days_since_last_earnings: {label: "财报后", format: "days", widget: plain}
      days_to_next_dividend:    {label: "距除息", format: "days", widget: countdown_chip,
                                 estimated_flag: next_dividend_is_estimated}
      days_since_last_dividend: {label: "除息后", format: "days", widget: plain}
      next_earnings_date:       {label: "财报日", format: "date", widget: plain}
      next_dividend_date:       {label: "除息日", format: "date", widget: plain}
      next_earnings_is_estimated: {label: "财报日为估计", format: "bool", widget: hidden}
      next_dividend_is_estimated: {label: "除息日为估计", format: "bool", widget: hidden}

  - id: mom_20
    fn: momentum
    core: true
    params: {period: 20}
    min_bars: 21                   # 主排序分，漏了这条会靠 NaN 兜底（§4.1）
    display: {label: "20日动量", format: "pct:2", widget: signed}
```

两条关于 `display` 的规则：

- **`dim_when` 对 `null` / `NaN` 必须 fail closed（判定为「打灰」）。**
  初稿只定义了表达式的文法，没定义空值语义 —— 而 §3.4 的 QQQ 自回归恰好会产出
  `alpha_t_stat` 为空，`NaN < 2` 求值为 false 会让基准行**不打灰**，
  即宣称一个「显著」的 0.00% alpha。空值一律打灰。
- `dim_when` 是一个**受限**表达式（只允许字段名、数字、比较与 `abs()`），
  由白名单求值器处理。**绝不用 `eval`** —— config 虽由我们自己写，
  但它会被前端在构建期读入，给它任意代码执行能力是无谓的风险。

**三条 CI 校验**（缺一都会让「配置驱动」这个承诺名存实亡）：

1. `core: true` 的每个 output → `metrics_daily` 必须有同名列（防止改了 config 忘了迁移）。
2. **反向**：`metrics_daily` 的每个指标列 → 必须在 `metrics.yaml` 里有声明
   （防止 §3 定义了、schema 建了，但 config 里没人声明的空洞 ——
   初稿的 `ema60_slope_20d` 正是这样漏掉的）。
3. `core: true` 的每个 output 必须有 `display` 条目（否则前端要显示的字段没有配置来源，
   §6 的「前后端永不漂移」第一天就破了）。

### 6.3 注册表模式
```python
# pipeline/metrics/registry.py
REGISTRY: dict[str, MetricFn] = {}

@register("rsi_wilder")
def rsi_wilder(close: pd.Series, period: int) -> pd.Series: ...
```
**两种指标函数签名**（§3.5 的事件距离逼出了第二种）：
```python
@register("rsi_wilder")                 # kind="series"：价格序列进，序列出
def rsi_wilder(close: pd.Series, period: int) -> pd.Series: ...

@register("event_distances", kind="table")   # kind="table"：不碰价格，读另一张表
def event_distances(symbol: str, dates: pd.Index, events: pd.DataFrame) -> pd.DataFrame: ...
```

> **顺带修正一句过头的承诺。** 下面那句「不需要改 schema、不需要改前端」
> 只对**纯价格派生**的指标成立。§3.5 的事件距离就是反例：
> 它需要一张新表、八个新列、一个新抓取器、一个新 widget 和一套刷新节奉。
> 引入新**数据域**时这个代价是绕不开的，写在这里以免下一个人拿这句话当承诺。

新增**纯价格**指标的完整流程：① 写纯函数并 `@register` ② `metrics.yaml` 加条目
③ 加一个 golden-value 测试 ④ 跑 backfill。**无 schema 迁移、无前端改动。**
（靠 `metrics_daily.extra` jsonb 列承接非核心指标，见 §9.2。）

> 「无前端改动」只有在前端表格**从第一天起**就同时读显式列和 `extra->>'<id>'`
> 时才成立。如果 M6/M7 只接了显式列，这个承诺是假的，而且要到第一次加指标时才发现。
> → **双路读取是 M6 的交付项**，写进验收标准。

---

## 7. 自动化（GitHub Actions）

### 7.1 调度
美股正常收盘 16:00 ET。**触发时间取收盘 +60 分钟（17:00 ET）**，只跑一次、只写最终价。

理由：收盘后 5 分钟，免费数据源的 consolidated close 普遍还没定稿（收盘集合竞价与
延迟申报通常要到 16:15–16:30 ET 才稳定）。等一小时可以彻底避开这段不确定区间，
换来的代价只是「晚一小时能看」——对一个 EOD 看板不构成任何损失，
却省掉了 preliminary/final 两段式带来的额外状态、额外一跑和额外的前端标记。
**结论：宁可晚一小时，不要一个会变的数字。**

GitHub cron **只认 UTC 且不懂夏令时**，所以挂两个时间点，由脚本自己判断是否真的过了收盘 +60min：

```yaml
on:
  schedule:
    - cron: '0 21 * * 1-5'    # EDT 17:00 ET (=收盘+1h) / EST 16:00 ET (太早，被拦)
    - cron: '0 22 * * 1-5'    # EST 17:00 ET (=收盘+1h) / EDT 18:00 ET
    - cron: '40 21 * * 1-5'   # 条件重试
    - cron: '40 22 * * 1-5'   # 条件重试
  workflow_dispatch:
    inputs:
      date:  {description: '指定交易日 YYYY-MM-DD，留空=最新', required: false}
      force: {description: '已有 ok 记录也强制重跑', type: boolean, default: false}

concurrency:
  group: markme-daily
  cancel-in-progress: false     # 不取消正在跑的，排队执行
```

DST 下的实际放行情况：**EST 两跑通过（17:00 / 17:40 ET），EDT 四跑全部通过**
（17:00 / 17:40 / 18:00 / 18:40 ET）。由此两条必须写死：

- **「兜底重试」必须是条件重试，不能是无条件重跑。** 初稿写「幂等 upsert，
  再写一遍无害」——这只在每跑产出相同数据时成立，而 §3.0 规则 3 说明它不成立：
  若 17:00 那跑用 yfinance 成功、18:40 那跑撞上限流降级到 Stooq，
  **好数据会被更粗的源静默覆盖**。重试的极性反了。
  → 任务体开头先查「本 `session_date` 是否已有 `ok` 记录」，
  已有 `ok` **或 `ok_events_stale`** 则记 `skipped_already_done` 退出（`force` 可覆盖）。
  （两个状态都意味着**核心数据已完整写入** —— 漏掉后者就会重新武装
  上面那条「好数据被更粗的源覆盖」的路径，见 §3.5(4)）。
  顺带在夏天把供应商请求量降到 1/4，直接削弱 §13 的头号风险。
- **必须有 `concurrency` 组。** §5.1 自己承认「Actions cron 可能延迟十几分钟」，
  一个延迟 40 分钟的 `0 22` 会和 `40 22` 重叠，两个 job 以任意顺序 upsert 同一批行，
  并各自往 `runs` 追加记录。

### 7.2 「今天是否开盘 / 数据是否可信」闸门
不要只靠日历或只靠时钟，两者都会骗你。**四重判定**：

1. **日历**：`pandas_market_calendars` 的 schedule 判断今天是否为交易日，
   并读出当日实际收盘时间（半日市 13:00 ET 会被正确识别）。

   > **M4 实测修正：初稿写的 `XNAS` 这个日历代码根本不存在。**
   > pandas_market_calendars 5.4.0 的 registry 里没有它，
   > `get_calendar("XNAS")` 直接抛 `RuntimeError` —— 按初稿字面实现，
   > 闸门 1 会在第一次运行时崩掉。**正确的名字是 `NASDAQ`。**
   >
   > 而且 `get_calendar("NASDAQ").name` 返回的是 **`"NYSE"`**：
   > 这个包里两者本来就是同一个日历类。实测 NASDAQ 与 `XNYS` 在
   > 2023-01-01–2026-12-31 上**完全一致**（各 1003 天，无日期差异、
   > 无收盘时间差异），所以初稿那句「两者的假日与半日市实际完全一致」
   > 是对的 —— 只是它据以做出「明示选择」的那个代码名是错的，
   > 而这个选择实际上不存在。
2. **时钟**：当前 ET 时间必须 **≥ 当日实际收盘时间 + `settle_minutes`**（config，默认 60），
   否则记 `skipped_too_early` 退出。半日市 13:00 收盘则闸门 14:00 ET 放行，规则自动适配。
   （注意：没有 cron 打在 14:00 ET，所以半日市当天最早的实跑仍是 16:00/17:00 ET ——
   闸门会自适应，延迟不会。这是可接受的，但别误以为半日市会早跑。）
3. **数据自证（逐标的，不是只看 QQQ）**：断言**每一个**标的的最新 bar 日期 ==
   当日 session 日期。初稿只断言 QQQ，于是「某一只股票拿到昨天的 bar」既不算抓取失败、
   也过得了闸门：AVGO 的 `mom_20` 会用一个错位一天的窗口去和 15 个日期正确的同行排名，
   全站头条的三强榜就是一个**混合日期的横截面**，而 §4.2 的 `days_in_top_n` 会把这个
   错误永久烤进历史。
   → 不匹配的标的：该标的指标写 `NULL`、排除出排名池、状态 `partial`、
   在 §10.5 的「部分标的缺失」态里显示出来。
4. **数据合理性（供应商脏数据闸门）**：上面三条只验证「数据到了没」，从不验证
   「数据像不像真的」。yfinance 的坏 tick（0.01 的收盘、重复的陈旧值、错位的日期）是常态。
   一个 MU 的 0.01 坏收盘会给出 `mom_20 ≈ -99.9%`、RSI 钉在 0，
   并且因为会进入 126 日窗口，**在上游修正之后仍继续污染统计半年**。
   全是 O(1) 的断言，没有理由不加：
   - **断言必须按源分支** —— Stooq 行的 `close` 是 `NULL`（§3.0 规则 3），
     拿 `NULL` 去比大小或做除法，会让这条**设计好的降级路径变成永久 `partial`**：
     | 源 | OHLC 一致性 | 日间跳变 |
     |---|---|---|
     | yfinance | `low <= open, close <= high` | `\|close_t / close_{t-1} - 1\| <= 0.5` |
     | Stooq | `low <= open <= high` 且 `low <= adj_close <= high` | `\|adj_close_t / adj_close_{t-1} - 1\| <= 0.5` |
   - `adj_close > 0`；`volume >= 0`
   - 跳变超限时若检测到公司行动则放行（拆股日的原始价必然跳变）
   - M4 必须有一个**跑通整窗 Stooq 降级**的测试，否则这条分支永远没被执行过
   - 两个源都有该 bar 时，做一次跨源收盘价一致性比对
     （§3.0 规则 3 的「整窗降级」让这个比对免费可得）
   - 违反 → 该标的 `partial`，不写毒数据

**幂等**（不是闸门，是写入语义）：每张表的冲突目标不同，初稿笼统写成
「所有写入用 `ON CONFLICT (symbol, date)`」是错的 ——
`strength_daily` 的主键是 `(date, symbol)` 且要删+插（§9.1.2）；
`runs` 是「每次运行先追加一行，随后只更新这一行的终态，从不删除」。见 §9.1。

退出语义（决定告警不告警）：
| 情形 | `runs.status` | exit code | 是否告警 |
|---|---|---|---|
| 正常写入 | `ok` | 0 | 否 |
| 非交易日（周末/假日） | `skipped_holiday` | 0 | 否 |
| 未到收盘 + `settle_minutes` | `skipped_too_early` | 0 | 否 |
| 本日已有 `ok` 记录（条件重试跳过） | `skipped_already_done` | 0 | 否 |
| 核心数据完整，仅事件抓取失败（§3.5(4)） | `ok_events_stale` | **0** | **否** |
| 数据源 bar 落后（基准） | `stale_vendor` | 1 | **是** |
| 部分标的落后 / 脏数据 / 降级 | `partial` | 1 | **是**（其余照常入库） |
| 计算/写库异常 | `failed` | 1 | **是** |
| （任务开始时先写入，结束时改写） | `running` | — | — |

### 7.2.1 告警：**沉默是这个设计里最危险的状态**

初稿的告警只有「Actions 失败自带邮件」。这条通道有一个结构性盲区：
**它只能对「跑了并且失败」告警，对「根本没跑」永远沉默。**
而「管道不复存在」这一整类故障 —— 计划任务被停用、密钥被吊销、workflow 被删、
Supabase 项目被暂停 —— 产出的正是沉默，在这个设计里**与成功不可区分**。

两个具体机制：

- **GitHub 会在仓库 60 天无活动后自动停用 `schedule` 触发器**，
  而 workflow 的「运行」不算仓库活动（提交才算）。这个项目的稳态恰恰就是
  「跑得很好、没人再推代码」—— 两个月后它会静悄悄地停掉。
  *（此项需在实施时对照 GitHub 当前文档复核。）*
  → 加一个月度 workflow 做一次轻量提交（heartbeat 文件 / README badge），重置这个计时。
- Actions 的失败通知发给**最后修改 cron 的那个人**，只在失败时发，
  且取决于个人通知设置 —— 单点且脆弱。
  → 加一个 dead-man's switch：成功时 ping 一个 healthchecks.io 之类的免费 URL
  （一个 URL、无密钥，比先前婉拒的 Slack 依赖还轻），**没收到 ping 才是告警**。

前端 header 的「数据截至 <date>（美东）收盘」+ 落后满 7 个日历日变黄条仍然保留 ——
它有效是因为你每天都会看，但**它是一个人类习惯，不能作为唯一的监控**。
阈值为何是 7 个日历日见 §10.6；要紧的是它放宽的**只有人眼这一路**，
自动探测器（`invariants.sql` 的「管道不得静默停摆」，容忍 1 个 session，
由 keepalive 每天跑）没有变 —— 这正是上面那句话的兑现。

### 7.3 数据源选型
| 方案 | 密钥 | 额度 | 稳定性 | 评价 |
|---|---|---|---|---|
| **yfinance (Yahoo)** | 无 | 无明文限制 | 中（非官方 API，偶发限流/改版） | **推荐起步**：17 个标的可一次 batch 拉完，零配置 |
| Stooq (CSV) | 无 | 宽松 | 中上 | **推荐作为 fallback**：接口极稳定但复权口径较粗 |
| Tiingo | 需 key | 免费 500 次/天 | 高 | 想要「正经」数据源时升级到它 |
| Alpha Vantage | 需 key | 免费 25 次/天 | 中 | 额度对每日重抓整窗太紧，不推荐 |

**决定**：主源 yfinance（必须 `auto_adjust=False`，见 §3.0 规则 4）。
数据源写在 `pipeline/fetch.py` 的 provider 接口后面，换源是换一个类。
`prices_daily.source` 记录每行实际来源。

**降级策略（初稿的「失败自动降级 Stooq」必须收紧）**：
源的选择是**整条序列**的决定，不是**单日**的决定。

- **单日 yfinance 失败 → 不降级**，记 `partial` / `stale_vendor`，
  交给 §7.1 的 40 分钟条件重试。这与本计划自己的哲学一致：
  「宁可晚一小时，不要一个会变的数字」。
- **yfinance 持续不可用 → 降级**，且必须**用 Stooq 重抓整个回看窗口**，
  使窗口内复权基准自洽（§3.0 规则 3）。
- 运行时断言：任一指标窗口内 `source` 必须唯一，否则该指标 `NULL` + `provisional` + `partial`。

> 为什么单日降级特别危险：它不是罕见的公司行动触发的，而是被**例行的限流**触发的
> —— 频率高得多，且每次都会在序列里制造一个假的单日跳变，污染其后 20（动量）
> 到 126（alpha/beta）个交易日。`source` 列只能让你**事后**诊断，不能预防。

### 7.3.1 抓取礼仪：**充分尊重数据源的限流，我们不追求抓取速度**

这是一条**设计约束，不是实现细节**。数据源（尤其免费的 yfinance）没有义务承受我们的量。
整个 run 花 5–10 分钟完全可以接受：我们是收盘后一小时才跑的 EOD 看板，
**没有任何理由跑得快**，而 GitHub Actions 的 job 上限是 6 小时，宽裕得离谱。

> **先澄清一个容易吓着自己的说法**：§3.0 规则 2 要求每天重抓
> 「400 根 × 17 标的」，但那是**一次批量请求里的数据量，不是 400×17 次请求**。
> yfinance 一次 `download` 就能传多个 ticker + 日期区间 → **日常运行是 1 次 HTTP 请求**。
> 换句话说，「每天重抓整个窗口」相对「只取今天一行」，
> **请求次数完全相同（1 次）**，只是 payload 从几 KB 变成约 1–2 MB。
> 这就是为什么这条规则买得起：它消灭了一整类静默错数，而代价近乎为零。

| 规则 | 做法 |
|---|---|
| **优先批量接口** | yfinance 支持一次 `download` 多个 ticker —— 17 个标的**一次请求**拿完，胜过 17 次。减少请求数是最有效的礼貌 |
| **串行，不并发** | 必须逐个请求时一律串行。**不开线程池、不开 asyncio 并发** |
| **请求间固定间隔** | `request_interval_seconds`（config，默认 **2**）。回填时用更大的值（默认 **5**） |
| **尊重 429 / `Retry-After`** | 收到 429 一律先读 `Retry-After` 并**完整照办**；没有该头才用指数退避 + 抖动（2s → 4s → 8s，上限 60s）。**那个 60s 上限只管我们自己算的退避，不管对方给的指令** —— 把对方的 300s 打折成 60s 正是这条规则要防的明知故犯。对方要求超过 10 分钟时既不打折也不干等：放弃这一跑记 `partial`，交给 §7.1 的条件重试 |
| **退避有上限，不无限重试** | 至多 3 次。仍失败就记 `partial` 并交给 §7.1 的条件重试（下一跑在 40 分钟后）—— **等待比重试便宜** |
| **全局请求预算** | 单次 run 的总请求数设硬上限，超出即中止并记 `partial`。防止某个循环 bug 变成一场无意的压测 |
| **诚实的 User-Agent** | 标明项目名与仓库地址，让对方能找到我们，而不是伪装成浏览器 |
| **回填分批** | 首次回填是一次性的大动作：按标的分批、批间 sleep，宁可跑 20 分钟 |

> 这条与 §7.1 的「条件重试」是同一个方向的两件事：
> 那里把夏令时的每天 4 跑降到 1 跑，已经把供应商负载直接砍掉 3/4。
> 两者叠加，本项目对数据源的日常压力约等于**一天一次批量请求**。

**写库侧也要跟着省**：抓回整窗之后，**只 upsert 与库里真正不同的行**
（逐行比对 `adj_close` / `adj_factor`）。正常日子只有今天那一行是新的，
其余 399 行原样不变 → 写入量从 400 行降到 1 行；
除息那天则有几百行确实变了，它们会被正确写入。
供应商侧成本不变，数据库侧成本降两个数量级，而正确性保证一点没丢。

> **但最新一个 session 的 `metrics_daily` 行必须无条件重写，不进这个差分优化。**
> 事件列（§3.5）会在**价格完全没变**的情况下改变 —— 财报改期、
> 估计转确认、或事件抓取失败需要把它们置 NULL。
> 若按 `adj_close` / `adj_factor` 比对就跳过这一行，
> 新的财报日永远写不进去，而旧值会留在页面上 —— 又一条静默错数。
> 同时：**非最新行的八个事件列要被显式写成 NULL**，不是「不管它」（§3.5(3)）。

> **如果将来连这 1–2 MB 也想省**：可以改成「日常只抓最近 5 根，
> 比对重叠行的 `adj_factor`，发现变化才触发整窗重抓」。
> **本期不这么做** —— 它把「一定正确」换成「检测逻辑正确时才正确」，
> 而检测逻辑本身也会有 bug，且这类 bug 的表现正是静默错数。
> 既然整窗只花 1 次请求，这个交换不划算。留作 config 开关的备选。

**一个非技术风险要一并记下**：Yahoo 的条款禁止转售/再分发其行情数据，
`yfinance` 自身文档也把自己定位为个人研究工具。一个**公开**站点（§12 #7）
+ 公开仓库 + Yahoo 衍生数据，是一个真实存在（虽概率不高）的暴露面。
→ 方法论页注明数据来源与免责；若站点产生实质流量，就按 §12 #1 换到许可允许
再分发的源（Tiingo / Polygon / EODHD）。这条进 §13。

### 7.4 五个 workflow
| 文件 | 触发 | 职责 |
|---|---|---|
| `daily.yml` | schedule + dispatch | 每日增量：闸门 → 抓取 → 计算 → upsert |
| `backfill.yml` | dispatch only | 全量回填（改 universe / 改指标后跑）。参数：`symbols`、`from_date`（可选覆盖）、`force` |
| `ci.yml` | push + PR | pytest + ruff + mypy + §6.2 三条 config 校验 + §9.3.2/§9.3.3 数据库不变式与越权测试 + gitleaks + 前端 `next build` |
| `keepalive.yml` | schedule（含周末） | 极轻的 `select 1` 防 Supabase 长假期暂停（§8.6）**+ 跑一遍 `invariants.sql` 与 §9.3.3**（§12 #9 第 3 条：不变式断言的是线上状态，不能只挂在 push 触发的 `ci.yml` 上） |
| `heartbeat.yml` | schedule（每月） | 一次轻量提交，重置 GitHub 的 60 天 cron 停用计时（§7.2.1）；顺带 `pg_dump` 备份 |

---

## 8. 凭证与安全（你问的：需要提供什么？怎么关联？）

前提：**仓库是 public 的**，所以「什么能进代码、什么只能进 Secret」必须分清。

### 8.1 三类凭证，三个去处

| 凭证 | 放在哪 | 能否公开 | 用途 |
|---|---|---|---|
| `SUPABASE_URL`（项目 URL） | GitHub **Variables**（非 Secret）+ Vercel env | 公开（它就是个域名） | 两端都要连 |
| **`MARKME_DB_URL`**（`pipeline_writer` 角色的连接串） | **只放 GitHub Secrets**，且只挂在写库那一步 | **绝对不可公开** | Actions 写库 |
| `SUPABASE_ANON_KEY`（或新版 `sb_publishable_...`） | Vercel env | **可以公开**（设计如此） | 读库，权限由 RLS 限死 |

> `SUPABASE_URL` 不要当 Secret 存 —— 同一行已经说明它是公开的，
> 而 Secret 会在日志里被打码，白白增加排障难度，换不来任何安全收益。用 Variables。

### 8.1.1 为什么写入用专用角色，而不是 `service_role`

初稿用 `SUPABASE_SERVICE_ROLE_KEY`，并自己承认它「等同 DB 超管」。
两个独立的理由汇合到同一个结论，所以改掉：

1. **爆炸半径。** 那个 job 会 `pip install` yfinance / pandas / statsmodels /
   supabase 及其约 80 个传递依赖。供应链投毒是反复发生的真实事件；
   一旦某个包在 import 期读走 `os.environ`，攻击者就拿到了对**现有及未来所有表**
   的完全读写删权限，而 §13 对「数据可被任意写入」的缓解措施（RLS）
   在 `service_role` 面前**按设计就是无效的**。
   把 `actions/checkout` 按 SHA 钉死、却让 80 个 PyPI 包浮动，是守住了小的那扇门。
2. **事务。** §9.1 的榜单写入需要「删+插」在一个事务里，
   PostgREST 做不了多语句事务（见 §9.1）。走 psycopg 直连本来就需要连接串。

所以：建一个 `pipeline_writer` 角色，**逐表授予恰好需要的动词**，用 psycopg 直连。

> **M3 实测的两条连接事实**（两条都会让「按文档写」的实现连不上或坏掉）：
> 1. **直连 `db.<ref>.supabase.co:5432` 连不通** —— 免费层的直连是 IPv6-only，
>    而 GitHub Actions 的 runner 是 IPv4。实测超时。**必须走 pooler。**
> 2. **pooler 要用 session 模式（5432），不是 transaction 模式（6543）。**
>    §9.1.2 的 T1/T2/T3 需要真正的多语句事务，而 psycopg 默认在同一连接上
>    复用预编译语句 —— 这两样在 transaction 模式下都会坏。
>
> 连接串形如：
> `postgresql://pipeline_writer.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require`
> （pooler 的用户名是 `<role>.<ref>`，不是裸角色名。）

一个 secret 换掉另一个 secret，但爆炸半径小一个数量级，且顺带解决事务问题。

| 表 | 动词 | 为什么 |
|---|---|---|
| `symbols` / `prices_daily` / `metrics_daily` | `SELECT, INSERT, UPDATE` | 原始事实层与可重算派生层，**不给 DELETE** |
| `strength_daily` | + `DELETE` | §9.1.2 的榜单重写是删+插 |
| `symbol_events` | + `DELETE` | §3.5(1) 的作废预告行清理（谓词受限，见 §9.3） |
| `trading_sessions` | + `DELETE` | §9.1.4 的全量对账是删+重插（ordinal 每次重新推导）；它可从日历包完整再生，删得起 |
| `private.runs` | `SELECT, INSERT, UPDATE` | 追加 + 改终态，从不删除 |

> M3 实测确认这条边界是活的：写入角色连**清理自己的探针行**都做不到
> （`permission denied for table metrics_daily`）。所以这三张表的修复
> 只能走 UPDATE / upsert，永远不走 DELETE —— 实现时别指望「先删再插」。

> 两条都要写死：
> - **不要图省事写成 `grant ... delete on <全部表>`** —— 价格层与指标层是
>   原始事实与可重算派生层，写入凭证被攻陷时不该有能力抹掉它们，
>   那恰恰是本节要缩小的那个半径。
> - **也不要漏掉 `trading_sessions`**（包括它的 RLS 策略）——
>   漏了的话第一次生产运行会在闸门之前就失败，因为日历对账跑在最前面。

配套的三件事：

- **依赖锁定到哈希**：`uv.lock`，或 `pip-compile --generate-hashes` +
  `pip install --require-hashes`。开 Dependabot。
- **secret 挂在 step 级而非 job/workflow 级**：抓取与计算两步不需要它。
- **从第一天就用新版 `sb_publishable_` / `sb_secret_` 密钥格式**（如果仍要用 Supabase 密钥）：
  旧版 JWT 密钥**无法单独吊销** —— 想吊销 `service_role` 只能轮换项目 JWT secret，
  那会**同时作废 anon key**，前端在同一刻下线。
  → §8.5 补一份轮换 / 事故处置流程，初稿完全没有。

> **anon key 公开是安全的 —— 前提是 GRANT 与 RLS 都配对了。**
> 这不是「反正没人看」，而是 Supabase 的正式设计：anon key 只是声明「我是匿名角色」，
> 真正的权限完全由数据库决定。注意是**两道**：
> **GRANT 管命令级（能不能执行 SELECT/INSERT），RLS 管行级（能看到哪些行）**，
> 两道都必须满足。§9.3 会说明为什么初稿只写了 RLS 那一道是不够的。
> 两道都要当作安全边界来测（§9.3.3 的集成测试）。
>
> 本项目前端在**服务端**读取（§10.1），所以 anon key 甚至不必加 `NEXT_PUBLIC_` 前缀，
> 不会出现在浏览器包里。这是纵深防御的一层，但**不是**安全的依据 —— 依据仍是 RLS。

### 8.2 我需要你提供的东西（清单）
1. **Supabase**：新建一个 free project（区域选 `us-east-1`，离 Vercel 默认区近）。
   > 你选了不建 staging（§12 #9）—— 那么 §12 里那四条补偿措施是**强制的**，
   > 尤其是「`0001_init.sql` 必须在库里还没有任何数据时执行」：
   > 那是唯一一次爆炸半径天然为零的机会，而它恰好是风险最高的一次迁移。

   需要给我的：
   - Project URL（形如 `https://xxxxxxxx.supabase.co`）— 公开，直接发我
   - `anon` / publishable key — 公开，直接发我
   - `pipeline_writer` 的连接串 ← **不要贴进仓库或聊天记录，由你自己填进 GitHub Secrets**

   > **角色由我用 `sbp_` 管理 token 建好，你零操作**（§12 #8）。
   > 但**顺序仍然是硬依赖，落在我身上而不是你身上**：
   > `create role` 必须在 `0001_init.sql` 之前执行，
   > 否则迁移里所有 `grant ... to pipeline_writer` 会直接失败并中止整个事务。
   > 我生成一个随机强密码、拼成连接串写进 `.env`，再由你放进 GitHub Secrets ——
   > 它不出现在迁移文件里，也不出现在任何被提交的文件里。
2. **GitHub**：仓库 Settings → Secrets and variables → Actions
   - **Variables**：`SUPABASE_URL`
   - **Secrets**：`MARKME_DB_URL`
   - 建一个名为 `production` 的 **Environment**，把 secret 挂在它下面，
     **并设置 deployment branch 限制为 `main`** —— 否则任何分支上声明
     `environment: production` 的 workflow 都能读到它。
   - Settings → Actions → Workflow permissions 设为 **read-only**
     （这样未来某个忘了写 `permissions:` 块的 workflow 继承到的是安全默认值）
   - Settings → Actions → Fork PR 审批设为 **"Require approval for all outside collaborators"**
     （§7.4 的 `ci.yml` 在 `pull_request` 上跑 `pytest` + `next build`，
     即在 GitHub runner 上执行任意 fork 代码；无 secret 风险，但默认策略只拦首次贡献者）
   - 开启 GitHub 免费的 **Secret scanning + push protection**
     —— 它在**推送时拦截**，比 CI 里事后报告的 gitleaks 强一个层级
3. **Vercel**：用 GitHub 账号登录 → Import 这个仓库 → Root Directory 设为 `web/` →
   Environment Variables 填 `SUPABASE_URL`、`SUPABASE_ANON_KEY`。
   Vercel 与 GitHub 是 **OAuth 关联，不需要任何 API key**。
   （若采纳 §10.1 的按需 revalidate，再加一条 `REVALIDATE_TOKEN`，两边同值。）
4. 若 production 启用 Vercel Deployment Protection，在 Vercel 创建一个仅供 CI 使用的
   Protection Bypass for Automation secret，并作为 GitHub Secret
   `VERCEL_AUTOMATION_BYPASS_SECRET` 保存；`keepalive.yml` 与 daily 的按需
   revalidate 都只在该值存在时发送 `x-vercel-protection-bypass`。否则健康检查和
   revalidate 请求都会在到达各自 Route Handler 前被拦住。
5. 无需数据源密钥（yfinance / Stooq 都不要）。若将来升级 Tiingo 再加一条 Secret。

**最省事的顺序**：你建好那个 Supabase 项目 → 自己把 Secret 填进 GitHub →
只把 **URL 和 anon key** 告诉我（这两个本来就公开）。写库凭证我全程不需要看见。

### 8.3 public repo 的 Actions 安全基线
- `permissions: contents: read`（workflow 顶层最小权限）+ 仓库级默认也设成 read-only。
- 触发器只用 `schedule` / `workflow_dispatch`。**绝不使用 `pull_request_target`** ——
  那是 public repo 泄露 secret 的头号途径。fork 发来的普通 `pull_request` 默认拿不到 secret。
- 第三方 action 按 commit SHA 钉版本（`actions/checkout@<sha>`），不用浮动 tag。
- **Python 依赖同样钉死到哈希**（§8.1.1）—— 只钉 action 不钉依赖是守小门。
- **`daily.yml` 里不要用 `actions/cache`。** 这个 job 持有唯一的写库凭证；
  依赖安装本来就只要约 20 秒，缓存买不到任何东西，却引入一个「PR 可能写过的东西」
  被恢复进该 job 的假设。*（缓存的分支隔离规则历年有变，需实施时复核 —— 但最简单的
  防御是这个 job 什么都不恢复。）*
- `.env` 系列进 `.gitignore`；仓库里只留 `.env.example`（全是占位符）。
- gitleaks 至少**全历史跑一次**（`--log-opts="--all"`），不只扫 diff；
  并且它拦不住来自 fork 的合并 —— 所以真正的控制点是上面的 push protection。

### 8.4 越权测试在 fork PR 上跑不起来 —— 现在就决定怎么办

§13 把「anon key 配 RLS 配错」的缓解押在「CI 中有 anon 越权集成测试」上。
但 public repo 的 fork PR **拿不到任何 secret**，于是这个测试在 fork PR 上必然跳过
—— 而那恰恰是外部贡献者动 `policies.sql` 的时候。

> **M3 实测推翻了本节的初稿结论。**
>
> 初稿说：把 anon key 存成仓库 **Variable** 而不是 Secret，
> 「于是 fork PR 也能跑这组测试」。**后半句是错的。**
> GitHub **不把仓库 Variable 传给由 fork 的 `pull_request` 触发的工作流** ——
> 与 Secret 是同一条限制，只是 Variables 的文档页对此只字未提
> （社区讨论 #44322 里由 GitHub 员工确认）。
>
> 用 Variable 依然是对的，但真正的理由只剩一条：**Secret 会在日志里被打码**，
> 而这组测试排障时要看的正是 URL 与返回体。anon key 按设计可公开，打码只添乱。
>
> 于是 fork PR 上这组**必然跳过**，这件事无法用密钥形式绕过。
> 剩下的选择是：让它在**凭证本来就该到位**的地方（push、schedule、
> 本仓库自己的 PR）变成**硬失败**，见下。

**结论**：anon key 存仓库 Variable；`SUPABASE_TESTS_REQUIRED` 在非 fork 的
触发上置 1 —— 此时「缺凭证」是失败而不是跳过。
否则变量被误删 / 改名 / 项目被暂停时，`pytest` 只打印 `28 skipped` 而 CI 全绿，
§13 押注的这道防线就在没人盯着的稳定期（§7.2.1）悄悄消失。
fork PR 置 0：无条件置 1 会让每一个外部贡献者的第一个 PR 都红，
还附带一句「去检查仓库变量」—— 而他们看不到那些变量。

> **口径（M3 实测，初稿写错了）：** 这个部署下 anon 被拒的写入返回
> **HTTP 401**，body 是 `{"code":"42501", …, "permission denied for table …"}`。
> 初稿写的 403 是错的。而 401 **不足以分辨**「被授权层拒绝」和「密钥无效」——
> 后者同样是 401，只是 body 里没有 `code` 字段。
> 所以测试**断在 SQLSTATE `42501` 上，不断在状态码上**：
> 那是授权层自己给出的答案，而状态码只是 PostgREST 对它的翻译。

### 8.5 轮换与事故处置（初稿完全缺失）

- 凭证分两类，**必须能独立吊销**：读（anon/publishable）与写（`pipeline_writer`）。
- 写凭证疑似泄露：改 `pipeline_writer` 密码 → 更新 GitHub Secret → 手动 dispatch 一次
  验证 → 检查 `runs` 表有无异常写入。**前端不受影响**（它不用这个凭证）。
  这正是不用 `service_role` 的直接收益。
- 读凭证轮换：换 anon key → 更新 Vercel env → 重新部署。
- 每次轮换后跑一遍 §8.4 的越权测试。

### 8.6 Supabase free tier 的两个坑
- **长期无活动会自动暂停项目。** 我们每个交易日都写库，天然规避；但长假期要留意。
  缓解：非交易日也发一个极轻的 keepalive 查询（`select 1`），
  status 记为 `skipped_holiday` 但连接照常建立。
  > 注意 `daily.yml` 的 cron 是 `* * 1-5`（仅工作日），**周末根本不会跑**，
  > 所以这条 keepalive 只覆盖工作日假期。若 Supabase 的暂停阈值确实是 ~7 天，
  > 工作日假期 + 周末的最坏组合仍在阈值内；但这句话在初稿里被说得比实际覆盖面更广。
  > 保险做法：keepalive 单独挂一个周末也跑的轻量 cron。
- **存储与出站流量配额。** 我们的体量：首次回填 17 标的 × 400 根 ≈ 6800 行价格，
  之后每交易日新增 17 行；即便跑十年也只有约 4.5 万行。个位数 MB，完全不是问题。流量靠前端 ISR 缓存（§10.1）进一步压到接近零。
- 具体额度数字以 Supabase 官网当前定价页为准（免费层条款会变），实施时核对一次。

---

## 9. 数据库设计（Supabase Postgres）

### 9.1 表
```sql
-- 交易日历（由 pandas_market_calendars 的 XNAS schedule 落库，M4 的交付项）
-- 没有这张表，SQL 侧就无从判断「上一行是不是上一个交易 session」——
-- 只有 date 的话，Postgres 不知道 Juneteenth 和感恩节，
-- 用工作日差替代会把交易所假日误判为连续，§4.2 的两个字段会静默算错。
create table trading_sessions (
  date     date primary key,
  ordinal  int  not null unique,      -- 连续序号：相邻 session 的差恒为 1
  is_half_day boolean not null default false,
  close_et time not null              -- 16:00 或 13:00，§7.2 闸门 2 直接读它
);
-- 它是第五张 public 表：§9.3 的 revoke/grant/RLS 与 §9.3.2 的不变式都必须覆盖它，
-- 否则 (a) anon 可能保留写权限，(b) 收紧后 security_invoker 视图 join 不到它，
-- (c) 「所有 public 表必须开 RLS」的不变式查询会把它报为违规。
-- 运维契约见 §9.1.4 —— 没有那份契约，这张表本身就会变成静默错数的新来源。

-- 标的元数据（由 config/universe.yaml 同步而来，M4 的交付项）
create table symbols (
  symbol      text primary key check (symbol = upper(symbol)),  -- 防 aapl/AAPL 变两行
  name        text not null,
  type        text not null check (type in ('stock','etf')),
  is_benchmark boolean not null default false,
  enabled     boolean not null default true,   -- 移出 config 的标的软删除，不真删
  expects_earnings boolean not null default true,  -- ETF 为 false，§3.5(5) 的新鲜度断言靠它
  updated_at  timestamptz not null default now()
);

-- 公司事件（§3.5）。**无自然主键是故意的**：幂等性来自整窗删+插。
create table symbol_events (
  id         bigint generated always as identity primary key,
  symbol     text not null references symbols(symbol),
  event_type text not null check (event_type in ('earnings','dividend')),
  event_date date not null,
  is_estimated boolean not null,             -- 无 default：写入方必须显式表态（§3.5(2)）
  amount     numeric(14,6)
    check (event_type = 'dividend' or amount is null),
  source     text not null check (source in ('yfinance','stooq')),
  updated_at timestamptz not null default now()
);
create index on symbol_events (symbol, event_type, event_date);
create index on symbol_events (event_type, event_date);   -- 跨标的新鲜度断言走这条
-- **没有自然主键，这是故意的**（§3.5(1)）：供应商不提供稳定的事件 id，
-- 而 (symbol, event_type, event_date) 也不唯一 —— 常规分红与特别分红可以同一个除息日。
-- 幂等性来自「整窗删+插」，不是来自主键冲突 —— 与 strength_daily（§9.1.2）同一套路。
-- amount 口径：`Ticker.dividends` 返回的是**拆股调整后**的每股金额，
-- 与公告原值在有拆股的窗口里不一致（NVDA/AVGO 2024 均 10:1）。本项目只存前者。

-- 日线价格（原始事实层，保留以便任何时候重算指标）
create table prices_daily (
  symbol    text not null references symbols(symbol),
  date      date not null,
  open      numeric(14,4),
  high      numeric(14,4),
  low       numeric(14,4),
  close     numeric(14,4) check (close > 0),   -- Stooq 来源时为 NULL，见 §3.0 规则 3
  adj_close numeric(14,4) not null check (adj_close > 0),
  adj_factor numeric(18,10)
    generated always as (adj_close / nullif(close, 0)) stored,  -- §3.0 规则 2 的比对依据
  volume    bigint,
  source    text not null check (source in ('yfinance','stooq')),
  updated_at timestamptz not null default now(),
  primary key (symbol, date)
);
create index on prices_daily (date desc);

-- 指标（派生层，可随时由 prices_daily 全量重算）
create table metrics_daily (
  symbol   text not null references symbols(symbol),
  date     date not null,
  -- 核心指标：显式列，便于索引与排序
  rsi_14              numeric(8,4),     -- 有界 0–100，定精度安全
  ema_60              numeric(14,4),
  close_vs_ema60_pct  numeric(10,6),
  ema60_slope_20d     numeric(10,6),
  alpha_annual        numeric,          -- 无自然上界 → 不定精度（见下）
  beta                numeric(10,6),
  r2                  numeric(8,6),
  corr                numeric(8,6),
  resid_vol_annual    numeric,          -- 同上
  alpha_t_stat        numeric(10,4),
  n_obs               int,
  mom_20              numeric(10,6),
  -- 事件距离（§3.5），单位是**日历日**，无事件时为 NULL
  -- **只在最新一个 session 的行上有值，历史行一律 NULL**（§3.5(3)）
  days_to_next_earnings    int,
  days_since_last_earnings int,
  days_to_next_dividend    int,
  days_since_last_dividend int,
  next_earnings_date       date,    -- 存日期才能渲染 tooltip，且让数字可审计
  next_dividend_date       date,
  next_earnings_is_estimated boolean,
  next_dividend_is_estimated boolean,
  -- 未来新增指标的落脚点：加指标不必做 schema 迁移
  extra    jsonb not null default '{}'::jsonb,
  -- 按指标而非按行的预热标记（§3.3）
  provisional_metrics text[] not null default '{}',
  computed_at timestamptz not null default now(),
  primary key (symbol, date)
);
create index on metrics_daily (date desc);

-- 每日完整排名（全量历史，全池全部名次，见 §4.4）
create table strength_daily (
  date            date not null,
  symbol          text not null references symbols(symbol),
  rank            int  not null check (rank >= 1),
  score           numeric(12,6) not null,
  -- 当时生效的全部口径，缺一则历史行不可比
  score_metric    text not null,
  rank_pool       text not null check (rank_pool in ('stocks','all')),
  benchmark       text not null references symbols(symbol),
  top_n           int  not null,       -- 当时的榜单深度（config 可改）
  in_top_n        boolean not null,    -- 写入时由 pipeline 判定，视图不必知道 top_n
  delta_to_next   numeric(12,6),
  delta_to_median numeric(12,6),
  primary key (date, symbol),          -- 而不是 (date, rank)
  unique (date, rank)                  -- 同一天同一名次只能有一个标的
);
create index on strength_daily (symbol, date desc);  -- v_strength_enriched 的窗口按 symbol 分区

-- rank_delta_1d 与 days_in_top_n 不入库，读取时用窗口函数现算（§4.2）
-- 关键：必须按**全部排名口径字段**分区，否则跨配置变更会编造出假的名次变动
--（在 stocks 池里排第 2 和在 all 池里排第 2 不是一回事）。
-- 必须是可执行的完整 SQL：它要进 0001_init.sql，不能留省略号。
create view v_strength_enriched with (security_invoker = true) as
with b as (
  select s.*, ts.ordinal,
         lag(s.rank)     over w as prev_rank,
         lag(ts.ordinal) over w as prev_ord,
         lag(s.in_top_n) over w as prev_in
  from strength_daily s
  join trading_sessions ts on ts.date = s.date
  window w as (
    partition by s.symbol, s.score_metric, s.rank_pool, s.benchmark, s.top_n
    order by ts.ordinal
  )
),
g as (
  select b.*,
         sum(case when prev_ord is null            -- 首行
                    or ordinal - prev_ord <> 1     -- 跨了非连续 session（含长假、缺数据）
                    or prev_in is not true         -- 昨天不在榜
                    or in_top_n is not true        -- 今天不在榜
                  then 1 else 0 end)
           over (partition by symbol, score_metric, rank_pool, benchmark, top_n
                 order by ordinal rows unbounded preceding) as streak_id
  from b
)
select date, symbol, rank, score, score_metric, rank_pool, benchmark, top_n,
       in_top_n, delta_to_next, delta_to_median,
       -- 只有相邻 session 之间才谈得上「名次变动」
       case when prev_ord is not null and ordinal - prev_ord = 1
            then prev_rank - rank end as rank_delta_1d,
       case when in_top_n then
              row_number() over (partition by symbol, score_metric, rank_pool,
                                              benchmark, top_n, streak_id
                                 order by ordinal)
            end as days_in_top_n
from g;
-- 连续性判据是 `ordinal - lag(ordinal) = 1`，不是日期相减 ——
-- 这正是需要 trading_sessions.ordinal 的原因，仅凭 date 做不到。
grant select on v_strength_enriched to anon;   -- 见 §9.3 第 2 步

-- 运行日志（可观测性）—— 放在 PostgREST 不暴露的 private schema，见 §9.3.1
create schema if not exists private;
create table private.runs (
  id          bigint generated always as identity primary key,
  session_date date,
  status      text not null check (status in (
                'running','ok','skipped_holiday','skipped_too_early',
                'skipped_already_done','ok_events_stale',
                'stale_vendor','partial','failed')),
  started_at  timestamptz not null,
  finished_at timestamptz,
  rows_prices int, rows_metrics int,
  git_sha     text,
  message     text
);

-- ── 以下必须放在所有建表语句**之后** ──────────────────────────────

-- 事件抓取的节奏状态（§3.5(4)）。放 private，不对 anon 暴露。
create table private.fetch_state (
  symbol              text primary key references symbols(symbol),
  last_event_fetch_at timestamptz
);

-- updated_at / computed_at 触发器（§9.2.1）。migration 里必须真的有这段。
create function touch_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;
create function touch_computed_at() returns trigger language plpgsql as $$
begin new.computed_at = now(); return new; end $$;

create trigger t_symbols_touch       before update on symbols
  for each row execute function touch_updated_at();
create trigger t_prices_touch        before update on prices_daily
  for each row execute function touch_updated_at();
create trigger t_events_touch        before update on symbol_events
  for each row execute function touch_updated_at();
create trigger t_metrics_touch       before update on metrics_daily
  for each row execute function touch_computed_at();
-- 注意 metrics_daily 用的是 computed_at 而不是 updated_at —— 别共用一个触发器。
```

### 9.1.1 上面这版 schema 相对初稿改了什么，以及为什么

| 改动 | 原因 |
|---|---|
| `strength_daily` 主键 `(date, rank)` → **`(date, symbol)` + `unique(date, rank)`** | 原设计下没有任何东西阻止同一标的出现在两个名次上。一个并列处理或 off-by-one 的 bug 会产出「NVDA 同时是第 2 名和第 3 名」的榜单，而 `(date, rank)` 的 upsert **检测不到**。 |
| `rank_delta_1d` / `days_in_top_n` 从列改为**视图现算** | 它们是「其他行的函数」。一旦反规范化，§3.0 的回补重算改了某个历史日的名次，其后所有天的连续计数链就错了，而没人会重算它们 —— 卡片上「在榜 5 天」会悄悄撒谎。 |
| `provisional boolean` → **`provisional_metrics text[]`** | §6.2 的 `provisional_below` 是**按指标**定的（RSI 250 / EMA 180）。一个行级 boolean 只能在「整行打灰（把可信的 RSI 也灰掉）」和「都不打灰（违反 §10.4）」之间二选一。这个迁移如果不在 M3 做，就会卡在 M7 中间做。 |
| `alpha_annual` / `resid_vol_annual` 去掉定精度 | `numeric(10,6)` 上限 9999.999999。这两个量没有自然上界，一旦溢出，Postgres 中止的是**整批 insert** —— 「一个坏格子毁掉一整天的数据」，对一个无界列是很差的交换。 |
| `runs.status` 加 `check` + 新增 `running` | 合法值原本只写在 SQL 注释里。一个 `'sucess'` 的拼写错误会让前端判活失败、黄条亮起，而你会先去查错层。另外 `status not null` 意味着行是在**结束时**插入的 —— 于是 runner OOM / 超时 / 被取消时**一行都不会留下**，可观测性表对硬崩溃完全失明。改为开跑即插 `running`，结束时改写。 |
| `source` / `rank_pool` 加 `check` | 同上，枚举值要由数据库强制，不是靠注释。 |
| `symbol` 加 `check (symbol = upper(symbol))` | 否则 `aapl` 与 `AAPL` 是两个外键上不同的行。 |
| `bigserial` → `generated always as identity` | 现代写法，避开序列归属的怪异行为。 |
| `strength_daily (symbol, date desc)` 索引 | `v_strength_enriched` 的窗口按 `symbol` 分区并按 session 排序，主键 `(date, symbol)` 对它是反向的。今天数据量小无所谓，但索引是免费的。 |
| `prices_daily.adj_factor` 生成列 | §3.0 规则 2 要比对「昨日行的复权因子是否变了」，需要它落库。 |

### 9.1.4 `trading_sessions` 的运维契约（没有它，这张表自己就是新的错数来源）

schema 本身不足以保证正确 —— `ordinal int unique` 只保证唯一，**不保证无缺口**。
以下四条必须写死：

1. **每次 `run_daily` / `backfill` 计算之前先跑 `sync_sessions.py`**，
   顺序是「同步日历 → 闸门判定 → 抓取 → 计算」。闸门 1 和闸门 2 都读这张表，
   而日历包会随新公布的假日更新 —— 用一张过期的表做闸门判定毫无意义。
2. **幂等的全量对账，而不是增量追加。** 每次从 `sessions_start_date` 到
   `今天 + sessions_horizon` 重新生成整张表，`ordinal` 由排序后的 session 序列
   **重新推导**，而不是沿用旧值。只有这样才能保证无缺口。

   > **`sessions_start_date` 是固定的，永不前移 —— 这是整套方案的支点。**
   > 因为 `ordinal` 每天都重新推导，它的正确性完全押在左端点不动上。
   > 若把它写成「从今天往回数 400 个 session」这种滚动写法，随着时间推移左端点前移，
   > 老的 `strength_daily` 行会失去对应的 session 行；而 `v_strength_enriched`
   > 用的是 **INNER** join，那些行会**无声地从视图里消失**；
   > 更糟的是幸存的第一行 `prev_ord is null` → `streak_id` 递增 →
   > **一只在榜数周的股票，`days_in_top_n` 悄悄归 1**。
   > 这正是 §4.2 建这个视图要防的那句「卡片上那个『在榜 5 天』会悄悄撒谎」。
   >
   > 取值：`sessions_start_date` 要早于首次回填的最早一天，留足裕量
   > （`lookback_bars` + 一年）。它只增不减，**只能往过去挪，不能往未来挪**。

   配套不变式（进 `invariants.sql`）：
   **每一个 `strength_daily.date` 都必须在 `trading_sessions` 里存在** ——
   等价于 `v_strength_enriched` 的行数必须等于 `strength_daily` 的行数。
   这条查询就是上面那个静默故障的探测器。
3. **日历修订要能被发现，并且价格层也要一起对账 —— 不能只重算派生层。**
   对账时比对新旧两版：
   - **只是未来地平线延长** → 无需任何重算。
   - **历史日发生增减**（新公布的假日、临时休市如国葬日，或日历包修正了旧数据）
     → 从**最早发生变化的那个 session 起**做一次完整修复，顺序不能颠倒：
     1. **先补价格层**：对受影响区间重新抓取并 upsert `prices_daily`。
        新增一个历史 session 而不补价格，后续重算会在一个**有缺口的价格序列**上
        数「20 个 session」—— 换了一个错误的行，且所有闸门全绿。
     2. **再清理多余行**：日历里已不存在的日期，其 `prices_daily` 行要么删除，
        要么被计算侧忽略。
     3. **最后重算** `metrics_daily` 与 `strength_daily`（该 session 之后全部）。
        因为 `ordinal` 变了，「20 个交易日前」和「相邻 session」两个判断的答案都变了。
4. **指标的输入必须显式按 `trading_sessions` 过滤，而不是「`prices_daily` 里有什么就用什么」。**
   这是上面第 2 步的兜底：即便有陈旧行残留在价格表里，它也进不了计算窗口。
   这条同时让「日历是唯一事实来源」成为结构性保证，而不是靠对账脚本跑对。
5. 对账过程记入 `private.runs.message`，变更触发的重算记为 `partial` 而非静默进行。

> 这不会造成无限重算：地平线延长被显式排除在重算之外，
> 历史修订是一次**有界的**修复（修完两版日历就一致了，下次对账无差异）。

> 失败场景（不加这份契约就会发生）：交易所在 11 月公布一个新的临时休市日，
> 日历包更新了，我们的表没更新 —— 那天的闸门 1 会判为「交易日」，
> 闸门 3 会因为没有新 bar 判为 `stale_vendor` 并告警（这是好的）；
> 但**更坏的情况**是反过来：表里多了一个实际没开市的日期，
> `ordinal` 整体后移，于是 `mom_20` 的「20 个 session 前」指到了错误的行，
> 而所有闸门全绿。

### 9.1.2 写入的原子性：必须走一个事务，不能走 PostgREST

三个问题串在一起：

1. `strength_daily` 的重跑必须是「**先 `delete from strength_daily where date = $1`，
   再插入**」。只 upsert 不删，当某次重跑产出的标的数比上次少时（例如 §7.2 闸门 3
   排除了两只），上一次的尾部名次会**存活下来**，显示出的榜单是**两次计算的拼接**，
   而这恰恰是 §7.2「重跑、补跑…结果都一样」承诺要保证的那张表。
2. **PostgREST 做不了多语句事务。** 所以不只是删+插不原子，
   `prices_daily → metrics_daily → strength_daily → runs` 这整条链都不原子 ——
   在第 2、3 步之间崩溃，会留下「昨天的榜单」配「今天的指标」，且无任何标记。
3. 初稿说「所有写入用 `ON CONFLICT (symbol, date)`」对 `strength_daily`
   和只追加的 `runs` 都是错的。

→ **走 psycopg 直连**（§8.1.1 已经因为爆炸半径把连接串准备好了）。

**但不能是「整天一个事务」—— 必须是三个事务。**
如果把 `runs` 的 `running` 行也放进那个大事务里，OOM / 超时 / 被取消时
它会和数据一起回滚，于是「开跑即插 `running`」根本没有解决 §7.2 说的那个
可观测性盲区（硬崩溃时一行都不留）。正确的切法：

| 事务 | 内容 | 目的 |
|---|---|---|
| T1 | `insert into runs (status='running', started_at)` 并**立即提交** | 崩溃也留痕；dead-man's switch 有依据 |
| T2 | `prices_daily` → `metrics_daily` → `strength_daily`（含 delete+insert）**一个原子单元** | 不会出现「昨天的榜单配今天的指标」 |
| T3 | `update runs set status=..., finished_at=...` 并提交 | 终态 |

崩溃落在 T2 中间 → 数据完整回滚，而 `runs` 里留下一行永远停在 `running` 的记录
—— **这正是我们想要的信号**：下一跑看到本日存在未完成的 `running` 行即可判定上一跑硬崩。

（如果坚持留在 PostgREST 上，替代方案是把 T2 包成一个 Postgres 函数用 `rpc()` 调用；
T1/T3 仍各自独立。）

### 9.1.3 NaN 绝不能进入写入路径

**这是第一次 backfill 就会撞上的问题，不是理论风险。** 每个预热 bar 都产 NaN：
RSI 前 14 根没有值，EMA(60) 前 59 根没有，`mom_20` 前 20 根没有，
alpha/beta 在 `min_obs: 120` 以下没有。§12 #5 回补 400 根 →
**每个标的约 126 个前导行 × 17 个标的**全都带 NaN
（最深的预热是 alpha/beta 的 127 根窗口；250 是 RSI 的 `provisional_below`，
那是「出值但标灰」，不是 NULL —— §3.3 写这条就是为了防止这两者被混用）。两种失败都近乎必然：

- 走 REST：Python `json.dumps` 默认 `allow_nan=True`，会吐出裸 `NaN` ——
  **那不是合法 JSON**。PostgREST 拒绝整个 body，整批失败，`runs.status = failed`，
  M4 的「回补成功」永远过不了，而原因一点都不明显。
- 若强行塞进去：Postgres `numeric` **接受** `'NaN'`，于是 `rsi_14 = NaN` 被存下来，
  在 `order by rsi_14 desc` 里排在**所有数字之前**，
  读出来又被 PostgREST 序列化成非法 JSON，把前端打挂。

`extra` jsonb 列有完全相同的问题。

→ **写入边界上的一条硬规则：`NaN` / `±Inf` 永不写入，一律转成 SQL `NULL`。**

> **M4 实测修正：初稿括号里写的 `df.replace([np.nan, np.inf, -np.inf], None)`
> 满足不了本节自己提的要求。**
> 实测（pandas 3.0.6）它在**普通数值列上有效** —— 列被提升成 object，
> 三种非有限值都变成真正的 `None`。但它够不到两处，而两处本节都点了名：
> ① **`extra` jsonb 列** —— `replace` 不走进嵌套 dict，
> `{'extra': {'x': nan}}` 原样穿过去，`json.dumps` 随即吐出裸 `NaN`；
> ② **不经过 DataFrame 的值** —— 事件列、`runs` 的计数、手工拼的行，
> 它们没有 `.replace` 可调。
>
> 正确的实现是一个**记录级的窄入口**（`pipeline/sanitize.py` 的
> `clean_records` / `clean_value`）：所有进库的行都过它，
> 于是「哪里可能漏掉 sanitize」这个问题有唯一的答案。
> DataFrame 路径上再顺手 `replace` 一次无妨，但正确性不依赖它。

Python 侧这一条是真正管用的那一条；数据库侧的 `check` 约束可以再加一层，
但 `numeric` 的 NaN 比较语义要先核实再写。

### 9.2 为什么是「显式列 + jsonb extra」的混合
纯 jsonb → 排序/筛选要写 `(extra->>'rsi_14')::numeric`，前端排序一慢就完蛋。
纯显式列 → 每加一个指标都要迁移，与「参数未来会改」的诉求冲突。
混合：**config 里标了 `core: true` 的指标占显式列，其余自动落 `extra`**，
两边都舒服。将来某个 extra 指标用得多了，再提升为显式列（一次迁移，一次回填）。
CI 检查见 §6.2 的三条 —— 关键是它必须是**双向**的：
初稿只检查「声明 → 列」，于是「§3 定义了但 config 没声明」这半类漂移仍然敞着
（`ema60_slope_20d` 正是这样漏掉的）。
同理，`extra` 的键也要校验其对应一个已声明的非核心指标。

### 9.2.1 `updated_at` / `computed_at` 需要触发器，光靠 `default` 不会更新

列的 `DEFAULT now()` **只在 INSERT 时生效**。`ON CONFLICT ... DO UPDATE`
不会重新触发它 —— 必须在 `SET` 列表里显式列出。

失败场景很具体：你在排查「AAPL 这行是不是陈旧了」，`updated_at` 显示 2026-09-20，
而这行昨天刚被重写过 —— **你唯一会去看的那一列恰好在撒谎**。
在 §3.0 规则 2 的滚动重写下这更要命，因为「某个复权基准是什么时候变的」
正是那时的核心诊断线索。

→ `symbols` / `prices_daily` / **`symbol_events`** 各加 `BEFORE UPDATE` 触发器写 `updated_at`；
（`symbol_events` 的写入走的是**整窗删+插**而不是 upsert（§3.5(1)），
所以那张表的 `updated_at` 其实由 INSERT 的 default 就能写对；
触发器留着是为了将来任何手工 UPDATE 也不会让它撒谎 —— 别据此写出 `ON CONFLICT`，
这张表没有可用的自然冲突目标）；
**`metrics_daily` 没有 `updated_at` 列，它的时间戳叫 `computed_at`**，
所以它要用一个写 `computed_at` 的触发器（语义是「这行是什么时候算出来的」）。
初稿这一段把三张表写成同一个触发器，会直接让迁移失败。
用触发器而不是在每条 upsert 里手写 `set ... = now()`，因为触发器扛得住「有人忘了写」。

### 9.3 访问控制：**GRANT 与 RLS 是两道，初稿只写了一道**

在 Postgres 里，`GRANT` 授权的是**命令**，RLS 过滤的是**行**，两者都必须满足。
初稿只写了 policy，从没写过 `grant select ... to anon` —— 它今天能跑，
纯粹是因为 Supabase 建项目时执行过
`alter default privileges in schema public grant all on tables to postgres, anon, authenticated;`，
所以在 SQL Editor 里以 `postgres` 建的表隐式继承了授权。

这带来两个独立的缺陷：

**(a) 缺失的 GRANT 是一颗前端定时炸弹。** 一旦那套默认权限变了，
或某次迁移由另一个角色执行，前端会以
`42501 permission denied for table prices_daily` 直接死掉，
而计划里没有任何一句话能解释为什么。

**(b) 同一套隐式默认意味着 `anon` 此刻对**六张表**都持有 INSERT / UPDATE / DELETE 授权。**
「不给策略 → 默认拒绝」只在 RLS 开着且没有放行写策略时成立。
**完全没有纵深防御**：只要有人在 SQL Editor 里敲一句
`alter table ... disable row level security`，或未来某次「修复」加了一条
`for all using (true)`，任何持有（按设计可公开的）anon key 的人就能
`DELETE FROM prices_daily`。§13 已经把这个后果标成「**数据可被任意写入**」，
而它列出的唯一缓解是「RLS 为强制项」—— 也就是刚刚失效的那一件东西。

```sql
-- 1) 先收权：把隐式继承来的写权限拿掉（authenticated 一并处理，虽然今天没有登录态）
--    注意 alter default privileges 只对「执行它的那个角色此后创建的对象」生效，
--    不会追溯修正其他角色的默认权限 → 必须固定一个迁移属主角色并写明 FOR ROLE。
alter default privileges for role postgres in schema public
  revoke all on tables from anon, authenticated;
revoke all on trading_sessions, symbols, symbol_events,
              prices_daily, metrics_daily, strength_daily
  from anon, authenticated;

-- 2) 再显式授予前端需要的读（含 §9.1 那个派生视图，否则前端拿不到名次变动；
--    也必须含 trading_sessions，否则 security_invoker 视图 join 它会被拒）
grant select on trading_sessions, symbols, symbol_events,
                prices_daily, metrics_daily, strength_daily
  to anon;
grant select on v_strength_enriched to anon;

-- 3) RLS 作为第二道，而不是唯一一道（**六张表**，一张都不能漏）
alter table trading_sessions enable row level security;
alter table symbol_events    enable row level security;
alter table symbols          enable row level security;
alter table prices_daily     enable row level security;
alter table metrics_daily    enable row level security;
alter table strength_daily   enable row level security;

create policy "public read" on trading_sessions for select to anon using (true);
create policy "public read" on symbol_events    for select to anon using (true);
create policy "public read" on symbols          for select to anon using (true);
create policy "public read" on prices_daily     for select to anon using (true);
create policy "public read" on metrics_daily    for select to anon using (true);
create policy "public read" on strength_daily   for select to anon using (true);

-- 4) 写入角色的授权。**角色在本文件之前已由管理 token 建好**（§8.2 / §12 #8），
--    本文件只做授权，不含 create role、
--    不含密码。（`:'var'` 是 psql 的变量语法，SQL Editor 不认，别写进迁移。）
grant usage on schema public to pipeline_writer;

grant select, insert, update         on symbols, prices_daily, metrics_daily
  to pipeline_writer;
grant select, insert, update, delete on symbol_events to pipeline_writer;
grant select, insert, update, delete on strength_daily   to pipeline_writer;
grant select, insert, update, delete on trading_sessions to pipeline_writer;
-- ↑ 需要 DELETE 的有**三张**：
--   · strength_daily —— §9.1.2 的删+插重写；
--   · trading_sessions —— §9.1.4 的全量对账（删+重插，ordinal 每次重新推导）；
--   · symbol_events —— §3.5(1) 的整窗删+插。与 trading_sessions 同一个理由：
--     它可从供应商完整再生（`Ticker.dividends` 给全历史），删得起。
--     因此其 delete 策略用 `using (true)`，与其他两张一致 ——
--     不要写成受限谓词，否则整窗删+插会静默删不完而留下孤儿行。
--     它是可从日历包完整再生的数据，删得起。
--   prices/metrics/symbols 是**原始事实层与可重算派生层**，写入角色被攻陷时
--   不该有能力把它们抹掉 —— 那正是 §8.1.1 要缩小的爆炸半径。
-- runs 在 private schema，见 §9.3.1。

-- RLS 策略必须与上面的动词一一对应（**六张表都要**）。
-- 漏掉任何一张的 writer 策略，RLS 默认拒绝 → INSERT 报
-- "new row violates row-level security policy" → 那是在 T2 里（§9.1.2）
-- → **整个当日事务回滚、当天零数据**。上一轮 trading_sessions 就是这么掉的。
-- 下面是完整清单，**不要用省略号代替** —— 本节就是 0001_init.sql 的正文。

-- 五张只读写不删的表
create policy "w sel" on symbols          for select to pipeline_writer using (true);
create policy "w ins" on symbols          for insert to pipeline_writer with check (true);
create policy "w upd" on symbols          for update to pipeline_writer using (true);
create policy "w sel" on prices_daily     for select to pipeline_writer using (true);
create policy "w ins" on prices_daily     for insert to pipeline_writer with check (true);
create policy "w upd" on prices_daily     for update to pipeline_writer using (true);
create policy "w sel" on metrics_daily    for select to pipeline_writer using (true);
create policy "w ins" on metrics_daily    for insert to pipeline_writer with check (true);
create policy "w upd" on metrics_daily    for update to pipeline_writer using (true);

-- 三张需要 DELETE 的表：strength_daily / trading_sessions / symbol_events
create policy "w sel" on strength_daily   for select to pipeline_writer using (true);
create policy "w ins" on strength_daily   for insert to pipeline_writer with check (true);
create policy "w upd" on strength_daily   for update to pipeline_writer using (true);
create policy "w del" on strength_daily   for delete to pipeline_writer using (true);
create policy "w sel" on trading_sessions for select to pipeline_writer using (true);
create policy "w ins" on trading_sessions for insert to pipeline_writer with check (true);
create policy "w upd" on trading_sessions for update to pipeline_writer using (true);
create policy "w del" on trading_sessions for delete to pipeline_writer using (true);
create policy "w sel" on symbol_events    for select to pipeline_writer using (true);
create policy "w ins" on symbol_events    for insert to pipeline_writer with check (true);
create policy "w upd" on symbol_events    for update to pipeline_writer using (true);
create policy "w del" on symbol_events    for delete to pipeline_writer using (true);
-- symbol_events 的 delete 用 using (true)，与另两张一致：它可从供应商完整再生
-- （`Ticker.dividends` 给全历史），删得起。**不要写成受限谓词** ——
-- 那会让 §3.5(1) 的整窗删+插静默删不完，反而留下孤儿行。

-- private schema（不走 RLS，靠 PostgREST 不暴露 + 无 anon 授权保护）
grant usage on schema private to pipeline_writer;
grant select, insert, update on private.runs, private.fetch_state to pipeline_writer;
```

> 两条不要照抄旧稿的地方：
> - **不加 `FORCE ROW LEVEL SECURITY`。** FORCE 影响的是**表属主**；
>   `pipeline_writer` 不是属主，它本来就受 RLS 约束。运行时不会用属主身份，
>   所以 FORCE 在这里不增加任何防护，只增加策略维护的失败面。
> - `runs.id` 用的是 `generated always as identity`（不是 `serial`），
>   **identity 列的内部序列不需要单独 `grant usage`**，表上的 INSERT 权限即可 ——
>   这正是 §9.1 选 identity 而不是 `bigserial` 的实际收益之一。

> *需在真实项目上核实一次*：
> `select grantee, privilege_type from information_schema.role_table_grants
> where table_schema='public';` —— 确认收权后 `anon` 只剩 SELECT。

### 9.3.1 `runs` 不走 SECURITY DEFINER 视图，改放 `private` schema

初稿用一个 `security_invoker = false` 的 `v_last_run` 视图把 `runs` 的三列暴露给 anon。
这**能工作**（`security_invoker` 是 PG 15 的视图选项，`false` 是默认值；
以视图属主 `postgres` 读 `runs`，而表属主默认豁免自己的 RLS），但它有三个边角问题：

- Supabase 的 Security Advisor 会把它标为 `security_definer_view` 告警。
  将来有人「修掉这个告警」把它翻成 `security_invoker = true`，
  视图会**返回 0 行且不报错** —— 前端 header 的「数据截至」悄悄变空、
  黄条为一个并不存在的数据问题而亮起。**响亮的失败很便宜，沉默的失败很贵。**
- 经 SQL Editor 建视图后要追加 `notify pgrst, 'reload schema';`，否则端点 404。
- `runs` 开了 RLS 但没有 anon 策略时，它仍会出现在自动生成的 OpenAPI schema 里，
  `GET /rest/v1/runs` 返回 `[]` + HTTP **200**（不是 403）—— 无害，但对外广告了这张表的存在。

**→ 已采纳的做法**：`runs` 的规范位置就是 `private.runs`（§9.1 的建表语句已按此写），
PostgREST 不暴露 `private` schema；
前端的「数据新鲜度」直接取 `select max(date) from metrics_daily`
—— 一张 anon 本来就在读的表。这一步同时删掉了：那个视图、唯一的 SECURITY DEFINER 对象、
那条 lint 告警，以及上面的 schema 泄露噪音。

配套要点（漏一条就会出错）：
- `runs` **不出现**在 §9.3 的 public 表 `revoke` / `grant` / `enable RLS` 列表里 ——
  它根本不在 `public`。
- 写入角色需要 `grant usage on schema private to pipeline_writer;`
  加 `grant select, insert, update on private.runs, private.fetch_state to pipeline_writer;`
  （两张都不给 DELETE）。
- §9.3.2 的「所有表必须开 RLS」不变式只扫 `public`，这是对的：
  `private` 的保护来自「PostgREST 不暴露 + 无 anon 授权」，不来自 RLS。

### 9.3.2 建表默认**不**开 RLS —— 需要一条会自己报警的不变式

Supabase 仪表盘的 *Table Editor* 默认勾选「Enable RLS」，
但在 *SQL Editor* 里 `create table` **不会**。叠加上面的 (b)，
任何在 `0002+` 迁移里新增的表都会带着「RLS 关闭 + anon 持有 ALL」上线。
§9.2 明确计划了未来的迁移（「某个 extra 指标提升为显式列」），
而 §11.5 的 review 闸门审的是**代码 diff，不是线上数据库状态**。

失败场景：半年后为一个新视图加了 `sectors` 表，没人记得敲
`enable row level security` —— 这张表从此对全世界可读可写，
而计划里没有任何测试、CI 步骤或 review 会发现。

→ **把它变成断言，而不是靠人记得**：
```sql
-- 必须返回 0 行
select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity;
```
外加一条「不存在 anon 写授权」的查询。两条都进 CI。
并且把「跑一次 Supabase Security Advisor / `db lint` 并归档输出」
写成 §11.5 中任何触及 `supabase/` 的里程碑的固定动作。

### 9.3.3 集成测试（§13 押注在它身上，所以要写死）
- anon `insert into metrics_daily ...` → 必须拿到 **`42501`**（本部署下 HTTP 401，见 §8.4）。
- anon `select` **六张公开表逐一列举**（`trading_sessions` / `symbols` / `symbol_events`
  / `prices_daily` / `metrics_daily` / `strength_daily`）+ `v_strength_enriched`
  → 全部必须成功。
  （这条测的是 §9.3 的 GRANT，不只是 policy；漏掉 `trading_sessions` 这一项，
  它缺授权时测试仍会通过，但 `security_invoker` 视图会在生产上 join 不到它而崩。）
- anon 访问 `runs` → 必须不可达。
- §9.3.2 的两条不变式查询 → 必须返回 0 行。

**以上全是「够不够得着」的断言，一个返回 0 行的视图能全部通过。**
§9.3.1 已经为它删掉的那个视图诊断过这个失败模式
（「返回 0 行且不报错 …… 响亮的失败很便宜，沉默的失败很贵」），
但那段推理没有被搬到 `v_strength_enriched` 上 —— 而它现在是唯一的视图、
面向 anon、且依赖一个每次运行都被删掉重建的表的 INNER join。
所以必须补上**内容级**断言（同样进 `invariants.sql`）：

| 断言 | 它探测什么 |
|---|---|
| `v_strength_enriched` 在 `max(date)` 上的行数 == 排名池大小 | 视图被改坏 / 分区列写错 |
| `v_strength_enriched` 总行数 == `strength_daily` 总行数 | **S4 的左端点前移**（INNER join 静默吞行） |
| `in_top_n` 为真的行，`days_in_top_n >= 1` | 连续天数计算退化 |
| `max(metrics_daily.date)` 与最近一个 session 相差 ≤ 1 | 管道静默停摆 |
| 抽样若干日期，用 `metrics_daily.mom_20` 重排能**精确复现** `strength_daily.rank` | §3.0 规则 2 的三层重写是否真的同步 |
| 每个标的未来 earnings 行最多一条 | **改期遗留的孤儿预告行**（§3.5(1)） |
| `expects_earnings 且 enabled` 的标的：有未来财报行，或 10 天内刚报过 | 事件数据静默断流（§3.5(5)） |
| 最新行 `days_to_next_earnings` == `(min 未来 event_date) - date` | 派生值与事实表脱钩 |
| **历史行的事件列必须全为 NULL** | 滚动改写悄悄回来（§3.5(3)） |

失败场景：某个 `0002` 迁移重建视图时分区列写漏了一个，迁移干净提交、
`invariants.sql` 返回 0 行、§9.3.3 全过、403 态也不会触发（因为根本没有 403）——
而首页的「今日三强」那条横条就是空的，或者每只股票都显示「在榜 1 天」。
- 执行方式见 §8.4（anon key 存成仓库 Variable —— 为的是不被日志打码，**不是**为了 fork PR；fork PR 拿不到 Variable）。

### 9.4 迁移管理
`supabase/migrations/NNNN_*.sql` 存在仓库里（顺序编号 + 幂等写法）。
本期手动在 Supabase SQL Editor 执行，但有三条硬性要求：

1. **没有 staging（§12 #9），所以四条补偿措施是强制的** —— 见 §12 的「不建 staging
   的补偿措施」。其中最关键的一条：`0001_init.sql` 必须在**库里还没有任何数据时**执行。
2. **每个迁移文件用 `begin; ... commit;` 包起来**，并在同目录放同名的
   `NNNN_rollback.sql`。**没有回滚脚本的迁移不允许执行。**
3. **迁移跑完立刻执行 `invariants.sql` + §9.3.3 全套集成测试**，不等下一次 CI ——
   直接上生产时，「改完马上验」就是唯一的安全网。
4. 涉及视图 / schema 变更的迁移末尾追加 `notify pgrst, 'reload schema';`。

迁移频繁起来再上 `supabase db push` + `SUPABASE_ACCESS_TOKEN`。

**备份**：几乎所有数据都能从供应商重新推导，所以影响低 ——
但这是一个值得**讲出来**的论证，而不是省略掉。
免费层的自动备份覆盖范围需实施时核实；
一个月度 `pg_dump` 的 workflow 近乎免费，买的是「供应商历史口径也一起变了」时的后悔药。

> **实施记录（2026-09-21）：这份备份一开始一次都没成功过。**
> 手动 dispatch 一次 heartbeat 才发现（否则要到 10-01 第一次定时跑才暴露），
> 一层层剥出五个问题：客户端 16 对不上服务端 17.6；runner 自带源里最新只到 16；
> 装上 18 之后 `pg_wrapper` 仍按默认集群派发出 16；整库 dump 撞 Supabase 托管
> schema 的权限；写入角色读不了序列。**而最后一层不是配置问题** ——
> 六张表都开了 RLS，`pipeline_writer` 既不是 owner 也没有 BYPASSRLS，
> 于是 pg_dump 拒绝导出。那个拒绝是对的：带 RLS 导出只会得到一份静默残缺的备份。
>
> 解法**不是**给 pg_dump 加 `--enable-row-security` 让它闭嘴，而是给备份一个够用
> 的身份：`0002_backup_reader.sql` 建了一个**只读 + bypassrls**的专用角色，
> 连接串存在 `MARKME_BACKUP_URL`。dump 只覆盖 `public` 与 `private`
> —— `auth` 不是我们的，而且那里面是用户凭证，不该躺进一个保留 90 天的 artifact。
>
> workflow 末尾会 `pg_restore --list` 数一下带数据的表（期望 ≥ 8），
> 因为「命令成功了」不等于「备份是完整的」：schema 导出来了、数据一行没有，
> 同样是 exit 0。首次成功的产物：486 KB dump / 455 KB artifact。

---

## 10. 前端设计

### 10.1 技术选型
Next.js 15 App Router + TypeScript + Tailwind + Server Components。
数据读取放在 **服务端**。首页按请求渲染（`dynamic = "force-dynamic"`），而不是页面级 ISR：

- 用户浏览器不直连 Supabase → anon key 不进浏览器包；
- 每个 Supabase `fetch` 显式带 `next: { revalidate: 3600 }`，所以缓存的是 **Data Cache**，
  不会因页面动态化而把每次访问都变成出站请求；
- 首屏仍是服务端 HTML，无 loading 闪烁；
- 陈旧天数每次请求重算，不会被 Full Route Cache 中一份旧页面永久冻结。

这避免了页面级 ISR 的 stale-while-revalidate 失败模式：数据源失联时，热 ISR
条目仍会以 200 返回旧价格和旧的「未陈旧」判断。Data Cache 本身仍是
stale-while-revalidate；这是可接受的，因为缓存内的价格及其 `as of` 日期仍为真。
冷缓存的真实读取失败则 fail closed 为 500。监控「数据源此刻可用吗」由不缓存的、
token 鉴权的 `/api/health` 承担；它每次直读一行并在数据库/授权异常时返回 503。

**Data Cache 的 `revalidate = 3600` + 按需重验证**，不是初稿的 `revalidate = 300`。
对一个一天只变一次的数据源，300 秒意味着**每天 288 次重新验证**，
与「出站流量接近零」自相矛盾，而且烧的是 Vercel 的函数额度。
→ 日常任务写库成功后 POST 一次 `/api/revalidate?token=...`。
**必须断言响应是 200**，非 200 升为 `partial` —— 否则一个 307 跳转会被当成成功（§10.5）。
代价是多一个 secret，换来的是**管道写完几秒内**页面就更新，
而不是「什么都没发生之后五分钟」。

> 另：anon key 按设计可公开，因此 `GET /rest/v1/prices_daily?select=*`
> 一次就能把整张表拉走。在 Supabase 的 API 设置里**显式设定 Max rows**，
> 不要继承默认值（默认值需实施时核实）。
图表用轻量方案（自绘 SVG sparkline + 必要处用 Recharts），避免引入重型图表库拖慢首屏。

### 10.2 信息架构
**单页仪表盘**（§12 #12：不做个股详情页）。整站只有三个页面：

```
/                     仪表盘 —— 一屏看完「市场当下是什么状态」
  ├─ Header          品牌 · 数据截至 YYYY-MM-DD（美东）收盘 · 市场状态 pill · 陈旧黄条
  ├─ § 全池速览       ★ 主体：17 行可排序表格 + 每行 60 日 sparkline
  │                   列：最新价 · 20日动量 · 距EMA60 · RSI(14) · β · α(年化) · 事件
  │                   「事件」列只显示**最近的一个即将发生的**事件倒计时标签
  │                   （如「财报 3d」/「除息 12d」，是否为预告值只进 tooltip）；
  │                   四个事件距离的完整值放行内展开（§3.5）
  │                   前三名的行打一个轻量强调标记（不是另起一块）
  │                   点行内展开：α/β 明细（t 值 · R² · 残差波动 · 样本数）
  ├─ § 今日三强       一条紧凑横条（§10.3），不是三张 hero 卡片
  ├─ § α–β 分布图     散点：x=β, y=年化α, 气泡大小=R²，QQQ 锚定在 (1, 0)
  └─ Footer          方法论 · 非理想态演示 · 源码（GitHub）· 免责声明
                     （「最后更新时间」不在这里，它在 Header 的「数据截至」）

/methodology          口径说明（公式逐条列出 + §1.3 的 low-buy 差异说明 + 数据源与局限）
/login                密码页（§10.5）
```

> **布局在本轮被改过一次**：初稿把「今日三强」做成页面顶部三张大卡片。
> 按 §4 开头的定位修正 —— 本项目不是榜单项目 ——
> **全池表格才是主体**，三强降为表格下方的一条紧凑横条，
> 并在表格里用一个轻量标记把那三行标出来，而不是把同样的信息呈现两遍。
> 这样打开页面第一眼看到的是「17 只标的当下的全貌」，而不是「今天谁第一」。

> 为什么砍掉个股页：它的四块内容里，三块是**历史**（1 年价格+EMA60 叠加、
> RSI 历史带、近 30 日上榜记录），而 §1.2 已经把「历史展示」排除在本期之外。
> 剩下的 α/β 卡片（t 值、R²、样本数）直接做成表格行的展开/悬浮即可。
> 省下约 1.5 人日，且数据全在库里 —— 哪天真想要，加一个页面就行。

> **「可排序」的四条取舍**（实现时定的，写下来免得被当成随手）：
>
> 1. **排序状态住在 URL 里，排序在服务端做**（`?sort=<列>&dir=asc|desc`）。
>    换成 `useState` 就得把整张表变成 client component，17 行 × 10 列连同
>    sparkline 的 SVG 全部进 hydration，而 §10.4 的性能预算（首屏 JS < 100KB gzip）
>    已经贴着 103KB。走 URL 实测**一字节客户端 JS 都没加**（`/` 的 First Load JS
>    仍是 103 kB），还白拿三样：排序可分享、后退键能用、**关掉 JS 照样能排**。
>    代价是每次点击一次整页导航；页面本来就是 `force-dynamic`，数据走 Data Cache，
>    那一次请求**不打数据源**，§10.1 不受影响。
>
>    > **表头必须是裸 `<a>`，不能用 `next/link`。** 初版用了 Link，实测它把路由
>    > 运行时第一次拉进主包：`/` 与 `/states` 的 First Load JS **103 → 106 kB**
>    > （多出来的 chunk 8.5 KB raw / 3.4 KB gzip）。那正好把这条取舍的**前提**
>    > 掏空 —— 一边说「零客户端 JS」一边收 3.4KB。换回裸 `<a>` 即回到 103 kB。
>    > 这个数字是这条决策记录的承重论据，别只看 `next build` 的
>    > 「shared by all」那一行（它不含每条路由自己的 chunk，看上去不会变）。
>
>    > **裸 `<a>` 的真实代价不是那次请求，是新文档会把表格框的横向位置和键盘
>    > 焦点清零**（软导航时 React 复用同一个 DOM 节点，两样都保得住）。手机上
>    > 因此要多滑两下才能看到自己刚排的那一列 —— 而 §10.4 刚说过「真正糟的不是
>    > 横滚，是横滚之后失去坐标」。**这个代价是权衡之后选的，不是没想到**：
>    >
>    > 试过的解法是给每个 `<th>` 加 `id`、href 末尾接同名 `#` 锚点，靠浏览器的
>    > 锚点跳转把横向位置要回来。横向那一半确实成了（实测 360px 视口下
>    > `scrollLeft` 从 0 回到 377，目标列完整露出，且按一次 Tab 就回到刚点的表头）。
>    > **但锚点跳转是 `block: "start"`，会连文档一起往下拖**：实测 390×844 下
>    > `window.scrollY` 跳到 141，§10.6 的陈旧黄条与「数据截至」双双出视野 ——
>    > 而「排序链接可分享」正是这套方案自己的卖点，收到链接的人**第一屏**
>    > 就看不到「管道停了」。那是 §10.6 那句「不是白屏，是**看起来没事**」的形状。
>    >
>    > `scroll-margin-top` **救不了它**，这一点值得单独记：它对**嵌套滚动容器里**
>    > 的目标无效。对照实验（同一页、同一个浏览器）：页面上一个普通 `div`
>    > 加 `scroll-margin-top: 300px`，落点从 2981 变 2681（正好差 300，生效）；
>    > 而表格框里那个 `<th>`，12rem 与 3000px 落点都是 141（毫无变化），
>    > 去掉 `position: sticky` 一样是 141，加在滚动容器自己身上也是 141。
>    > 文档那一级对齐的根本不是 `<th>`，是包着它的那个滚动容器。
>    >
>    > 于是二选一：多滑两下，或者让分享出去的链接第一屏藏起「管道停了」。
>    > **按 §10.4「诚实优先于漂亮」选前者。** 两个代价的性质也不对称：
>    > 丢坐标是**点排序的人**自己付、他知道刚才发生了什么、滑回去就好；
>    > 锚点拖走文档是**收到链接的人**付、他不知道、而且页面看起来是好的 ——
>    > 那正是 §10.6 定义的那一类失败。
>    >
>    > **「那就只在没有黄条的时候加锚点」这条也不成立**：锚点是写死在 URL 里、
>    > 会被分享出去的，而黄条在不在取决于**收件人打开链接的那一刻**管道停没停，
>    > 那个时刻在生成链接时是未知的。今天分享的干净链接，明天打开就可能正好
>    > 撞上那次事故。**片段是持久的，陈旧状态不是**，所以这个危险没法被条件化
>    > 裁掉，只能整个不要。
>    >
>    > 哪天要重开这个口子，先把上面那组测量重做一遍，**别只测横向那一半**。
> 2. **缺失永远沉底，不随方向翻转。** 空位跟着方向跑的话，降序时一排「—」会顶在
>    最上面 —— 读起来就是「这几只的 RSI 最高」，而它们其实是没有值。
>    这是 §10.4「诚实优先于漂亮」在排序上的形态。同值一律按标的字母兜底，
>    于是同一个 URL 永远给同一个顺序。
> 3. **「60 日走势」按这条线的首末涨跌排**，并在表头写明（`title` + 屏幕阅读器文本）。
>    那一列画的是线、没有数字可排；按什么排必须说出来 —— 「按你看不见的量排序」
>    是一种安静的欺骗。
> 4. **「事件」列的排序键与标签上的天数同源**（`lib/nextEvent.ts`，两个消费者共用）。
>    各算各的话，排出来的顺序会和印在标签上的天数对不上，
>    而那种错**每一行单独看都是对的**。
>
> 排序指示器**不用强调色** —— 琥珀是「今日三强」这一个语义的专用色（§10.4
> 「单一强调色」），排序借它一用那个语义就被稀释了。用字形（`▲`/`▼` 对 `⇅`）
> 加亮度差，两者任一单独都够用。

### 10.3 三强横条的视觉结构（设计要点）

不是三张 hero 卡片，是表格下方的一条紧凑横条 —— 风险上下文（RSI / 距EMA60 / β）
**不在这里重复**，它们就在上方表格的同一行里，抬眼就能看到。

```
今日三强  (20日动量 · 收盘口径)                        尺子: mom_20 ▾
┌──────────────────┬──────────────────┬──────────────────┐
│ ① NVDA  +18.42%  │ ② AVGO  +15.32%  │ ③ MU    +12.20%  │
│   ↑2 · 在榜5天    │   —  · 在榜2天    │   ↑7 · 新晋      │
│   领先② +3.10pp  │   领先③ +3.12pp  │   领先④ +0.31pp  │
│   ████████░░     │   ████████░░     │   █░░░░░░░░      │
│   超中位 +9.4pp  │   超中位 +6.3pp  │   超中位 +3.2pp  │
└──────────────────┴──────────────────┴──────────────────┘
                                    ⚠ 第③名与第④名胶着（见下）
```
每格四行：**名次+主分 / 排名动能+持续性 / 领先厚度+条形 / 超池内中位**。
条形长度就是 `delta_to_next`，一眼看出这个名次稳不稳 ——
上图里第 ③ 名的条只有一格，正是需要主动标注的那种情况。
**第 3 名的 `delta_to_next` 过小时，卡片加一条 "名次胶着" 的提示条** ——
这是整个榜单里最容易误导人的一格，必须主动标注。

但阈值**不能是固定的 0.5pp**（初稿如此）。这 16 只大型科技股的 20 日动量离散度
在平静期约 2pp，在 2020 年 3 月或某个财报周可达 25pp。
把算术摆出来：平静期 16 只标的铺开 2pp，相邻间隔约 **0.125pp**，
远小于 0.5pp → **一直触发**；高离散期铺开 25pp，间隔约 **1.5pp**，
大于 0.5pp → **永不触发**。
于是它在最不需要提醒的时候最吵（平静期本来就全体密集），
而在真正该提醒时沉默（高离散期里一个相对微小的间隔仍然是真胶着）。

> 初稿这里写的是「前一种永不触发、后一种一直触发」，**方向是反的**，
> 已按上面的算术更正。这个错是 M2 写测试时被逼出来的 ——
> 测试的期望值按原文写就跑不通。

→ 用归一化阈值：`delta_to_next < k × stdev(池内得分)`（k ≈ 0.1）。
它是尺度无关的，因此语义也更合理：**同样的绝对间隔，在密集的池里是真分开了，
在铺得很开的池里只是噪声**。

### 10.4 视觉语言（专业金融看板的取舍）
- **深色优先，双主题**：`prefers-color-scheme` 自动切换，并提供手动开关。
  背景用近黑的中性灰（不用纯黑，纯黑上的细线会糊）。
- **数字排版是第一性的**：全站数字 `font-variant-numeric: tabular-nums`，
  确保列内小数点对齐。正负号常驻（`+1.2%` / `−1.2%`，用真减号 U+2212 而非连字符）。
- **方向色不用红绿裸配**：用 teal（涨）/ rose（跌）的低饱和组合，对红绿色盲更友好；
  且**永不单靠颜色传达方向** —— 始终同时有符号或箭头。
  （另注：中美股市红绿含义相反，符号优先可以规避这个歧义。）
- **单一强调色**：强调色只用于「今日三强」这一个语义（表格里那三行的轻量标记 +
  表下那条横条）。注意分寸：§4 已明确三强**不是全站主题**，
  所以这里的「强调」是一个可识别的标记，不是一块抢眼球的色块。
  其余全是中性色 + 方向色。强调色到处用 = 没有强调。
- **指标各自的最佳形态**（不是所有数字都该是数字）：
  - RSI → 0–100 分段条，30/70 带着色，指针标注当前值。
  - 距 EMA60 → 以 0 为中心的双向发散条。
  - β → 以 1.0 为中心的刻度条（QQQ 位置常驻参考线）。
  - α → 带符号数值 + 显著性；`|t| < 2` **降透明度**，`t` 的具体值放在 tooltip 里。
    （初版还加了一个 "未显著" 微标签，实现后拿掉了：调暗已经把「跨没跨过 |t|≥2」
    说清楚了，再加三个字是同一件事讲两遍，而表格里每多一块东西都在和数字抢注意力。）
  - 20 日动量 → 数值 + 60 日 sparkline。
- **诚实优先于漂亮**：`provisional`（预热不足）与 `NULL`（样本不足）
  都有明确的视觉表达，绝不用 0 或上一日的值冒充。
  站上的每个价格都是**已定稿的收盘价**（§7.1），不存在"会变的数字"这一类状态。
- **响应式（按表格优先的新布局）**：
  - **冻结表头与那个 `max-height` 的框在实现里是无条件的，不带断点**
    （`PoolTable.tsx` 的 `max-h-[calc(100dvh-7rem)] overflow-auto` + `.frozen-head` /
    `.frozen-col`），所以下面两条代价**对桌面同样成立**，不只是手机：
    (1) 表格被收进一个约一屏高的框，一屏能看到的行数变少 ——
    17 行 × 约 53px ≈ 900px，笔记本上桌面端一定会在框内滚动；
    (2) 框内滚到底之后整页继续滚时，这个框连同被钉住的表头会被推出视口上方。
  - ≥1024px：宽表为主体，三强横条在表下三列平铺；行内展开在行下方展开一条。
  - <768px：初稿写的是「表格转为卡片列表（金融表格强行横滚是最糟的移动端体验）」。
    **实现走了另一条路，这里按实际决定改写**：表格保留横滚，
    但把**纵横两个表头同时冻结** —— 横向的指标名钉在顶，纵向的标的列钉在左
    （`web/components/PoolTable.tsx` 的 `.frozen-head` / `.frozen-col`）。
    那句话只说对了一半：真正糟的不是横滚，是**横滚之后失去坐标** ——
    滚到中间就不知道眼前这个数字属于哪只标的的哪个指标。坐标钉住之后，
    横滚反而保住了卡片列表一定会丢掉的那件事：**跨标的竖着扫同一列**，
    而那正是这张表存在的理由（§10.2：一屏看到的是 17 只标的的全貌，
    不是 17 张要一屏一屏翻的卡片）。
    三强横条仍然纵向堆叠为三行。**α/β 行内展开仍未实现**：初稿把它定义成
    「卡片内的可折叠区」，而卡片已经不存在了 —— 真要做，它得是**冻结表里
    的行下展开**（与 ≥1024px 那条同一种形态），而不是卡片里的折叠区。
- **可访问性**：对比度 ≥ WCAG AA，表格用真 `<table>` + `<caption>` + `scope`，
  所有图表有文字替代摘要。
- **性能预算**：首屏 JS < 100KB gzip，LCP < 1.5s。数据 payload < 50KB（17 行 × 若干列）。

### 10.5 象征性密码保护（§12 #7 改版）

站点加一道密码。**Vercel 自带的 Password Protection 是 Pro 付费功能（$20/月），
Hobby 免费版没有** —— 免费版只有 "Vercel Authentication"，那不是密码，
而是要求访问者用**你的 Vercel team 账号**登录，你想分享给别人时对方必须被拉进 team。
所以自己实现：

- `middleware.ts` 拦截页面路由，检查一个签名 cookie。
  **matcher 必须排除 `/login`、静态资源和 `/api/*`** —— 排除 `/api/*` 不是可选项：
  §10.1 的按需重验证是 GitHub Actions POST `/api/revalidate?token=...`，
  那个请求**没有 cookie**，会拿到一个 **307 跳转到 `/login`** 而不是错误；
  于是管道报 `ok`、重验证从未发生、`REVALIDATE_TOKEN` 成为死重量，
  页面退回 `revalidate = 3600` —— 恰好是 §10.1 特意设计掉的那个延迟，而且无从得知。
  `/api/*` 自带 token 鉴权，不需要 cookie。
- 无 cookie → 重定向到 `/login`；提交正确密码 → 下发 cookie（`httpOnly`、`secure`、
  `sameSite=lax`，有效期 30 天）→ 回到原路径。
- 密码从 **Vercel 环境变量 `SITE_PASSWORD`** 读取。
  **绝不写进仓库，也不写进 `.env.example`** —— 那个文件是要提交进公开仓库的。
- cookie 存的是 `HMAC(SITE_PASSWORD, salt)` 而不是密码本身；比较用恒定时间比较。
  （这不是因为威胁模型需要，而是因为这几行成本为零，且避免写出一段将来会被抄走的坏范例。）

**诚实标注它的强度**：这是**象征性**的，不是安全边界。
仓库是公开的，密码是弱口令，middleware 只拦页面。
它买到的是两件实际的东西，而不是「安全」：

| 买到了什么 | 为什么有价值 |
|---|---|
| 搜索引擎索引不到 | §13 里 **Yahoo 条款禁止再分发**那条风险显著下降 —— 一个不可被检索的私人页面与一个公开发布的数据站不是一回事 |
| 爬虫刷不到 | §13 的「免费额度被刷爆」归零 |

**真正的安全边界仍然是 §9.3 的 GRANT + RLS**（anon key 本来就是可公开的，
密码页拦不住直接打 Supabase REST 的人）。这一点必须写进方法论页，
否则将来有人会误以为「有密码 = 数据是私密的」。

### 10.6 必须设计的非理想态
空数据（首次部署）、数据陈旧（落后满 **7 个日历日**未更新，顶部黄条）、
部分标的缺失（该行显示「数据缺失」而非空白）、
新标的预热不足（`provisional` 灰标）。
**这四种状态比 happy path 更能决定这个看板可不可信。**

陈旧阈值用**日历日**不用交易日，是实施时改的（初版写的是「>1 交易日」）：
管道在收盘后约一小时才写入，所以「今天还没有今天的数据」是每天的常态，
周末与假日更是必然落后两三天 —— 旧判据于是**每天下午到晚上都自动亮一次黄条**，
而数据完全正常。一条天天出现的告警会被训练成噪声，等真的停了也不会有人看，
恰好废掉它唯一的用途。取 7 天：美股最长的连续休市约 4 个日历日，
7 天窗口里必然含 ≥3 个 session，亮起来一定是真事；而 7 个*交易日*要等 9 天以上，
白白把 dead-man 窗口拉长一倍。值与完整理由落在 `web/lib/market.ts`
的 `STALE_AFTER_DAYS`（**不做成 config 旋钮**：它由市场结构决定而非部署偏好，
与时区同类）。黄条亮起后**两个数都印**：触发用的日历日，以及
「N 个交易日没有数据」—— 落后 3 根和落后 40 根是完全不同的两件事，
而只印一把尺子会让读者对着「满 7 个日历日才亮」的规则看见「已落后 5 个交易日」，
两边对不上而屏幕上没有任何桥。

---

## 11. 里程碑与交付

**诚实的总量：11–14 人日**（不含 §11.5 的 review 闸门耗时，而那一项很可能不小 —— 见 §11.5）。

> 两次修正：初稿的 6 人日把 M7 严重低估（→ 3.5d）；
> 之后 §12 #12 砍掉个股详情页，M7 回到 2.0d。
> 净变化是范围收窄，不是估算变松。

| # | 里程碑 | 交付物 | 验收标准 | 估时 |
|---|---|---|---|---|
| **M0** | 仓库骨架 | git init、目录结构、`pyproject.toml`、依赖锁（含哈希）、`.gitignore`、`.env.example`、`ci.yml`、LICENSE | `pytest` / `ruff` / `mypy` 在空项目上绿 | 0.5d |
| **M1** | 配置层 | 4 个 YAML + pydantic schema | 非法 config 被明确报错；universe 17 个标的解析正确；**`min_bars ≤ provisional_below` 不变式生效**；§6.2 三条 CI 校验生效 | 0.5d |
| **M2** | 指标引擎 | `pipeline/metrics/*` + 注册表 + 单元测试 | RSI/EMA 对齐手算 golden values（显式递归口径）；**QQQ 对自己 β=1、α=0、R²=1**；**`r2 == corr²`**；**合成拆股序列上 `close_vs_ema60_pct` 连续**；除零 / NaN / 样本不足 / 全 NaN 排名 均有测试；**事件距离的今天边界**（财报当天 `days_since=0` 且 `days_to` 指向下一季）有测试 | 1.5d |
| **M3** | 数据库（六张表） | **前置（脚本，用管理 token）**：创建 `pipeline_writer` 角色 + 随机密码，**必须在 0001 之前**。**本里程碑交付**：`0001_init.sql`（REVOKE/GRANT、RLS 与策略、触发器、`v_strength_enriched` 完整 SQL，**不含 create role**）+ `0001_rollback.sql` + `invariants.sql` + §9.3.3 全部集成测试 | **在空库上执行**（M4 回填之前）；anon INSERT 得 `42501`（见 §8.4）；anon SELECT **六张公开表 + 视图**全部成功；§9.3.2 的不变式返回 0 行；回滚脚本实测可用 | **1.5d** |
| **M4** | 抓取与写入 | `fetch.py`（整窗降级 + §7.2 闸门 4 的合理性断言）、**`sync_symbols.py`（config → `symbols`）**、**`sync_sessions.py`（XNAS 日历 → `trading_sessions`，带 ordinal）**、**`fetch_events.py`（财报/分红 → `symbol_events`，逐标的 17 次请求，按 §3.5(4) 的周频与 §7.3.1 的限流）**、`store.py`（T1/T2/T3 三事务 + NaN sanitizer）、`backfill.py` | 回补 400 根 bar 成功（约 126 个 NaN 前导行不炸）；重复跑 backfill 行数不变；复权因子变化能被检出；`close` 是否已拆股调整**实测确认一次**；**事件侧**：移动财报日不留孤儿行（§3.5(1) 不变式返回 0 行）、整窗 Stooq 降级跑通、`calendar['Ex-Dividend Date']` 究竟是下一次还是最近一次**实测确认一次** | **3.0d** |
| **M5** | 自动化 | `daily.yml` + 四重闸门 + 条件重试 + `concurrency` + `runs` 日志 + dead-man's switch | **冻结时钟的单元测试**覆盖 4 个 cron 时刻 × 2 个时区(EST/EDT) × 2 类交易日(全日/半日) = 16 种组合，逐一断言闸门判定；手动 dispatch 跑通 | 1.0d |
| **M6** | 前端骨架 | Next.js + 服务端读取 + 构建期直读 YAML + **显式列与 `extra` 双路读取** + **最小错误边界** + 部署 Vercel | 线上能看到真实数据的裸表格；新增一个 extra 指标无需改前端代码；**数据库不可达时显示「数据暂不可用」+ 正确 HTTP 状态码，不是白屏** | 1.0d |
| **M7** | 前端成品 | 全池表格+sparkline（主体，α/β 明细行内展开）、三强横条、α–β 散点、方法论页、**密码 middleware + /login**、双主题、**响应式：横滚 + 纵横表头冻结**（§10.4，不做卡片列表）、四种非理想态 | 移动端可用；Lighthouse ≥ 95；四种状态可手动触发演示；**403 白屏态必须真的实现**（§12 #9 第 4 条） | **2.0d** |
| **M8** | 收尾 | README（中英）、方法论定稿、监控、gitleaks 全历史、月度 heartbeat + `pg_dump` workflow | 见下 | 0.5d |

> **M5 的 DST 测试是这张表里最重要的一条验收标准。**
> §13 把夏令时列为「数据错误且不易察觉」，而你**无法靠等待来验证它**——
> 要等到 3 月或 11 月。冻结时钟的单元测试是唯一能在今天就知道冬令时那几条 cron
> 写对没有的办法。
>
> **M8 的「连续 5 个交易日无人工干预自动更新成功」不是 0.5 天的工作量** ——
> 那是 7+ 个自然日的**经过时间**。它是一条**上线后的观察窗口**，不是一个工时条目；
> 上面的 0.5d 只包含写文档与配监控。

**关键路径**：M1 → M2 → M4 → M5。M3 可与 M2 并行；M6/M7 只依赖 M3 的 schema，
可在 M4 完成前用 seed 数据先行开发。

### 11.5 每个里程碑的质量闸门（Review Protocol）

**每完成一个小环节（= 上表的一个里程碑，或里程碑内一个自成一体的子模块），
必须依次通过下面两道闸门才能推进下一环节。没通过不往前走。**

**闸门 A — Claude review agent（并行多组）**
- 派出一组 review agent（通常 2–3 个，各有分工：正确性 / 安全与配置 / 简化与一致性），
  让它们**读代码、读 diff**，独立给出发现。
- 修复所有 **serious issues**（正确性错误、安全问题、数据口径错误、会导致静默错数的坑）。
  nice-to-have 记进 `BACKLOG.md`，不阻塞。
- 修完再跑一轮，直到**没有 serious issues**。

**闸门 B — codex CLI review**
闸门 A 干净之后，调用外部 codex（终端命令，不是内部 subagent，独立性来自这里）做复核。
要点（三条都不能省）：
1. `codex exec --dangerously-bypass-approvals-and-sandbox` — 非交互模式 + 跳过沙盒审批。
2. 用管道喂 prompt，否则 `codex exec` 会一直等 stdin。
3. 先设 `$env:HTTPS_PROXY` / `$env:HTTP_PROXY` 为 `http://127.0.0.1:7890` ——
   Node.js 不读 Windows 注册表的代理设置，必须显式设环境变量。

**prompt 走文件，不要内联字符串。**
`Write-Output '<prompt>' | ...` 在 prompt 含撇号或 `$` 时会直接崩，
而 review prompt 里出现代码片段几乎是必然的：
```powershell
powershell.exe -Command "$env:HTTPS_PROXY='http://127.0.0.1:7890'; $env:HTTP_PROXY='http://127.0.0.1:7890'; Get-Content -Raw .\.review-prompt.txt | codex exec --dangerously-bypass-approvals-and-sandbox"
```
- 同样：修完所有 serious issues，重跑到干净为止。

**闸门通过的记录**：每个里程碑在 `docs/reviews/M<N>.md` 留一份
「发现 → 处置（已修 / 记入 backlog / 判定为误报及理由）」的小结，
便于事后追溯，也便于判断 review 本身是否在空转。

**两条要承认的成本**：
- 9 个里程碑 ×（2–3 个 review agent + 一轮 codex + 迭代到干净 + 写记录）
  很可能**超过实现本身的工作量**。§11 的「11–14 人日」只是实现量，不是排期。
  > codex 在第 2 轮复核时建议：对一个单人看板，可以只对
  > **schema / 权限 / 指标计算 / 自动化**这四类高风险变更保留双闸门，
  > 普通前端与文案子模块降为「一次 review + CI」。
  > **这是你的流程，我不替你改** —— 当前按你的要求对每个环节都执行；
  > 想减负的话告诉我，我改 `AGENTS.md`。
- `--dangerously-bypass-approvals-and-sandbox` 是在一台存有 OneDrive / Dropbox
  的机器上运行一个无沙盒的 agent。这是一个被明确接受的风险，写在这里以便它是**被选择的**
  而不是被默认的。

---

## 12. 需要你拍板的决策

| # | 问题 | 我的建议 | 影响 |
|---|---|---|---|
| 1 | 数据源 | yfinance 主 + Stooq 备（零密钥起步） | 若要「正经」源，改 Tiingo，你需多给一个 key |
| 2 | SPY 是否入池 | **已定：不入池**。universe = QQQ + 16 只个股 = 17 | 少一行噪音；想要时加一行 config 就回来 |
| 3 | 三强股排序尺子 | **已定**：`mom_20`（对齐 low-buy 已注册规则，改收盘口径）。**`rs_vs_qqq_20d` 已删除** | 减去一个全体共同的常数改不了任何名次 —— 它作为尺子是空操作，作为列与 `delta_to_median` 重复 |
| 4 | alpha 的无风险利率 | 默认 `rf = 0`，方法论页注明 | 留开关，未来可接 ^IRX |
| 5 | 历史回填深度 | **已定**：`lookback_bars = 400`（约 19 个月），推导见下 | 回填与日常滚动重抓用**同一个数** |
| 6 | 触发时刻 | **已定**：收盘 +60min（17:00 ET）单跑，只写最终价。`settle_minutes` 在 config 里可调 | 无两段式、无 `preliminary` 状态，数据库与前端都更简单 |
| 7 | 站点是否公开可访问 | **已定（改版）**：加一道**象征性密码**（§10.5，自己用 middleware 实现，Vercel 自带的是 Pro 付费功能）+ 页脚免责声明 | 搜索引擎索引不到 → Yahoo 条款风险与爬虫刷额度两条风险同时下降；但它**不是**安全边界，真正的边界仍是 §9.3 |
| 8 | 写库凭证 | **已定**：`pipeline_writer` 最小权限角色 + psycopg 直连（§8.1.1）。**角色由我用你给的 `sbp_` 管理 token 建好，你零操作** | 你原本反对的成本（手工建角色）已消失；保留最小爆炸半径 + 事务能力 |
| 9 | 是否建 staging 项目 | **已定：不建**，直接在生产跑迁移 | 见下方「不建 staging 的补偿措施」—— 排练场没了，就得用别的办法把风险压回去 |
| 10 | 榜单存多少名 | **已定**：全部 16 名（§4.4），前端只显示前 3 —— 但**榜单不是本项目的主题**，只是加在这批标的上的又一个变量（§4 开头、§10.2 布局已按此修正） | 约 4000 行/年；全池表格才是首页主体，三强降为一条紧凑横条 |
| 11 | alpha 年化方式 | **已定**：线性（`alpha_d × 252`），`compound` 保留为可选 | 复利式会把强势半年的 α 夸大 2–4.5 倍，极端时还会撑爆列宽 |
| 12 | 个股详情页 | **已定：不做**，只做单页仪表盘（§10.2） | M7 从 3.5d 降到 2.0d；数据都在库里，以后想加随时能加 |
| 13 | 事件距离参数 | **已定：加**财报/分红的前后距离共 4 个（§3.5），单位为**日历日** | 新增 `symbol_events` 表（第六张公开表）；抓取**不可批量**（逐标的 17 次）故改为**周频**刷新；**事件列只写最新一行、历史行全为 NULL**（§3.5(3)），于是不存在前视泄露也不需要就此写免责声明 |
| 14 | 仓库与凭证交付 | **已定**：设计定稿后在你的个人 GitHub 上新建 **public** 仓库；`sbp_` 管理 token 与 GitHub token 走 `.env`（已 gitignore），**不经聊天记录**，用完你删 | 注意：`sbp_` 只能操作 Supabase，建仓库需要另一个 GitHub token；本架构里**不存在**「把 repo 连到 Supabase」这一步 —— 连接就是 Secrets 里那条连接串 |

### §12 #5 的推导：`lookback_bars` 为什么是 400

这个数是**算出来的，不是拍的**。它同时是回填深度和日常滚动重抓窗口：

**这里有两种不同的标准，初稿把它们混在了一列里 —— 分开看才成立：**

**标准 A：展示策略**（`min_bars` / `provisional_below`，决定某一行要不要打灰）

| 指标 | 有值 | 脱离 `provisional` |
|---|---|---|
| `mom_20` | 21 | — |
| alpha/beta(126) | 127 | — |
| EMA(60) | 60 | **180** |
| RSI(14) | 15 | **250 ← A 里最紧** |

**标准 B：喂入要求**（算这一行时要给计算函数多少根，与打不打灰无关）

| 要求 | bar 数 |
|---|---|
| alpha/beta 的窗口 | 127 |
| **EMA(60) 的 SMA seed 影响衰减到可忽略（6×N）** | **360 ← B 里最紧** |
| 取整留裕量 → **`lookback_bars`** | **400** |

`lookback_bars` 由**标准 B** 决定（喂多少），`provisional` 标记由**标准 A** 决定（灰不灰）。
两者都成立且互不矛盾：`provisional_below: 180` 说的是「有 180 根历史的那一行可以不打灰」，
360 说的是「算的时候别只喂 180 根」。**400 同时满足两者。**

> **一条要记下来的更正**：在去掉个股详情页（#12）之前，下界是
> `250(RSI预热) + 图表窗口`，由图表拖着。个股页一去，仪表盘只需要
> 「今天的一列数字 + 60 日 sparkline」，图表不再是约束，
> **真正的下界回到了 EMA60 的 6×N 收敛要求**。
> 你选的半年（126 交易日）仍然生效 —— 它现在是 α/β 的窗口与全站的时间视角，
> 但不再是回填深度的决定因素。

**一个白捡的好处**：回填 400 根价格之后，

- 第 250–400 根这段（约 **150** 个交易日）的**全部指标**都非 provisional（RSI 是最后解锁的）；
- 而**历史榜单**解锁得更早 —— 排名只依赖 `mom_20`（`min_bars: 21`），
  与 RSI / EMA 的预热无关，所以第 21 根之后（约 **380** 个交易日）就都排得出来。

于是上线第一天 `days_in_top_n` 和 `rank_delta_1d` 就有意义，
而不是所有人都显示「在榜 1 天」。这不违背「不做历史研究」的定位：
它不是给人看的历史，是让今天的数字有意义的最小上下文。

### 不建 staging 的补偿措施（#9 的直接后果）

你选了直接上生产。那就必须用别的办法把「改错授权 = 线上事故」这个风险压回去
—— 下面四条是替代排练场的，不是可选项：

1. **把 `0001_init.sql` 跑在库里还没有任何数据的时候。** 这是唯一一次
   「爆炸半径天然为零」的机会，而它恰好是风险最高的那次迁移（REVOKE/GRANT/RLS 全在里面）。
   顺序固定为：建角色 → 跑 0001 → 跑 `invariants.sql` → 跑 §9.3.3 集成测试 → **然后才**回填数据。
2. **每个迁移文件包在 `begin; ... commit;` 里**，并在同目录放一个同名的
   `NNNN_rollback.sql`。没有回滚脚本的迁移不允许执行。
3. **迁移执行后立刻跑 `invariants.sql` + §9.3.3 的全套集成测试**，
   而不是等下一次 CI。生产上唯一的安全网就是「改完马上验」。

   > **但这些不变式还必须按计划定期跑，不能只挂在 `ci.yml` 上。**
   > `ci.yml` 的触发是 `push + PR`，而 §9.3.2 的不变式断言的是**线上数据库状态**，
   > 不是代码。§7.2.1 已经指出这个项目的稳态是「跑得很好、没人再推代码」——
   > 于是「有人在 SQL Editor 里关掉了 RLS」「Security Advisor 的自动修复删掉了一条授权」
   > 这类探测器，**恰好在没人盯着的时候停止运行**。
   > → `invariants.sql` + §9.3.3 同时挂到 `keepalive.yml`（它本来就按计划跑、本来就要连库）。
   > 这同时覆盖了第 4 类场景：**从 Dashboard 直接改库**——
   > 四条补偿措施原本全都是围绕「迁移文件」设计的，而 §9.3.2 自己就指出
   > SQL Editor 建表**不会**默认开 RLS，那才是更现实的那条路径。
4. **前端不可用不等于数据损坏**：授权改错最可能的表现是前端 403 白屏。
   §10.5 要求的错误态必须真的实现 —— 它在这里从「体验问题」升级成
   「事故时唯一能告诉你出了什么事的东西」。

---

## 13. 风险清单

按「会不会静默出错」排序 —— **沉默的错误排在响亮的故障前面。**

| 风险 | 影响 | 缓解 |
|---|---|---|
| **复权因子被供应商追溯改写，而管道只写当日** | **静默错数**：跨接缝的 `mom_20` 误差 = 股息率，足以换掉榜上的名字；所有闸门全绿 | §3.0 规则 2：每日重抓并覆盖 `lookback_bars = 400` 的整个回看窗口；`adj_factor` 落库并比对，变了自动回补 |
| **单日降级到 Stooq，窗口内混用复权口径** | **静默错数**，且由例行限流触发（比公司行动频繁得多） | §7.3：单日失败不降级，交给条件重试；持续不可用才降级且**整窗重抓**；窗口内 `source` 唯一性断言 |
| **yfinance `auto_adjust` 默认值变化** | `close == adj_close`，「最新价」对不上券商软件 | §3.0 规则 4：显式 `auto_adjust=False` + 「某分红股某历史行 `close != adj_close`」CI 不变式 |
| **财报改期留下孤儿预告行** | **静默错数**：供应商那边日期是对的，站上却倒数到一个已作废的日期，并「播报」一场从未发生的财报；数字过几天还会自愈 | §3.5(1)：抓取成功后**整窗删+插**（旧行在插入前已被删），抓取失败则不删；叠加孤儿行不变式 |
| **财报日是估计值却被当成事实** | 用户按一个会挪的日期判断事件风险 | §3.5(2)：`is_estimated` 落库，前端 tooltip 区分；临近（≤10 天）改为每日刷新 |
| **可选指标的失败污染核心数据** | 事件抓取失败 → `partial` → 不写 `ok` 行 → 条件重试不跳过 → 够令时四跑全跑 → 好数据被 Stooq 静默覆盖 | §3.5(4)：改记 `ok_events_stale`（exit 0、不告警）；§7.1 的跳过判定同时接受两个状态 |
| **供应商坏 tick**（0.01 收盘 / 陈旧重复） | 一个坏值污染 20–126 个交易日的统计，上游修正后仍在 | §7.2 闸门 4 的 O(1) 合理性断言 + 跨源一致性比对 |
| **管道「不复存在」而无人知晓**（60 天无活动停用 cron / 密钥吊销 / 项目暂停） | **沉默 = 成功**，在初稿的告警设计里不可区分 | §7.2.1：月度 heartbeat 提交 + dead-man's switch（没收到 ping 才告警）+ 前端黄条 |
| 夏令时导致某一跑写入未定稿的收盘价 | **数据错误且不易察觉** | 触发取收盘 +60min + 时钟闸门；**M5 的冻结时钟测试**（唯一能不等半年就验证冬令时的办法） |
| **anon 仍持有隐式写授权，RLS 是唯一一道** | **数据可被任意写入** | §9.3：显式 `revoke` + 只授 SELECT（**六张表**），RLS 降为第二道；§9.3.2 的「所有表必须开 RLS」不变式进 CI；§9.3.3 集成测试 |
| **写库凭证被供应链投毒窃取** | `service_role` 下等于全库沦陷，且 RLS 按设计无效 | §8.1.1：改用最小权限的 `pipeline_writer`；依赖锁哈希 + Dependabot；secret 只挂写库那一步；§8.5 轮换流程 |
| NaN 进入写入路径 | 首次 backfill 直接失败，或存进去后污染排序与 JSON | §9.1.3：写入边界统一 sanitizer + 单元测试 |
| 单一坏格子中止整批写入（`numeric` 溢出） | 一只标的毁掉全天数据 | §9.1：无界量去掉定精度；alpha 年化改线性（§3.4）从源头压掉量级 |
| yfinance 被限流或改版 | 数据断更 | **§7.3.1 的抓取礼仪是第一道**（批量接口、串行、间隔、尊重 429、请求预算）—— 不被限流好过被限流后重试；叠加条件重试 + `stale_vendor` 告警 + 前端黄条；provider 抽象层可快速换源 |
| GitHub cron 延迟导致两跑重叠 | 交错写入 / 死锁 / `runs` 重复 | §7.1 的 `concurrency` 组 + 条件重试 |
| **Yahoo 条款禁止再分发** | 法务/合规暴露（概率低但真实） | §10.5 的密码页使站点不可被检索，风险显著下降；方法论页注明来源与免责；若将来去掉密码或产生实质流量，按 §12 #1 换到许可允许再分发的源 |
| Supabase free 项目被暂停 | 全站不可用 | keepalive（注意 §8.6 的周末覆盖缺口）；前端明确的错误态而非白屏 |
| 指标口径与行情软件对不上 | 信任崩塌 | 方法论页逐条列公式；Wilder RSI golden-value 测试；§1.3 的 t ↔ t+1 可证伪命题 |
| 免费额度被刷爆（Vercel ISR 比 Supabase 更先触顶） | 超额 | §10.1：`revalidate = 3600` + 按需重验证；PostgREST 显式 Max rows；§10.5 的密码页让爬虫进不来 |
| **迁移直接改生产（已选，无 staging）** | 授权改错 = 前端 403 白屏 | §12 #9 的四条补偿：0001 在空库时跑、每个迁移配 rollback 脚本、改完立刻跑 invariants + 集成测试、错误态必须真的实现 |
| 数据丢失 | 影响低（绝大部分可从供应商重推） | 月度 `pg_dump`；但这个论证要写出来，不能默认 |

---

## 14. 目录结构（最终形态）

```
markme/
├─ README.md                   ← 项目说明 + **计划索引**（索引在根目录，不在 docs/）
├─ AGENTS.md                   ← 仓库约定（CLAUDE.md 只有一行指向它）
├─ BACKLOG.md                  ← review 闸门产出的 nice-to-have
├─ skills/                     ← 可复用的 agent skill，与具体 agent 无关
├─ docs/                       ← 所有计划文档都在这里，文件名具体化，**不放 README**
│  ├─ create-project.md        ← 本文件
│  └─ reviews/plan-v1.md ...   ← §11.5 的闸门记录
├─ LICENSE                     ← public repo 必备，初稿漏了
├─ config/
│  └─ universe.yaml  metrics.yaml  strength.yaml  app.yaml
│                              （前端构建期用 js-yaml 直读，无中间产物）
├─ pipeline/                    Python 3.13（依赖锁定到哈希）
│  ├─ config.py                 pydantic 加载 + 校验
│  ├─ calendar_gate.py          交易日 / 结算时刻闸门（XNAS）
│  ├─ fetch.py                  provider 抽象 + 整窗降级 + 合理性断言
│  ├─ sync_symbols.py           config → symbols 表（软删除）
│  ├─ sync_sessions.py          XNAS 日历 → trading_sessions（ordinal / 半日市）
│  ├─ fetch_events.py           财报 / 分红 → symbol_events（不可批量，周频刷新）
│  ├─ metrics/
│  │  ├─ registry.py  rsi.py  ema.py  alpha_beta.py  momentum.py  strength.py
│  ├─ store.py                  psycopg 单事务写入 + NaN sanitizer + runs 日志
│  ├─ run_daily.py  backfill.py
│  └─ tests/                    golden values / 冻结时钟 DST 测试 / anon 越权测试
├─ supabase/
│  ├─ migrations/0001_init.sql  （只含 revoke/grant、RLS、触发器；**不含 create role**）
│  └─ invariants.sql            所有必须返回 0 行 / 必须成立的断言（≥ 9 条）：
│                              §9.3.2 两条、§9.1.4 的 strength⊆sessions、
│                              §9.3.3 五条内容级、§3.5(1) 孤儿行、§3.5(5) 新鲜度
├─ web/                         Next.js（Vercel Root Directory 指向这里）
│  ├─ middleware.ts             密码保护（§10.5）
│  ├─ app/(page.tsx, methodology/page.tsx, login/page.tsx)
│  ├─ components/  lib/supabase.ts  lib/format.ts
│  └─ ...
├─ .github/workflows/          daily.yml  backfill.yml  ci.yml
│                              keepalive.yml  heartbeat.yml   （共五个，见 §7.4）
├─ .env.example
└─ pyproject.toml
```

---

## 15. 下一步

确认 §12 的剩余决策（尤其 #1 数据源、#5 回填深度），
并按 §8.2 建好 Supabase 项目、把两条 Secret 填进 GitHub。
之后从 **M0 → M1 → M2** 开始，每个里程碑都走 §11.5 的两道 review 闸门。
指标引擎和它的 golden-value 测试是整个项目的地基，先把它做正确，剩下的都是管道和展示。
