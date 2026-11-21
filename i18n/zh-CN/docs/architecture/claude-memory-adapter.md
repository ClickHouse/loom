<!-- i18n: source=docs/architecture/claude-memory-adapter.md blob=b72a628092f2f86d148e87a01ed0f9e99469b83f model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md), which is authoritative.

<a id="claude-api-memory-file-tree"></a>

# Claude API 记忆文件树

Claude API 记忆适配器通过 Loom 知识文档实现框架的 Python 记忆工具基类和 TypeScript 记忆处理器。框架 SDK 是可选依赖。两种实现使用相同的文档 URI 和存储标签，因此任一语言都可以读取另一种语言创建的文件。

文件的形式为 `loom-memory://<store>/memories/<path>`。目录是 URI 前缀，没有单独的目录行。路径会被规范化，除非仍位于 `/memories` 之下，否则会被拒绝。读取目录使用 `knowledge.list`，并带有存储标签、URI 前缀和投影的 URI 字段；读取文件使用 `knowledge.get`。编辑使用 `knowledge.upsert`，删除使用有数量上限的 `knowledge.bulk_delete` 调用。重命名会先复制每个文件，再删除其旧 URI。重命名失败可能会留下副本，并且同时进行的读取-修改-写入编辑会保留后一次写入。

<a id="source-entry-points"></a>

## 源码入口

- `sdk/python/loom/integrations/claude_memory.py`：`AsyncLoomMemoryTool` 和 `LoomMemoryTool`。
- `sdk/typescript/src/integrations/claude-memory.ts`：`loomMemoryTool` 和 `loomMemoryHandlers`。

<a id="invariants"></a>

## 不变量

存储用于对文件分组；访问由命名空间授权控制。私有用户树需要各自独立的命名空间。适配器不会把存储名称解释为访问授权。操作者、来源溯源、文档修订、blob 清理和擦除均沿用现有的知识操作。不会新增本地持久状态、数据库或模式。目录操作不是原子快照。
