<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI agent 的长期共享记忆

Loom 是一个自托管的 MCP 服务器和 HTTP API，为 AI agent 提供可共享的长期记忆：一个 agent 在会话中保存的事实，可以被下一次会话、其他 agent，或者在控制台上的用户找到。本页介绍这一机制的工作方式，以及从哪里开始上手。

<a id="what-you-get"></a>

## 你能得到什么

- **每个 key 对应一条声明，存放在一个命名空间中。** `memory.set` 保存一条事实；`memory.get` 按 key 读回；`memory.search` 和 `loom_search` 按语义查找。命名空间是共享的基本单位：默认情况下，每个成员都能读取其中的每一行。
  [记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **可以被修正的记忆。** 每次变更都会生成带有历史记录的新版本。经规范化后相同的事实会合并到已存储的行中，用不同措辞表达的相同事实会作为重复项存储，相互矛盾的内容会被标记等待审核，针对已固定（pinned）事实的写入会被拒绝。
  [冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **会弃权的搜索。** 当没有任何结果达到相似度下限时，`memory.search` 会返回 `abstain=true` 和一个原因码，并将每个较弱的命中标记为 `below_threshold`；`loom_search` 则会丢弃这些结果。[搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **跨编码 agent 的记忆。** Claude Code 和 Codex 插件会在会话开始时加载相关上下文，并在会话运行过程中捕获内容；Cursor 和其他 MCP 客户端可连接到同一个服务器。[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **为你自己的 agent 提供按用户划分的记忆。** Python 和 TypeScript SDK 可按最终用户写入和读取记忆。[你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 实测结果

在 100 道配对的 LongMemEval-S 问题上，使用相同的 `gpt-5` reader，基于 Loom 检索到的记忆生成的答案得分为 94%，而基于完整历史记录生成的答案得分为 90%，reader 的提示词 token 数减少了 91%（不含构建记忆的一次性成本）；两者的准确率差异在统计上不显著。
[使用与不使用 Loom 的对比](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## 开始

[Quickstart](../getting-started/quickstart.md) 会启动本地服务栈，并通过 HTTP、Python 和 MCP 保存第一条记忆。[When Loom does not fit](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) 介绍了进程内记忆库更适用的场景。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。
