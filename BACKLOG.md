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

## M6 / M7（前端）之前

- **`.gitattributes` 的二进制类型表**已预置常见前端资源（woff2/ico/webp），
  新增其他类型时记得补，否则 `* text=auto eol=lf` 会去归一化二进制文件。

## 随时

- **Action 的 SHA 钉版本会腐烂。** `actions/checkout` v4.2.2（2024-10）与
  `setup-uv` v5.3.1（2025-02）都已偏旧，且跑在 node20 runtime 上。
  已加 `.github/dependabot.yml` 的 `github-actions` 生态来产生更新信号。
- **`requires-python = ">=3.13"`。** 设计文档 §14 写的是 Python 3.12。
  已收紧到 3.13 与本机、CI、`uv.lock` 的解析标记一致 ——
  声明一个从来没被测过的 3.12 支持，是一句没人验证的承诺。
