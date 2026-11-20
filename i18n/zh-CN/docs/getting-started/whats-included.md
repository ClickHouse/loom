<!-- i18n: source=docs/getting-started/whats-included.md blob=62311ea2165c49b6c91511404f1308be7524c42a model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/whats-included.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/whats-included.md), which is authoritative.

<a id="whats-included"></a>

# 包含的功能

本页列出 Loom 的主要功能（基于 Apache-2.0 仓库自托管）：每个领域一个小节，每一行都链接到介绍它的页面。[文档索引](https://github.com/ClickHouse/loom/blob/main/docs/index.md)列出了其余文档。部分功能需要手动开启，相应链接页面会说明开启方法；默认加载为 false 的布尔开关列在[状态](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)中。embedder 由你选择：内置的本地模型、托管的提供方，或你自己运行的 OpenAI 兼容端点。提取、摘要和带引用的回答需要真实的 LLM（参见[模型与 API 密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)）。

<a id="memory"></a>

## 记忆

| 能力                        | 作用                                                                                                                                   | 页面                                                                             |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| 共享长期记忆                | 命名空间中每个键对应一条声明，写入前会与已有事实进行比对判断                                                                           | [写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)                                  |
| 类型化记录                  | 带有精确字段的行，用于具有身份标识的数据，无需解析文本即可回答谓词类问题                                                               | [类型化记录](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#typed-records)                     |
| 搜索与提问                  | 返回排序后的命中结果，或给出引用记忆键的回答；当记忆无法支持时，两者都会弃权                                                           | [搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)                           |
| 冲突与拒绝                  | 相近但不同的值会被标记，由命名空间所有者决定采用哪个事实；已注册的模式会拒绝匹配的写入                                                 | [冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)             |
| 历史与来源                  | 每次写入都会生成新版本，并记录写入者以及进入记忆的方式                                                                                 | [历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)           |
| 从转录文本中提取            | 由服务器判断转录文本中哪些内容是持久的                                                                                                 | [提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)                                   |
| 导入                        | 其他记忆系统已提炼好的事实，会经过与任何写入相同的检查后写入                                                                           | [导入](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#importing-from-another-memory-system)  |
| 变更 webhook                | 当记忆被写入、修订、删除、恢复、编辑脱敏或擦除时，向命名空间管理员注册的 URL 发送带签名的事件                                          | [变更 webhook](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#change-webhooks)                 |
| 工作记忆与配置文件          | agent 每次都会获得的小型命名块和配置文件文本                                                                                           | [工作记忆与配置文件](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md) |
| 流程与经验                  | 从上报的运行、反思以及候选事实的审核队列中学到的步骤                                                                                   | [流程与经验](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)       |
| 与代码关联的记忆            | 关于代码的记忆会随其所指定义的移动或变化而跟随                                                                                         | [与代码关联的记忆](../concepts/memory.md#memories-linked-to-code)         |
| 实体图                      | 记忆行旁边的实体及其关系                                                                                                               | [实体图](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)                                      |

<a id="documents-code-and-work-records"></a>

## 文档、代码与工作记录

| 功能 | 作用 | 页面 |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 文档搜索 | 文档被拆分为多个章节，经过嵌入后按段落进行搜索和引用；未找到结果的搜索会被归入知识缺口 | [知识文档](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md) · [知识缺口](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#knowledge-gaps) |
| 文件与文档来源 | PDF、DOCX、PPTX、XLSX、CSV 等格式由提取器服务解析；已注册的来源会同步到命名空间中 | [导入文件](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files) · [文档来源](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources) |
| 统一搜索 | 一次调用即可搜索记忆、配置档案、文档和历史对话，并为任务生成上下文包 | [统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) |
| 询问 AI | 控制台中的助手，回答命名空间中包含的内容，每个论断都附有引用；其模型回答运行在你部署于 Loom 旁边的聊天服务器上 | [控制台](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md#ask-ai) · [聊天服务器设置](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/librechat.md#the-deployed-assistant) |
| 代码索引 | 针对你的 git 仓库提供定义、调用方、影响范围以及按语义搜索 | [代码索引](../features/code-index/overview.md) |
| PR 与发布历史 | 将已合并的拉取请求与当前的符号、调用方和测试进行关联 | [历史、PR 与发布](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) |
| 跟踪器 | 从你自己的 ClickHouse 实时读取 issue、拉取请求、事件和 CI 运行 | [跟踪器平面](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) |
| 对话与会话 | 可搜索的逐字记录，在管理员为命名空间开启日志记录后才会保存，另有供 agent 恢复工作的会话简报 | [对话与会话](../features/conversations-and-sessions.md) · [记录](../features/conversations-and-sessions.md#recording-is-the-owners-choice) |
| 运行 | 每次任务执行对应一条记录，并附带其所依赖的记忆 | [运行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) |
| 任务、决策与策略 | 任务收件箱、决策记录以及用于 agent 之间协调的策略 | [任务、决策与策略](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) |

<a id="access-governance-and-operations"></a>

## 访问、治理与运维

| 能力                     | 作用                                                                                                                  | 页面                                                                                                                                                     |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 命名空间与共享           | 组织内隔离与共享的单位；每次读取都限定在一个命名空间内                                                                | [命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)                                                                                          |
| 令牌与角色               | 按命名空间划分的令牌、授权和角色                                                                                      | [令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)                                                                                                      |
| 单点登录与 SCIM          | 面向控制台、CLI 和 MCP 客户端的 OIDC 登录，以及通过 SCIM 取消账号配置                                                 | [单点登录](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)                                                                                                                   |
| 审计日志                 | 每个被审计的操作或服务器事件对应一行，其自由文本值以哈希形式存储，并封存到经过验证的检查点链中                        | [审计日志](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)                                                                                                          |
| 保留与擦除               | 过期、已配置的保留策略，以及在 Loom 保存数据的所有位置擦除某个范围的数据                                              | [保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)                                                                                     |
| 敏感值                   | `high` 敏感级别的记忆不会被嵌入，只能按 key 读取，并且读取会被审计；检测方式可配置                                    | [记忆类型](../concepts/memory.md#memory-types)                                                                                                       |
| 模型调用控制             | 组织管理员可以关闭针对该组织数据的所有模型调用，部署也可以将这些调用路由到推理网关                                    | [关闭模型调用](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#switching-an-orgs-model-calls-off) · [推理网关](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#through-an-inference-gateway) |
| 成本与用量               | 每一笔嵌入和 LLM 费用都记录在产生该费用的操作上                                                                       | [成本与用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)                                                                                            |
| 遥测                     | 按操作记录的账本、OpenTelemetry 追踪、日志和指标，以及查询归因                                                        | [遥测](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)                                                                                                      |
| 控制台                   | 供人处理冲突、提案、任务、授权请求和策略的 Web 应用                                                                   | [控制台](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)                                                                                                                    |
| SQL 访问                 | 针对租户的记忆、历史、对话和请求账本的只读 ClickHouse 视图，也可从 Postgres 读取                                      | [SQL 接口](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md) · [从 Postgres 查询](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)                                                     |
| 副本与 Kubernetes        | 位于同一 URL 之后的相同副本，以及 Helm chart                                                                          | [运行 N 个副本](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md) · [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)                                                              |
| Postgres 记忆权威存储    | 可选：将实时记忆和文档头部存放在 Postgres 中，首次启动时选定                                                          | [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)                                                                                                                    |
| 备份与恢复               | ClickHouse 备份，以及所用的文档存储桶和 Postgres                                                                      | [备份与恢复](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)                                                                                              |
| 升级                     | 替换镜像，其迁移在启动时应用；全新安装走同样的路径                                                                    | [升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)                                                                                                                    |
| 命名空间复制与 OKF 包    | 按计划将命名空间复制到另一个 Loom，或导出并导入为 OKF 包                                                              | [复制命名空间](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) · [OKF 包](../features/integrations/okf.md)                                                        |
| 用量导出                 | 某个 UTC 日的用量和命名空间快照，以 Parquet 格式写入 S3，供数据仓库使用                                               | [分析导出](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)                                                                                                    |

<a id="ways-to-connect"></a>

## 连接方式

适用于任何 MCP 客户端的 MCP，面向 Claude Code 和 Codex 的插件，框架适配器，HTTP API，Python 和 TypeScript SDK，以及 `loom` CLI：
[集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md) ·
[MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ·
[框架](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md) ·
[HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md) · [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) ·
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) · [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)。
