<!-- i18n: source=docs/architecture/google-adk-adapter.md blob=6c709e61102ee7413e9b4764bfd5c80f4511830e model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md), which is authoritative.

<a id="google-adk-memory-service"></a>

# Google ADK 记忆服务

Google ADK 适配器通过 Loom 的 Python 和 TypeScript SDK 实现该框架的记忆服务。框架包是可选依赖。每个应用都使用独立的 Loom 命名空间，因为其中的知识文档会与该命名空间的所有读取者共享。

会话写入会从处于活动状态的用户事件和模型事件中提取文本，排除工具负载和思考内容，然后分批调用 `memory.set_from_messages`，每批大小有上限。两个 SDK 在发起任何调用之前，都会拒绝空的、哨兵值的以及带有首尾空白的用户 id。每次调用都携带最终用户，以及由用户和原生会话 id 派生出的会话。Python 还实现了已安装框架中的增量事件、直接记忆和删除方法。Python 的删除 id 包含 key、会话、写入者和确切版本，并直接调用 `memory.delete_version`。版本不存在或已被改写时为空操作，且不会选中其他写入者的行。记忆搜索命中结果中新增的 `agent` 响应字段来自存储行的身份，包括被停放的页面。没有已知写入者或版本的命中结果没有删除 id。旧客户端会忽略这个额外字段；要进行精确的 ADK 删除，需要服务器提供该字段。这不会增加持久化列，也不需要迁移。TypeScript 返回按写入者限定范围的信息性 id；其已安装的框架接口没有删除方法。

搜索时，先为绑定的用户调用 `memory.search`，再用限定在命名空间内的 `knowledge.search` 的结果填充剩余的可用结果位置。知识是共享的应用上下文，而不是用户私有的记忆。两个 SDK 使用相同的摘要编码；框架原生的时间戳表示在边界处进行转换。一个服务实例只绑定一个应用。需要隔离互不信任的应用时，使用命名空间授权。

<a id="source-entry-points"></a>

## 源码入口

- `sdk/python/loom/integrations/google_adk.py`：`LoomMemoryService`。
- `sdk/typescript/src/integrations/google-adk.ts`：`LoomMemoryService`。

<a id="invariants"></a>

## 不变量

服务器通过其常规写入路径提供操作者和来源的出处信息。适配器不添加数据库、模式或提供方。现有的记忆删除、知识擦除和滚动升级规则同样适用。语义提取可能会修订事实；该服务不是精确的对话记录存储。知识结果没有用户记忆 id，无法通过记忆服务删除。
