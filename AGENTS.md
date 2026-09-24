# markme (market metrics) — 仓库约定

本文件是**所有** coding agent 的契约（不限定哪一家）。`CLAUDE.md` 只有一行指向这里。

## 目录约定

| 位置 | 放什么 |
|---|---|
| 根目录 | `README.md`（项目说明 + 计划索引）、`AGENTS.md`、`LICENSE`、`BACKLOG.md` |
| `docs/` | 所有计划 / 设计文档（**不放 README**） |
| `docs/reviews/` | 每个里程碑的 review 闸门记录 `M<N>.md` |
| `skills/` | 可复用的 agent skill（与具体 agent 无关，不放 `.claude/`） |
| `config/` | 标的池与指标参数，见 `docs/create-project.md` §6 |

## 计划文档

- 所有计划 / 设计文档放在 **`docs/`**，不放仓库根目录。
- **每份计划必须有一个说明其内容的具体文件名**，禁止 `workplan.md` / `plan.md`
  这类通用名。小写 kebab-case，例如 `create-project.md`、`add-sector-metrics.md`。
- 新增计划后，在**根目录 `README.md`** 的「计划文档清单」表里登记一行。
  索引留在根 README，`docs/` 下不放 README。
- 当前的总体建设计划：[`docs/create-project.md`](docs/create-project.md)。

## Skills

- 写 skill 一律放在仓库根目录的 **`skills/<skill-name>/`**，用通用的
  `SKILL.md` + 前置元数据格式，**不要**放进 `.claude/skills/` 或任何
  绑定某一家 agent 的目录 —— 未来不一定是同一个 agent 来执行。
- skill 内部不要硬编码某个 agent 专有的工具名或路径。

## 每个里程碑的 review 闸门（强制）

完成任一小环节后，必须依次通过两道闸门才能推进下一环节：

1. **闸门 A — review agent（并行多组）**：派 2–3 个 review agent 读代码 / 读 diff
   并行审查（正确性 / 安全与配置 / 简化与一致性）。修完所有 **serious issues**，
   重跑到干净为止；nice-to-have 记进 `BACKLOG.md`，不阻塞。
2. **闸门 B — codex CLI**（外部终端命令，不是内部 subagent，保证独立性）：
   它是**另一个进程、另一个模型**，独立性全部来自这一点，所以不能用内部
   subagent 代替。同样修完所有 serious issues，**重跑到干净为止**。

   **具体怎么跑写在 [`skills/review-gate/SKILL.md`](skills/review-gate/SKILL.md)，
   这里不留第二份。** 那边记着沙盒模式该先试哪个、prompt 为什么必须走文件
   而不是内联、代理为什么要显式设、以及 prompt 里**不能**写它在流程里的位置。
   这些都是实测出来的、会随环境变的东西，抄两份必然漂移
   —— 本文件此前那份就已经和 skill 说的相反了。

每个里程碑在 `docs/reviews/M<N>.md` 留一份「发现 → 处置（已修 / 记入 backlog /
判定为误报及理由）」的小结。

> 实战经验（v1→v3 的 10 轮）：**闸门 B 抓到的 serious，多数是闸门 A 的修复
> 自己引入的回归。** 所以「修完就走」不行，必须重跑到干净为止。

## Git 工作流

**闸门守的是 `main`，不是里程碑分支。** 所以顺序是这样的：

**闸门 A 干净之后**，在里程碑分支上 `commit`（说清楚做了什么、为什么）并
`push` —— 闸门 B 是外部进程，读的是**磁盘上**的状态，不先推它就审不到一棵
稳定的树（理由见 skill 的「跑之前」）。这一步**只动里程碑分支，不碰 `main`**；
闸门 B 若报了问题，就在同一分支上继续修、继续推 —— **两道都重跑到干净**：
闸门 B 的修复同样会引入回归，而且是**闸门 A 才抓得到**的那种：M11 里
闸门 B 的 H1/H3 修完之后，补跑的闸门 A 第 8 轮又报出 2 条，两条都是那次修复
自己引入的。所以不是只把闸门 B 再跑一遍就算数。
分支上的 commit 随时能被后续修复叠掉，合进 `main` 的不能。

**两道闸门都干净之后**才做下面这三步，顺序固定：

1. 开 `PR`（描述里带本轮 review 结论与 `docs/reviews/M<N>.md` 链接）
2. **`rebase merge`** —— 不是 merge commit，不是 squash。**`git log` 必须保持线性。**
3. 删掉已合并的分支

- 分支命名对应 §11 的里程碑：`m0-scaffold`、`m1-config`、`m2-metrics` …
- **不直接推 `main`**，所有改动走分支 + PR。
- 冲突用 `rebase` 解决，不用 `merge` —— 线性是硬要求。

## 独立性

本项目与 `../low-buy` **完全独立**：不 import、不共享数据目录、不依赖其运行环境。
唯一共享的是标的清单这一份事实，且已硬拷贝进 `config/universe.yaml`。
