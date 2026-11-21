<!-- i18n: source=docs/getting-started/overview.md blob=6fbe75840c341cccfe02d72afa8d5bbb4dfb7809 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md), which is authoritative.

<a id="overview"></a>

# 概览

Loom 是一个服务器，为 AI agent 存储记忆、文档、对话、实体图谱和代码索引，所有数据以行的形式保存在 ClickHouse 中，并可选用 [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) 保存实时记忆和知识状态。agent 和用户可以通过 MCP、HTTP、Python 和 TypeScript SDK 以及控制台访问它。Loom 采用 Apache-2.0 许可。本页帮助你判断它是否适合你的场景。

<a id="what-it-does"></a>

## 功能介绍

- **持久的记忆。** agent 保存的事实会出现在之后的会话和其他工具中，每个键对应一条声明，并带有历史、来源和有效期。它可以是个人的记忆，也可以由多个 agent 和多人共享。
- **写入时检查。** 近似重复的内容会被合并，矛盾会被标记，针对已固定事实的写入会被拒绝。
- **会弃权的搜索。** 当没有结果通过针对你的 embedder 校准的阈值时，`memory.search` 会返回 `abstain=true` 和一个原因码。
- **审计与擦除。** 每次改变记忆的写入都会留下一条带内容哈希的审计行。`erasure.request` 以对读取路径所涉及的每张表进行的残留普查作为结束。
- **带历史的代码索引。** 为你的仓库提供搜索、符号、调用方、影响分析、带评审评论的 PR 历史以及 bisect 候选项，每个答案都会注明其背后的提交和索引层级。连接跟踪器数据源后，还支持问题跟踪器：重复检查、issue↔PR 关联以及不稳定测试。

记忆和代码索引是同一台服务器上相互独立的两个平面。可以只用其中一个，也可以两个都用。记忆需要一台服务器和一个写入令牌。代码索引还需要添加一个仓库（`LOOM_CODE_REPOS_JSON`，或者在运行[仓库任务沙箱](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)的控制台中使用 **Repositories → Add repository**），并设置 `LOOM_CODE_INGEST_ENABLED=true`。[Claude Code 和 Codex 插件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)同时使用两者。

<a id="where-namespaces-fit"></a>

## 命名空间的定位

- **org** 是租户。
- **命名空间**是 org 内共享和访问控制的单位。一个部署通常包含：每个团队或项目一个命名空间，每个必须保持隔离的机器人一个命名空间，每个人一个 `personal-<slug>`（首次使用时创建，也是该用户 MCP 客户端的默认命名空间），以及每个已建立索引的分支一个 `code-<repo>[-<branch>]`。
- 命名空间的每个成员都可以读取其中的每一行。
- `agent` 记录是谁写入了某一行，它不是边界。
- `user_id` / `read_scope=own_user` 用于在同一个机器人的命名空间内区分不同的最终用户。
- 访问权限是命名空间上的角色（viewer/writer/admin），由令牌携带，或通过授权添加。

[身份与命名空间](../concepts/identities-and-namespaces.md)中有完整的模型说明。

<a id="when-loom-fits"></a>

## Loom 适用场景

- 你使用多个 AI 工具，希望它们共享你告诉过其中任何一个的内容，并保存在个人命名空间中。
- 多个工具和人员（Claude Code、Codex、Cursor、客服机器人、在控制台操作的人）需要使用同一份记忆。
- 你希望用 SQL 查询 agent 所掌握的信息：事实、trace、成本和审计记录都是 ClickHouse 表。
- 你已经在运行 ClickHouse，或者可以运行。它是 Loom 唯一必需的存储；[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) 可选，用于存放实时记忆和知识状态。
- 弃答或标出冲突，好过给出自信但错误的回答。
- 你需要历史记录、来源追溯、审计或可验证的删除。
- 编码 agent 需要你的代码仓库的索引。

<a id="when-it-does-not-fit"></a>

## 不适用的场景

| 场景                                         | 原因                                   | 可考虑的替代方案                     |
| -------------------------------------------- | -------------------------------------- | ------------------------------------ |
| 单个 agent、单个进程、短期记忆               | 没有共享需求时，服务器和数据库只是额外开销 | 进程内的记忆库                       |
| 热点路径上的亚毫秒级键查找                   | 每次读取都是一次数据库查询             | 在自有存储前加一层缓存               |
| 全图分析或长路径查询                         | 遍历时每一跳是一次有界查询，最多四跳   | 图存储，数据由 `loom entity-export` 导入 |
| 需要 agent 框架                              | Loom 不运行 agent 循环、提示词或模型   | agent 框架，以 Loom 作为其记忆       |
| 开箱即用的合规保证                           | 擦除和审计只是工程层面的基础能力       | 您自己的合规流程，并使用这些基础能力 |
| 在没有备份计划的单节点 ClickHouse OSS 上运行生产环境 | 没有复制或时间点恢复                   | ClickHouse Cloud                     |
| 固定不变的 API                               | 操作和字段在不同版本之间会变化         | 固定某个版本，并检查 `ops.capabilities` |

有效期只涉及有效时间（`valid_at`、`valid_to`）；Loom 记录了什么以及何时记录，都保存在版本历史中，而不是另设第二条时间轴。部分功能需要运维人员手动开启：代码索引、跟踪器来源、OAuth、遥测导出，以及 Langfuse、ClickStack 中的擦除。

<a id="what-each-property-costs"></a>

