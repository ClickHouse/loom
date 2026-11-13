<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI 智能体的长期共享记忆

Loom 是一个可自托管的 MCP 服务器和 HTTP API，为 AI 智能体提供它们可以共享的长期记忆：一个智能体在某次会话中保存的事实，可以被下一次会话、另一个智能体，或在仪表板前的用户检索到。本页介绍其工作原理以及如何开始使用。

<a id="what-you-get"></a>

## 你能获得什么

- **每个 key 对应一条声明,按 namespace 组织。** `memory.set` 保存一条事实;`memory.get` 按 key 读回;`memory.search` 和 `loom_search` 按语义查找。namespace 是共享的单位:默认情况下,每个成员都能读取其中的每一行。
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **可以纠正的记忆。** 每次变更都会生成带有历史记录的新版本。规范化之后相同的事实会合并到已存储的行中,换一种说法表达的同一事实会作为重复项存储,相互矛盾的内容会被标记等待审核,而针对已固定(pinned)事实的写入会被拒绝。
  [冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **会弃权的搜索。** 当没有任何结果达到相似度下限时,`memory.search` 会返回 `abstain=true` 和一个原因码,并把每个较弱的命中标记为 `below_threshold`;`loom_search` 则直接丢弃这些结果。[搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **跨编码 agent 的记忆。** Claude Code 和 Codex 插件会在会话开始时加载相关上下文,并在会话运行过程中捕获会话内容;Cursor 和其他 MCP 客户端连接到同一个服务器。[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **为你自己的 agent 提供按用户划分的记忆。** Python 和 TypeScript SDK 可以按终端用户写入和读取记忆。[你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 实测结果

在 100 个成对的 LongMemEval-S 问题上，使用相同的 `gpt-5` 阅读器，基于 Loom 检索到的记忆生成的答案得分为 94%，而基于完整历史记录生成的答案得分为 90%，同时阅读器提示词 token 减少了 91%（不含构建记忆的一次性开销）；准确率差异在统计上并不显著。
[使用与不使用 Loom 的对比](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## 开始

[快速开始](../getting-started/quickstart.md)会启动本地服务栈，并通过 HTTP、Python 和 MCP 保存第一条记忆。[Loom 不适用的情况](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)介绍了进程内记忆库更适合的场景。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)中被标记为实验性。
