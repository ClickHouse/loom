<!-- i18n: source=docs/use-cases/multi-agent.md blob=d147692b665800cf10e0dfda74f2fdc9f9de8293 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# 多 agent 工作流的共享上下文

Loom 为多 agent 工作流中的各个 agent 提供一个共享上下文的地方：命名空间内每个 agent 都能读取的记忆、用于把工作从一个 agent 交给另一个 agent 的任务收件箱，以及决策记录。工作流由你的 agent 和编排器运行，Loom 保存它们学到的内容和交接的内容。

<a id="what-you-get"></a>

## 你能得到什么

- **带来源信息的共享记忆。** 每一行都会记录是哪个 agent 写入的，在默认的 `auto_reinforce` 策略下，两个 agent 之间的矛盾会被标记出来等待审查，而不是被悄悄覆盖。[命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [命名空间冲突策略](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **任务收件箱。** 每个 agent 都有一个持久队列，带有租约、重试和审查规则；已认领的任务同一时间只有一个所有者。[任务、决策与策略](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **决策。** 在类似情况下做了什么决定、原因是什么，以及是否奏效。[决策](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **工作记忆。** 每次都会提供给 agent 的文本，而不是靠搜索找到，与长期记忆分开。[工作记忆与配置档案](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **上下文包。** `context.pack` 会构建 agent 在任务开始时加载的有界内容包。[统一搜索与上下文包](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **会话简报。** 一条工作线的可恢复状态：目标、状态、决策、死胡同和已完成的内容。一个 agent 用 `session.brief_save` 保存，下一个用 `session.brief_get` 加载。[会话简报](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md#session-briefs)
- **框架适配器。** SDK 通过各 agent 框架自带的记忆接口来呈现 Loom。[Agent 框架](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="two-agents-sharing-a-research-note"></a>

## 两个 agent 共享一条研究笔记

在个人笔记本电脑上，[单用户快速入门](../getting-started/quickstart.md)可以让两个已连接的 agent 读取同一个命名空间。按照[连接指南](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)连接两个客户端；例如，用 Claude Code 收集发现，用 Cursor 据此写作。

1. 在第一个客户端中，让它使用 `loom_memory_set` 保存 `example/research/next-step`，值为“Check the paper's evaluation dataset before adding it to the reading list.”。检查返回的 `memory_id`，并在控制台中找到这一行。
2. 在另一个客户端中，让它使用 `loom_memory_get` 读取这个确切的键。要求它在起草下一步之前，先拿到工具响应，其中包括 `found`、`value` 和 `agent`。它应当读到第一个客户端写入的值，并识别出写入者。
3. 完成后，从写入该示例键的客户端中删除它。

这些 agent 共享的是一条已保存的事实；Loom 不运行它们，也不调度它们的工作。对于位于不同人员机器上的 agent，请使用[带身份验证的团队部署](../getting-started/quickstart.md#start-in-multi-user-mode)，由每个人自己的令牌授予对同一命名空间的访问权限。本地单用户运行无法测试这些访问控制。

<a id="start"></a>

## 开始

[将 Loom 集成到 agent 中](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)介绍了每个 agent 发送的身份以及该写入什么内容；[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)可为每个 agent 分配各自的令牌。

本页中的部分操作在[状态页面](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。
