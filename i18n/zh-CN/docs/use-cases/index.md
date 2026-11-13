<!-- i18n: source=docs/use-cases/index.md blob=178effa61120f8d47e67f6ff291322133ca3e84f model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md), which is authoritative.

<a id="use-cases"></a>

# 使用场景

每个页面回答人和 agent 在寻找工具时会问的一个问题：Loom 如何满足该需求，以及从哪里开始。

| 需求                                                              | 页面                                                       |
| ----------------------------------------------------------------- | ---------------------------------------------------------- |
| 面向 AI agent 的长期记忆和共享记忆（MCP 记忆服务器）              | [Agent 记忆](agent-memory.md)                              |
| 面向 agent 的知识和文档搜索（RAG）                                | [文档搜索](document-search.md)                             |
| 面向编码 agent 的代码索引和代码智能                               | [代码索引](code-intelligence.md)                           |
| 代码索引对各语言提供的能力（C++、Python、TS、Go 等）              | [按语言划分的代码索引](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md) |
| 降低编码 agent 的模型成本                                         | [编码 agent 成本](coding-agent-cost.md)                    |
| 多 agent 工作流中 agent 之间的共享上下文                          | [多 agent](multi-agent.md)                                 |
| 自主 agent 的记忆、召回、审计和擦除                               | [自主 agent](autonomous-agents.md)                         |
| agent 运行记录、追踪、跟踪器和 PR 历史                            | [运行记录和 PR 历史](runs-and-pr-history.md)               |
| 基于 ClickHouse 自托管 agent 记忆，使用 Docker 或 Kubernetes      | [基于 ClickHouse 自托管](self-hosted-clickhouse.md)        |

[Loom 不适用的情况](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)列出了其他工具更适合的需求。
