<!-- i18n: source=docs/architecture/n8n-adapter.md blob=f2efbe08cacbe3e9b1bb2cb39161b71c20e2be32 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md), which is authoritative.

<a id="n8n-adapter"></a>

# n8n 适配器

n8n 社区包通过 TypeScript SDK 将对话历史和持久事实存储在 Loom 部署中。这些节点不增加任何存储服务或本地缓存。

<a id="interfaces-and-storage"></a>

## 接口与存储

`LoomMemory.supplyData` 返回框架的 `BufferWindowMemory`，其底层是 `BaseListChatMessageHistory` 的 `LoomChatHistory` 实现。消息序列化使用框架的已存储消息格式。窗口只改变发送给模型的上下文，较早的消息仍可供后续读取和显式清除使用。

每条消息都是一条 `episodic` 记忆，其键中带有时间戳和随机后缀。episodic 写入会保留重复消息，不做语义合并。Loom agent 的组织、命名空间和 agent 名称限定历史记录的范围。用户以及工作流/会话标识符的摘要提供其余的身份字段。读取时通过 `memory.list` 分页，在过滤投影行时对重叠的页去重，按键排序，并对被截断的值使用 `memory.get`。`clear` 只删除这些条目。并发写入方在一整个轮次内没有事务保证。

Loom 操作节点调用 `memory.set`、`memory.search` 和 `knowledge.search`。其输入提供组织、命名空间、agent 名称和可选的用户。服务器根据令牌和操作推导来源信息，并强制执行命名空间授权。

<a id="packaging"></a>

## 打包

该包捆绑了 SDK 和框架库，以避免与其他社区节点产生运行时依赖冲突。其构建产物包含它们的许可证文件。`n8n-workflow` 由宿主提供，作为 peer 依赖。开发依赖固定了测试所用的接口，不会添加到 Loom 的服务器或 SDK 依赖中。

该包面向自托管的 n8n，并使用官方节点 CLI 针对该目标的配置。它不声称已在 Cloud 上验证。发布标签工作流会先构建并检查该包，然后使用 npm trusted publisher 和 provenance 发布，仓库中不保存任何注册表令牌。

<a id="invariants"></a>

## 不变量

- 适配器只会将数据发送到其 n8n 凭据中的 Loom URL。
- 由令牌授权来控制访问，用户参数只是过滤条件，不是授权。
- 清除对话不会删除由操作节点创建的持久事实。
- SDK 或服务器的拒绝会使节点失败；在启用失败后继续时，则变为错误项。
- 适配器不创建表，并使用 Loom 现有的擦除路径。

<a id="source-entry-points"></a>

## 源码入口

- `integrations/n8n/nodes/LoomMemory/LoomChatHistory.ts`：消息持久化。
- `integrations/n8n/nodes/LoomMemory/LoomMemory.node.ts`：框架记忆适配器。
- `integrations/n8n/nodes/Loom/Loom.node.ts`：持久记忆和文档操作。
- `.github/workflows/n8n-publish.yml`：检查与 trusted publishing。

[框架设置](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n)