## 每项特性的代价

| 特性                              | 机制                                                                                                    | 代价                                                                                                                          |
| --------------------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 单一引擎（ClickHouse 为权威数据源） | 向量、词法、图、过滤、审计和擦除都是针对同一批行的 SQL                                                  | 每次读取都是一次查询。读取方需要为带版本的行承担 `FINAL` 的开销。派生行（实体、摘要）会稍后通过 worker 写入。                  |
| 弃答而不是给出弱匹配              | 在启动时根据嵌入模型自身的余弦相似度值推导出阈值（`LOOM_ADAPTIVE_THRESHOLDS`）                          | 一些较弱但正确的匹配会被拒绝；可以按请求设置 `min_similarity` 或 `floor_mode`。                                               |
| 会变化的事实                      | 每次写入时给出合并、重复、冲突和固定（pinned）的判定；仅追加的 `loom.memory_versions`                   | 每次写入都要与相邻记忆比较。一条改写的内容可能会在审核队列中等待，直到有人处理。                                              |
| 可解释的排序                      | 带 `explain=true` 的 `memory.search` 会逐项列出每个命中的得分；命中结果带有 `source_class` 和 `verified` | 当 `top_k` 超过 15 时，`explain` 默认关闭，`explain=true` 会使每个命中更大。                                                  |
| 带访问控制的共享                  | 每一行都带有身份元组；令牌和授权携带命名空间角色                                                        | 你需要签发和轮换令牌并授予角色。跨主体泄漏测试套件在 CI 中为每个读取 op 把关。                                                |
| 可核查的擦除                      | 可恢复的 saga，以残留普查结束；外部后端采用失败即关闭的策略                                             | 擦除是一个多步骤任务。无法确认删除的后端会使 saga 处于 `failed` 状态，需要人工升级处理。                                      |
| 带新鲜度的代码答案                | 解析器层级和编译器（SCIP）层级；答案带有 `as_of.commit` 和过期标记                                      | 大型仓库的索引需要数小时。编译器层级需要工具链。                                                                              |

质量方面的结论需要你自己衡量。仓库附带了基准测试工具，以及已发布数字背后的原始结果（[结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)）。在依赖某个默认值之前，请在你的部署上使用真实的提供方运行这些检查（[运行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)）。

<a id="decision-checklist"></a>

## 决策清单

如果右侧一列中有两个或更多答案符合，Loom 很可能不是合适的工具。

| 问题                                                   | 倾向 Loom | 倾向其他方案        |
| ------------------------------------------------------ | --------- | ------------------- |
| 是否有两个或更多工具或 agent 需要共享记忆？            | 是        | 否，只有一个进程    |
| 你能否运行 ClickHouse 并运维一项服务？                 | 是        | 否，你需要托管服务  |
| 数据是否必须保留在你自己的基础设施内？                 | 是        | 无特别要求          |
| 与其给出自信但错误的答案，是否更希望弃答或标记冲突？   | 是        | 始终要返回结果      |
| 你是否需要历史记录、来源追溯、审计或可验证的删除？     | 是        | 否                  |
| 你的热点路径是否是亚毫秒级的键查找？                   | 否        | 是                  |
| 你是否需要整图分析，而不仅是几跳查询？                 | 否        | 是                  |
| 你能否接受版本之间的 API 变更，或者固定在某一版本？    | 是        | 否                  |
| 你是否愿意为真实的 embedder 和 LLM 付费，并在自己的数据上做基准测试？ | 是        | 否                  |

<a id="prerequisites"></a>

## 前提条件

| 你需要                                            | 用途                                                                                                                                                                                                                                                           |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse（评估用 OSS，生产用 Cloud）            | 所有功能。参见 [ClickHouse 要求](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#clickhouse-requirements)。                                                                                                                                                                |
| Postgres 16+（可选）                              | 在 [Postgres 记忆权威](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)下保存实时记忆和知识状态，在部署首次启动时选择；除非设置 `LOOM_PG_DURABILITY=local`，否则需要同步备库（[要求](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#requirements)）。 |
| Docker 与 Compose 2.20 或更高版本                 | 已发布的本地栈（一行命令安装程序；在 Windows 上，[从 WSL 2 运行](quickstart.md#fast-path-released-image-no-clone)）和服务器镜像。                                                                                                                   |
| Python 3.12+                                      | 仅供贡献者使用的 `loom up`、服务器开发和运维 CLI 工作。                                                                                                                                                                                          |
| 嵌入提供方                                        | 真实检索：OpenAI、Gemini，或已发布栈使用的本地 CPU 嵌入器。                                                                                                                                                                             |
| LLM 提供方                                        | 提取、`memory.ask` 综合、`code.ask`：OpenAI 或 Anthropic。                                                                                                                                                                                           |
| 令牌和命名空间                                    | 访问权限。参见[令牌和授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)。                                                                                                                                                                                               |
| 代码索引                                          | 对仓库的 Git 访问权限、用于 PR 历史的 `LOOM_CODE_GITHUB_TOKEN`，以及首次索引所需的时间。                                                                                                                                                         |

<a id="next"></a>

## 下一步

[评估 Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)区分了产品冒烟测试和真正的质量评估。[快速入门](quickstart.md)会运行本地栈。[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)说明如何为团队运行 Loom。[文档地图](https://github.com/ClickHouse/loom/blob/main/docs/index.md)按阅读顺序列出了每一个页面。
