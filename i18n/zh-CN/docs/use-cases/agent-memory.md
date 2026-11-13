<!-- i18n: source=docs/use-cases/agent-memory.md blob=27f44a06830b04a8e513cbab521e16aef4bb7034 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI agent 的长期记忆与共享记忆

Loom 是一个自托管的 MCP server 和 HTTP API，为 AI agent 提供可共享的长期记忆：一个 agent 在某次会话中保存的事实，下一次会话、另一个 agent 或者使用控制台的人都可以找到。本页介绍它的工作方式以及从哪里开始。

<a id="what-you-get"></a>

## 你能得到什么

- **每个键一条声明，位于命名空间中。** `memory.set` 保存一条事实；`memory.get` 按键读回；`memory.search` 和 `loom_search` 按语义查找。命名空间是共享的单位：默认情况下，每个成员都能读取其中的每一行。
  [记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **可以纠正的记忆。** 修改会生成带有历史记录的新版本。经规范化后相同的事实会合并到已存储的行中，换一种说法表达的同一事实会作为重复项存储，相互矛盾的内容会被标记待审核，对已固定事实的写入会被拒绝。
  [冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **会弃答的搜索。** 当没有结果达到相似度下限时，`memory.search` 返回 `abstain=true` 和一个原因码，并将每个较弱的命中标记为 `below_threshold`；`loom_search` 则会丢弃它们。[搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **为你的编码 agent 提供长期记忆，并在多个 agent 之间共享。** Claude Code 和 Codex 插件会在会话开始时加载相关上下文，并在会话运行过程中捕获内容。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode、goose、Zed 以及其他 MCP 客户端都可以连接到同一个 server，位于同一命名空间的 agent 能找到其他 agent 在其中保存的事实。
  [连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- **为你自己的 agent 提供按用户划分的记忆。** Python 和 TypeScript SDK 按终端用户写入和读取记忆。[你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 实测结果

在 100 道成对的 LongMemEval-S 问题上，使用相同的 `gpt-5` 阅读模型，基于 Loom 检索到的记忆给出的答案得分为 94%，而基于完整历史的得分为 90%，阅读模型的提示 token 减少了 91%（不含构建记忆的一次性成本）；准确率的差异在统计上并不显著。
[使用与不使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## 开始使用

[快速入门](../getting-started/quickstart.md)会启动本地服务栈，并通过 HTTP、Python 和 MCP 保存第一条记忆。[Loom 不适用的情况](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)介绍了进程内记忆库更合适的场景。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)中被标记为实验性。
