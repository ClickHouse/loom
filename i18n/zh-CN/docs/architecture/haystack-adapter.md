<!-- i18n: source=docs/architecture/haystack-adapter.md blob=c2cfa8a3d7cfb7c046a16c39b584aa93d1ea0420 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md), which is authoritative.

<a id="haystack-document-store-and-retriever"></a>

# Haystack 文档存储与检索器

可选的 Haystack 3.5 适配器通过同步的 Loom SDK 实现其文档存储协议和原生检索组件。核心 SDK 和服务器不依赖该框架。每次 HTTP 调用的客户端生命周期由 SDK 管理；适配器不会留下服务器、本地数据库、缓存或持久的后台进程。

<a id="invariants"></a>

## 不变量

存储会把每个原生文档 id 映射为位于存储专属前缀下的哈希来源 URI。匹配的标签用于限定列表和检索的范围。完整的原生文档序列化内容（包括元数据、嵌入向量和二进制字段）保存在文档的原始文件字节中。分块元数据只携带一个很小的格式标记；文本作为文档内容建立索引，没有文本时则索引 id。Loom 常规的文档写入、来源溯源、命名空间授权、版本控制和擦除路径适用于每一次写入和删除。文档在命名空间内共享，而不是用户私有。

计数和过滤会通过游标分页读取并还原整个存储。原生过滤辅助函数负责处理比较和布尔逻辑。检索最多搜索 100 个分块，并带有明确的命名空间、标签和 URI 边界，然后还原完整文档，按来源 URI 去重，同时保留每个文档排名最高的命中结果。这些操作没有跨请求的快照。并发写入时，Loom 可能返回冲突。重复检查和多文档操作不是事务。

序列化后的连接信息携带 Haystack 环境变量 Secret 引用；原始的 Secret 令牌会拒绝序列化。显式的端点 URL 不允许内嵌密钥。适配器保留 Haystack 的管道反序列化信任策略，从不注册受信任的模块，也不会选择不安全的反序列化。

<a id="source-entry-points"></a>

## 源码入口

- `sdk/python/loom/integrations/haystack.py`：`LoomDocumentStore` 和 `LoomRetriever`。
- `sdk/python/tests/test_haystack.py`：原生文档存储与管道行为。
- `tests/integration/test_sdk_haystack.py`：实时文档持久化与检索。
