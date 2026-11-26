<!-- i18n: source=docs/index.md blob=173314858f057f518abf314eb98af3afff03c87b model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/index.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/index.md), which is authoritative.

<a id="clickhouse-loom-documentation"></a>

# ClickHouse Loom 文档

Loom 是面向 AI agent 的记忆、文档和代码索引服务器，数据存储在 ClickHouse 中，也可以选择用 [Postgres](architecture/storage-authority.md#why-two-stores) 存放实时记忆和知识状态。agent 和用户可以通过 MCP、HTTP、Python 和 TypeScript SDK 以及控制台访问它。本页按阅读顺序列出了所有页面。

![控制台的资料库：搜索退款规则，返回带引用的段落](https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/document-search.webp)

<video src="https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/clip-assistant-answer.webm" controls autoplay loop muted playsinline width="100%" aria-label="Ask AI answering a question about refunds, with its numbered sources checked against Loom"><a href="https://github.com/ClickHouse/loom/blob/main/docs/assets/screenshots/clip-assistant-answer.webm">Ask AI 回答问题并附带已核对的来源（视频片段）</a></video>

<a id="start-here"></a>

## 从这里开始

| 步骤                | 阅读                                                                                                                                                                                      |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. 评估             | [概览](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md) → [评估 Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)                                                                                                    |
| 2. 安装             | [快速开始](getting-started/quickstart.md) → [部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)                                                                                                      |
| 3. 连接 agent       | [连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) → [集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)                                                                      |
| 4. SDK 和 API       | [你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md) → [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) / [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                                                            |
| 5. 命名空间与访问   | [身份与命名空间](concepts/identities-and-namespaces.md) → [令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)                                                                     |
| 6. 代码索引         | [代码索引](features/code-index/overview.md) → [代码仓库](features/code-index/repositories.md) → [代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)                        |
| 7. 运维             | [监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md) · [故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md) · [升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [备份与恢复](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md) |

<a id="use-cases"></a>

## 使用场景

每个需求一个页面，说明 Loom 能为此做什么，以及从哪里开始：
[agent 记忆](use-cases/agent-memory.md) ·
[文档搜索与 RAG](use-cases/document-search.md) ·
[面向编码 agent 的代码索引](use-cases/code-intelligence.md) ·
[按语言划分的代码索引](use-cases/code-intelligence-by-language.md) ·
[与代码关联的记忆](use-cases/memory-linked-to-code.md) ·
[编码 agent 的模型成本](use-cases/coding-agent-cost.md) ·
[多 agent 共享上下文](use-cases/multi-agent.md) ·
[自主 agent：召回、审计、擦除](use-cases/autonomous-agents.md) ·
[运行、追踪与 PR 历史](use-cases/runs-and-pr-history.md) ·
[基于 ClickHouse 的自托管](use-cases/self-hosted-clickhouse.md) ·
[所有使用场景](use-cases/index.md)。

<a id="1-getting-started"></a>

## 1. 快速入门

- [概览](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md)：Loom 是什么、适用场景、前提条件。
- [包含的功能](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/whats-included.md)：Loom 的主要能力，每项都附有对应页面。
- [快速开始](getting-started/quickstart.md)：完整走通本地部署。
- [配置模型和 API 密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)：agent 订阅、服务器 LLM、嵌入模型以及单一提供商的配置。
- [评估 Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)：冒烟测试、工作负载评估以及可复现的公开基准测试。
- [连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)：插件或 MCP 接入。
- [通过编码 agent 设置 Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md)：适用于由 agent 安装固定版本并验证其可用的场景，附有固定格式的报告。
- [你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)：SDK 教程。

<a id="2-install-and-run-the-server"></a>

## 2. 安装并运行服务器

- [部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)：部署形态、环境、提供方、身份模式、代理、镜像、生产环境检查清单。
- [单点登录](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)：为控制台、`loom login` 和 MCP 客户端接入 OIDC 身份提供方。
- [不使用控制台使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md)：`loom login`、`loom call` 和 SDK 可完成控制台每个视图的调用。
- [升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [受支持的版本](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md) ·
  [备份与恢复](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md) ·
  [监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md) · [维护](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md) ·
  [运行时设置](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md) ·
  [故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md) ·
  [代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md) ·
  [运行 N 个副本](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)：多个相同的 Loom 位于同一个 URL 之后 ·
  [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)：Postgres 记忆权威存储 ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)：Helm chart，附带 EKS 和 kind 覆盖配置 ·
  [ClickHouse Cloud 与 Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)：基于这两项托管服务的 chart ·
  [将命名空间复制到另一个 Loom](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) ·
  [将用量导出到数据仓库](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)

<a id="3-connect-agents"></a>

## 3. 连接 agent

[集成](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md) ·
[MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ·
[Agent 指令](features/integrations/agent-instructions.md) ·
[Claude Code](features/integrations/claude-code.md) ·
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) · [Cursor](features/integrations/cursor.md) ·
[VS Code](features/integrations/vscode.md) ·
[框架](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md) ·
[Grep 垫片](features/integrations/grep-shim.md) ·
[Langfuse](features/integrations/langfuse.md) ·
[ClickStack](features/integrations/clickstack.md) ·
[OKF](features/integrations/okf.md)

<a id="4-sdk-and-api"></a>

## 4. SDK 与 API

[参考索引](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md) · [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md) ·
[MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md) · [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) ·
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) · [环境变量](https://github.com/ClickHouse/loom/blob/main/docs/reference/environment.md) ·
[运维](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md) · [配置](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) ·
[工作进程](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md) · [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md) · [错误](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md) ·
[状态](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) · [数据模式](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md) ·
[提取器契约](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md)。各个版本之间，操作和字段会有变化；[未知字段和已移除字段](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)说明了客户端如何处理服务器不再读取的字段。

<a id="5-namespaces-access-and-the-memory-model"></a>

## 5. 命名空间、访问与记忆模型

- [身份与命名空间](concepts/identities-and-namespaces.md)：元组、角色、授权和读取范围。
- [记忆](concepts/memory.md)：行、写入如何被判定、时间、历史、来源。
- [检索与排序](concepts/retrieval-and-ranking.md)：检索路径、融合、弃答、校准。

<a id="guides"></a>

## 指南

[agent 集成](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) ·
[无头机器人](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md) ·
[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) ·
[跟踪并恢复长时间运行](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) ·
[优化命名空间](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md) ·
[调查类 agent 的记忆](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md) ·
[提取流水线](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md) ·
[性能调优](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md) · [调试](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md) ·
[从 Postgres 查询 Loom](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md) ·
[使用 Loom 教学](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md)

<a id="features"></a>

## 功能

- 平面：[命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) · [知识](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md) ·
  [实体图](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md) ·
  [对话与会话](features/conversations-and-sessions.md) ·
  [运行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) · [任务与决策](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) ·
  [控制台](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)
- 记忆：[写入](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md) ·
  [搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) ·
  [冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) ·
  [历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md) ·
  [保留与删除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) ·
  [流程与经验](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md) ·
  [工作记忆与档案](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- 可观测性：[审计](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md) ·
  [成本与用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md) ·
  [遥测](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)
- 跟踪器：[概览](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) ·
  [记录约定](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)

<a id="6-code-index"></a>

## 6. 代码索引

[概览](features/code-index/overview.md) ·
[仓库](features/code-index/repositories.md) ·
[新鲜度](features/code-index/freshness.md) ·
[历史与 PR](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) ·
[运维](features/code-index/operations.md) ·
[代码索引运维手册](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)

<a id="architecture"></a>

## 架构

[系统概览](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) ·
[服务器布局](architecture/server-layout.md) ·
[ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md) ·
[记忆架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) ·
[存储权威](architecture/storage-authority.md) ·
[单元](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md) ·
[存储布局](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md) ·
[代码索引架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/code-index-architecture.md) ·
[文档提取](architecture/document-extraction.md) ·
[文档来源](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md) ·
[LLM 与嵌入](architecture/llm-architecture.md) ·
[安全](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md) ·
[助手设计](https://github.com/ClickHouse/loom/blob/main/docs/architecture/assistant.md) ·
[故障与恢复](https://github.com/ClickHouse/loom/blob/main/docs/architecture/failure-and-recovery.md) ·
[滚动升级](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)

<a id="benchmarks"></a>

## 基准测试

[概览](benchmarks/overview.md) · [覆盖范围](benchmarks/coverage.md) · [方法论](benchmarks/methodology.md) · [运行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md) · [结果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) · [添加用例](benchmarks/adding-a-case.md) · [Skill 评测](benchmarks/skill-eval.md)

<a id="development"></a>

## 开发

面向贡献者：[仓库指南](development/repository-guide.md) ·
[本地开发](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md) ·
[测试](development/testing.md) · [代码生成](development/code-generation.md) ·
[迁移](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md) ·
[发布流程](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md) ·
[文档风格](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md) ·
[整理循环设计](https://github.com/ClickHouse/loom/blob/main/docs/development/curation-loop-design.md) ·
[扩展 Loom](development/extending-loom.md) ·
[添加操作](development/adding-an-operation.md) ·
[添加 worker](development/adding-a-worker.md) ·
[添加表](development/adding-a-table.md) ·
[添加语言支持](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md) ·
[控制台设计](https://github.com/ClickHouse/loom/blob/main/docs/development/dashboard-design.md)。
[CONTRIBUTING.md](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) 介绍了 pull request 的工作流程。

<a id="also"></a>

## 另请参阅

[术语表](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md)：Loom 的术语，每个只定义一次。[常见问题](faq.md)：最先会遇到的问题，每个问题一段回答。[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)：为 agent 生成的文档索引；[`llms-full.txt`](https://loom.clickhouse.com/docs/llms-full.txt) 将说明 Loom 是什么以及如何开始使用的页面合并在一个文件中。
