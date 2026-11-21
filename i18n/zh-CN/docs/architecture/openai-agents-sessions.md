<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=96269df2f0037fdcb16f6a101e85bf9aafedef73 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# OpenAI Agents 对话会话

`LoomSession` 基于 Loom 的情景记忆实现了 Python 和 TypeScript 的原生会话接口。现有工具可以另外保留对话之外的事实。框架包仍是可选依赖。

每个条目对应一次 `memory.set` 调用，内容为其原生 JSON，键由时间戳和随机数组成，并带有终端用户，以及由用户和原生会话 id 派生的会话。单点写入会绕过 SDK 的合并，因此并发写入的重复条目会各自保留。情景记忆行会保留重复条目，不做语义提取，也不合并重复项。Python 和 TypeScript 使用不同的派生会话前缀，因为两者的原生条目格式不同。

读取时通过 `memory.list` 分页，对重叠的键去重，并用 `memory.get` 补全缺失或被截断的值。条目按键顺序返回，指定 limit 时保留最新的条目。带正数 limit 的读取同样会扫描全部历史，因为服务器的到达时间无法界定由写入方生成的键。TypeScript 适配器在构造时对已验证的用户做快照。其原生的图像和文件字节载荷在存储中使用 JSON 数组，返回时为 `Uint8Array`；普通 JSON 和媒体信息保持不变。删除时会携带用户和派生会话：`pop_item` 删除最新的条目，`clear_session` 删除已列出的历史。并发的追加或清除是各自独立的服务器操作，而不是跨整个对话的事务。如果某个条目在补全之前消失，读取会失败，而不会返回不完整的原生条目。键使用写入方的时钟；并发的写入方不共享服务器端的序列。

<a id="source-entry-points"></a>

## 源码入口

- `sdk/python/loom/integrations/openai_agents.py`：`LoomSession`。
- `sdk/typescript/src/integrations/openai-agents.ts`：`LoomSession`。

<a id="invariants"></a>

## 不变量

agent 的组织、命名空间和名称共同界定每份历史的范围。省略用户时，会写入共享的命名空间行；显式指定用户时，则选择该用户的历史。服务器提供操作者和来源的溯源信息。适用常规的记忆删除、擦除和滚动升级规则。不会新增本地历史数据库或 schema。
