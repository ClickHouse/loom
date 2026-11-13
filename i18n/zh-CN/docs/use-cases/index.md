<!-- i18n: source=docs/use-cases/index.md blob=4e8f25dd04959ccdeda89aef0aa7fead3a458cd8 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md), which is authoritative.

<a id="use-cases"></a>

# 使用场景

每个页面回答人和 agent 在寻找工具时会问的一个问题：Loom 如何满足该需求，以及从哪里开始。

| 需求                                                              | 页面                                                   |
| ----------------------------------------------------------------- | ------------------------------------------------------ |
| 为 AI agent 提供长期记忆和共享记忆（MCP 记忆服务器）              | [Agent memory](agent-memory.md)                        |
| 为 agent 提供知识和文档搜索（RAG）                                | [Document search](document-search.md)                  |
| 为编码 agent 提供代码索引和代码智能                               | [Code index](code-intelligence.md)                     |
| 降低编码 agent 的模型成本                                         | [Coding-agent cost](coding-agent-cost.md)              |
| 多 agent 工作流中 agent 之间的共享上下文                          | [Multi-agent](multi-agent.md)                          |
| 为自主 agent 提供记忆、召回、审计和擦除                           | [Autonomous agents](autonomous-agents.md)              |
| agent 运行记录、trace、tracker 和 PR 历史                         | [Runs and PR history](runs-and-pr-history.md)          |
| 基于 ClickHouse 的自托管 agent 记忆，使用 Docker 或 Kubernetes    | [Self-hosted on ClickHouse](self-hosted-clickhouse.md) |

[Loom 不适用的情况](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)列出了其他工具更适合满足的需求。
