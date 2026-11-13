<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# 面向自主 agent 的记忆、召回、审计与擦除

Loom 为自主 agent 提供可供日后召回的记忆、记录谁写入了什么的审计记录，以及在 Loom 保存数据的所有位置擦除某个范围的能力。这些都是服务器端操作，数据存储在 ClickHouse 中（实时记忆和知识状态则存储在 Postgres 中）。

<a id="what-you-get"></a>

## 你能获得什么

- **记忆与召回。** 带有来源信息的多版本事实；在没有足够接近的结果时会放弃作答的搜索；以及对成功运行所依赖的记忆提升排序的机制。[记忆](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [运行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **拒绝写入。** 被固定的事实会拒绝与之矛盾的写入，`blacklist.add` 可注册一个任何写入都不得存储的模式。[冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **审计。** 每个修改记忆的操作都会留下一行审计记录，其中包含内容的 SHA-256，并且一个 worker 会将每小时的窗口封存为哈希链，因此任何编辑或删除都可以被检测到。传输层的审计写入是 fail-open 的（插入失败会丢失该行）；在 Postgres 作为权威存储时，记忆修订版本的审计行与该修订一同存储。[审计日志](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **可核查的擦除。** `erasure.request` 会从所有保存记忆内容的表中擦除某个范围（默认使用墓碑标记；`purge=true` 会删除 `user_id` 或 `namespace_list` 范围的行），并以残留普查和回执结束。[保留与擦除](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **回滚。** `namespace.rollback` 会把命名空间中的每条记忆恢复到某个时间点的版本。[历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **无头运行。** bot 拥有自己的身份（在开放自助注册的地方可自行注册），在首次运行前检查其连接，并且在它停止响应时可以进行诊断。[无头 bot](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

审计和擦除是工程层面的基础能力，并非合规认证。[安全](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

本页中的部分操作在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。
