<!-- i18n: source=docs/use-cases/agent-memory.md blob=e9322c4d7b4b3395943f3caead94d6436cd0954b model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI agent 的长期记忆与共享记忆

Loom 是一个自托管的 MCP 服务器和 HTTP API，为 AI agent 提供可共享的长期记忆：一个 agent 在某次会话中保存的事实，可以被下一次会话、另一个 agent，或在控制台查看的人找到。本页介绍其工作方式以及从哪里开始。

<a id="what-you-get"></a>

## 你能获得什么

- **每个键在命名空间中对应一条声明。** `memory.set` 保存一条事实；`memory.get` 按键读回；`memory.search` 和 `loom_search` 按语义查找。命名空间是共享的单位：默认情况下，每个成员都可以读取其中的每一行。
  [记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **可以更正的记忆。** 每次修改都会生成带有历史记录的新版本。规范化之后相同的事实会合并到已存储的行中，换一种说法表达的同一事实会作为重复项存储，相互矛盾的内容会被标记等待审核，对已固定事实的写入会被拒绝。
  [冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **会弃权的搜索。** 当没有任何结果达到相似度下限时，`memory.search` 会返回 `abstain=true` 和一个原因代码，并将每个较弱的命中标记为 `below_threshold`；`loom_search` 则会丢弃这些命中。[搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **为你的编码 agent 提供长期记忆，并在 agent 之间共享。** Claude Code 和 Codex 插件会在会话开始时加载相关上下文，并在会话运行时捕获会话内容。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode、goose、Zed 和其他 MCP 客户端可以连接到同一个服务器，同一命名空间下的 agent 可以找到其他 agent 在其中保存的事实。
  [连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- **与所涉代码关联的记忆。** 编码 agent 的经验会跟随其所指的定义，经历重命名和编辑，并在 agent 阅读该代码时出现。[关联到代码的记忆](memory-linked-to-code.md)
- **为你自己的 agent 提供按用户划分的记忆。** Python 和 TypeScript SDK 按终端用户写入和读取记忆。[你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 实测结果

在 100 个成对的 LongMemEval-S 问题上，使用相同的 `gpt-5` 读取模型，基于 Loom 检索到的记忆给出的答案得分为 94%，而基于完整历史的得分为 90%，读取模型的提示 token 减少了 91%（不含构建记忆的一次性成本）；准确率的差异在统计上不显著。
[使用与不使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="a-research-assistant-that-remembers-your-reading-preferences"></a>

## 记住你阅读偏好的研究助手

完成[快速开始](../getting-started/quickstart.md)后，通过控制台的[客户端设置](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#how-do-i-give-cursor-or-another-mcp-client-shared-memory)连接 Claude Desktop 或 Cursor。这些工具可与原生 LLM 配合使用：由你的聊天应用提供调用它们的模型。

1. 对助手说：“使用 `loom_memory_set` 将我的研究偏好保存在 `example/research/source-policy` 下：在我的阅读列表中包含经过同行评审的论文。”
2. 检查工具响应中是否包含 `memory_id`。在控制台中打开 **Memories**，确认值和写入者。如果工具拒绝了写入，请先解决其报告的问题再继续。
3. 开始一个新的聊天。对助手说：“使用 `loom_memory_get` 读取 `example/research/source-policy`，然后告诉我哪些来源应该放进我的阅读列表。”检查工具是否返回 `found: true` 以及你保存的偏好。
4. 完成后，让助手删除该示例键，然后确认控制台不再将其列为有效记忆。

第二个聊天在没有第一个聊天记录的情况下读取了已存储的偏好。这检验的是持久化和客户端接入是否正常，并不检验助手能否评判论文或回答研究问题。如需对论文本身进行搜索，请使用[文档搜索](document-search.md)。

<a id="start"></a>

## 开始

[快速开始](../getting-started/quickstart.md)会启动本地环境，并通过 HTTP、Python 和 MCP 保存第一条记忆。[Loom 不适用的情况](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)介绍了进程内记忆库更合适的场景。

本页中的一些 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。

使用 [Microsoft Agent Framework 上下文提供程序](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)，可在运行前召回用户事实，并在运行后记录对话。

将 [Claude API 记忆工具](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#claude-api-memory-tool)的文件树存储在 Loom 中，使命名空间内的 agent 可以共享其内容。

将 [OpenAI Agents 会话历史](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#openai-agents-sdk)保存在 Loom 中，以便在多次 agent 运行和交接之间保持。

对于可视化工作流，[n8n 节点](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n)可将 AI Agent 连接到 Loom 的对话历史、持久记忆和知识搜索。
