<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=d96f44f5824749f9ff8d8d20e70fb23370c26d8b model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# OpenAI Agents 对话会话

`LoomSession` 在 Loom 的情景记忆之上实现了原生的 Python 和 TypeScript 会话接口。现有工具可以另行保留对话之外的事实。框架包仍然是可选依赖。

每个条目对应一次 `memory.set` 调用，保存其原生 JSON，使用由时间戳和随机数组成的键，并带有终端用户，以及由用户和原生会话 id 派生的会话。单点写入绕过 SDK 合并，因此并发的重复条目仍保持独立。情景记忆行会保留重复条目，不做语义提取，也不合并重复项。Python 和 TypeScript 使用各自独立的派生会话前缀，因为它们的原生条目格式不同。

读取时通过 `memory.list` 分页，对重叠的键去重，并用 `memory.get` 补全缺失或被截断的值。条目按键顺序返回，若指定了 limit，则保留最新的条目。带正数 limit 的读取同样会扫描全部历史，因为服务器的到达时间无法限定由写入方生成的键。TypeScript 适配器在构造时对已校验的用户做快照。其原生的图像和文件字节载荷在存储中使用 JSON 数组，返回时为 `Uint8Array`；普通 JSON 和媒体信息保持不变。删除操作会携带用户和派生会话：`pop_item` 删除最新条目，`clear_session` 删除已列出的历史。并发的追加或清除都是各自独立的服务器操作，而不是跨整个对话的事务。如果某个条目在补全之前消失，读取会失败，而不是返回不完整的原生条目。键使用写入方的时钟；并发的写入方不共享服务器端的序列。

两种实现都会在构造时以及每次回调时，保留应用已有的绑定保护族。Python 还会保留回调上下文中处于活动状态的贡献者。重新绑定客户端无法移除先前的贡献者。原生用户 id 和会话 id 只用于选择存储的历史，不会确立保护族。每个逻辑对话族请使用一个会话实例，并在恢复该对话时重新打开同一个由应用持有的保护胶囊。

原生历史的读取和追加，都作为自动工作在所有保留的贡献者之下运行。带标记的回复会在其值返回之前保护每一个贡献者。第一次完整的带标记历史回复仍可能返回其条目；之后的分页、补全或条目写入会被抑制。带标记的条目写入会在下一个条目之前中止批量写入。显式的 pop 和 clear 操作在保护生效之后仍然可用，并继续在保留的贡献者之下观察回复。由模型选择的工具保持其显式操作语义。

<a id="source-entry-points"></a>

## 源码入口

- `sdk/python/loom/integrations/openai_agents.py`：`LoomSession`。
- `sdk/typescript/src/integrations/openai-agents.ts`：`LoomSession`。

<a id="invariants"></a>

## 不变量

agent 的组织、命名空间和名称共同限定每份历史的范围。省略用户时，写入的是共享的命名空间行；显式指定用户时，则选择该用户的历史。服务器提供操作者和来源的出处信息。适用常规的记忆删除、擦除和滚动升级规则。不会新增本地历史数据库或模式。
