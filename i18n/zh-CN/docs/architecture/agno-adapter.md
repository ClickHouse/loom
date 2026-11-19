<!-- i18n: source=docs/architecture/agno-adapter.md blob=3cda341b0d2ad39a80b6546d3002b103a90de53d model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md), which is authoritative.

<a id="agno-user-memory-database"></a>

# Agno 用户记忆数据库

Agno 适配器通过同步的 Loom SDK 实现其 3.2 版 `BaseDb` 接口中的用户记忆方法。这个可选的、固定次版本的依赖不会进入核心 SDK 和服务器。`MemoryManager` 可以独立于 agent 的会话数据库使用这个数据库。与记忆无关的功能会直接拒绝，而不是使用框架默认的存储。

<a id="invariants"></a>

## 不变量

每个适配器都绑定一个用户和一个应用存储。每次 SDK 调用都携带该用户和派生出的会话；键包含同一个作用域摘要以及原生 id 摘要。如果指定了不同的用户，则会被拒绝。原生用户记忆以 JSON 形式保存在 Loom 的情景行中，保留 id、元数据和修订版本，不会对不同的原生记录做语义合并。操作者和来源的出处信息来自 Loom 的常规写入路径。适用常规的记忆删除、擦除和滚动 schema 规则。

列表按照限定数量并经过投影的分页方式读取，对重叠部分去重，并在转换为原生对象之前补全被截断的值。内容、主题和归属过滤，以及排序、分页和统计，都在这个限定了用户和存储范围的快照上执行。没有本地持久状态。并发编辑时保留服务器最后一次写入；批量操作是一系列写入，失败时可能只完成一部分。需要隔离互不信任的应用时，请使用命名空间授权。

<a id="source-entry-points"></a>

## 源码入口

- `sdk/python/loom/integrations/agno.py`：`LoomMemoryDb`。
- `sdk/python/tests/test_agno.py`：原生记忆管理器的行为。
- `tests/integration/test_sdk_agno_memory.py`：实际运行中的记忆作用域和出处信息。
