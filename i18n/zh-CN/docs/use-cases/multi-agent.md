<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# 多 agent 工作流的共享上下文

Loom 为多 agent 工作流中的各个 agent 提供一个共享上下文的统一位置：同一命名空间内所有 agent 都能读取的记忆、用于把工作从一个 agent 交给另一个 agent 的任务收件箱，以及决策记录。工作流由你的 agent 和编排器运行，Loom 负责保存它们学到的内容和交接的内容。

<a id="what-you-get"></a>

## 你能获得什么

- **带来源的共享记忆。** 每一行都会记录是哪个 agent 写入的；在默认的 `auto_reinforce` 策略下，两个 agent 之间的矛盾会被标记出来等待审核，而不会被悄悄覆盖。[命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [命名空间冲突策略](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **任务收件箱。** 每个 agent 都有一个持久队列，支持租约、重试和审核规则；已被领取的任务同一时间只有一个所有者。[任务、决策与策略](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **决策。** 在类似情况下做出了什么决定、原因是什么，以及是否奏效。[决策](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **工作记忆。** agent 每次都会获得的文本，而不是通过搜索去查找，与长期记忆分开。[工作记忆与配置档案](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **上下文包。** `context.pack` 会构建 agent 在任务开始时加载的有界数据包。[统一搜索与上下文包](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **框架适配器。** SDK 通过各 agent 框架自己的记忆接口来呈现 Loom。[Agent 框架](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## 开始

[将 Loom 集成到 agent 中](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)介绍了每个 agent 发送的身份信息以及应写入的内容；[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)为每个 agent 分配各自的令牌。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)中被标记为实验性。
