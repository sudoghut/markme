# BACKLOG

Review 闸门产出的 nice-to-have：**不阻塞当前里程碑，但也不该被忘掉**。
条目按「哪个里程碑会撞上它」归类，而不是按提出时间。

---

## M2（指标引擎）之前

- **`pytest-cov` 尚未接入。** M0 把它从依赖里去掉了 —— 声明了却没人用比没有更糟。
  M2 开始写真正的测试时再加回来，并同时配 `[tool.coverage]` 与 `addopts` 里的 `--cov`。

## M4（抓取与写入）之前 —— 优先级最高的一组

- **yfinance 1.7.0 的五项实测核对。** 设计文档（§3.0 / §3.5 / §7.3）是对着
  0.2.x 的 API 写的，而 `uv.lock` 解析到了 **1.7.0**。已实测确认三项：
  | 项 | 状态 |
  |---|---|
  | `auto_adjust` 默认值为 `True`（§3.0 规则 4 的警告成立） | ✅ 已确认 |
  | `auto_adjust=False` 时仍返回独立的 `Adj Close` 列 | ✅ 已确认 |
  | 分红股 `close != adj_close`（§3.0 规则 4 的 CI 不变式可满足） | ✅ AAPL 13/13 行 |
  | 原始 `Close` 已做拆股调整（NVDA 2024-06-10 的 10:1 无断崖） | ✅ 已确认 |
  | `Ticker.calendar` 的返回类型与 `Ex-Dividend Date` 键名，且它是**下一次**而非最近一次 | ⬜ 待测 |
  | `Ticker.earnings_dates` 仍是 property，且覆盖前后各约 4 季 | ⬜ 待测 |
  | `Ticker.dividends` 仍返回全历史，金额仍是拆股调整后 | ⬜ 待测 |
  | 多标的 `download()` 的 MultiIndex 形状；`threads=False` 满足 §7.3.1 的「串行不并发」；如何设诚实的 User-Agent（1.7.0 走 `curl_cffi` 而非 `requests`） | ⬜ 待测 |

- **`filterwarnings = ["error"]` 的定向放行。** pandas 3.x 与 yfinance 都话多。
  上游的 `DeprecationWarning` 会在 collection 阶段就让整套测试变红，
  而失败信息会指向我们的测试而不是那次依赖升级。
  → 届时**加定向条目**（如 `"default::DeprecationWarning:yfinance.*"`），
  **不要**把全局放松成非 error。

- **Stooq 降级路径的 HTTP 依赖。** `requests` 目前只是 yfinance 的传递依赖。
  若 `fetch.py` 直接 import 它，就要提升为直接依赖；
  或者用 `pandas.read_csv(url)`，那样不需要新增任何依赖。

## M3（数据库）之前 —— ~~已还~~

- ~~**删掉 `test_config.py` 里的 `REAL_METRICS_COLUMNS` 常量。**~~
  **M3 已还。** `pipeline/schema.py` 现在真的解析 `0001_init.sql`，那份手抄常量
  已删除。顺带：解析器本身改成词法感知的（`pipeline/sqltext.py`），
  因为一个**答错而不是报错**的解析器会让 §6.2 的双向校验永远通过。

## M6 / M7（前端）之前

- **`dim_when` 需要一个 TypeScript 孪生实现。** §6.1 说前端用 js-yaml 直接读 YAML，
  所以 `DimExpr.evaluate` 的语义要在前端重写一遍。**两份实现就是两个要对齐的地方**，
  所以务必对齐这几条而不只是比较运算：
  - Kleene 三值 `and` / `or`（`False and NULL` → False，`NULL and True` → NULL）
  - `null` / `NaN` / **`Infinity`** / 非数值 一律 → 打灰（fail closed）
  - 求值期绝不抛异常
  - 根节点必须是比较或布尔运算


- **`.gitattributes` 的二进制类型表**已预置常见前端资源（woff2/ico/webp），
  新增其他类型时记得补，否则 `* text=auto eol=lf` 会去归一化二进制文件。

### 前端失败路径的残留取舍（闸门 A 收尾一轮之后）

- **页面在有缓存时返回 200，这是有意的，不是待办。** 实测页面级 ISR 和
  `fetch` 的 Data Cache **都是** stale-while-revalidate，抛出去的异常被吞成
  一行日志。结论是：只要页面还有一份能用的缓存，它就不该返回 500 ——
  缓存里的数字是真的，`数据截至 <date>` 也是真的。
  状态码归 `/api/health`（不带缓存，实测源死后稳定 503），已接进
  `keepalive.yml`。**冷缓存**那条路页面仍然抛 500。

- **冷缓存那次 500 的正文是客户端渲染的。** 响应体是 Next 的
  `__next_error__` 文档，preload 了 `app/error-*.js` 并带 digest，
  浏览器水合后才渲染出「数据暂不可用」——`curl` 或关掉 JS 会拿到一个空的 500。
  要连正文一起服务端渲染，得把 `/` 改成 Route Handler 自己吐 HTML，
  那会丢掉 RSC。不值得。

- **页面丢掉了整页的 CDN 边缘缓存**（`force-dynamic`），换来「陈旧黄条永远
  说真话」。出站流量仍接近零（实测连续 5 次请求只打 6 次数据源）。
  真要把边缘缓存拿回来，得等一个能「再生失败时让缓存条目过期」的钩子，
  Next 目前没有。

- **Lighthouse ≥ 95 尚未实测**（§11 M7 验收）。预览部署挂在 Vercel 的
  SSO 保护后面，跑不了外部审计；要在**生产** URL 上跑一次。

- **`HEALTHCHECK_URL` 未配置**（§7.2.1 的 dead-man's switch）。
  它需要一个 healthchecks.io 之类的外部账号，得由人去开。
  没配时 `daily.yml` 静默跳过那一步 —— 也就是说「管道整个不复存在」
  这一类故障目前**没有探测器**，只有 `keepalive.yml` 每天那次站点健康检查。

- **为受 Vercel Deployment Protection 保护的 production 配置健康检查 bypass。**
  `keepalive.yml` 已在 GitHub secret `VERCEL_AUTOMATION_BYPASS_SECRET` 存在时发送
  `x-vercel-protection-bypass`；需由项目管理员在 Vercel 创建一个仅供 CI 使用的
  Protection Bypass for Automation secret，并同步到 GitHub。否则若 production 开启
  Vercel Authentication / Password Protection，健康检查会在到达 `/api/health` 前被拦住。

## 随时

- **Action 的 SHA 钉版本会腐烂。** `actions/checkout` v4.2.2（2024-10）与
  `setup-uv` v5.3.1（2025-02）都已偏旧，且跑在 node20 runtime 上。
  已加 `.github/dependabot.yml` 的 `github-actions` 生态来产生更新信号。
- **`requires-python = ">=3.13"`。** 设计文档 §14 写的是 Python 3.12。
  已收紧到 3.13 与本机、CI、`uv.lock` 的解析标记一致 ——
  声明一个从来没被测过的 3.12 支持，是一句没人验证的承诺。
- **`sharp` 的 libvips/libheif 漏洞**（`npm audit` 高危）。它是 Next 的
  可选依赖，本站**不用 `next/image`**，所以不在实际路径上。
  Next 自己钉的版本，只能等上游升。
- **Next 15.x 仍带着有漏洞的 `postcss`**（`npm audit` 说要升到 16.x 才干净）。
  那是构建期依赖，输入是我们自己仓库里的 CSS。升 Next 16 是一次单独的动作，
  不塞进这个 PR。

## M6（前端）开始时第一件事 —— ~~已还~~

- ~~**把 Vercel 的键位加回 `.env.example`**~~ **M6 已还。**
  `VERCEL_TOKEN` / `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID` 与
  `REVALIDATE_TOKEN` 都已加回，值一律写成占位符 ——
  真值从本地 `.env` 读。
