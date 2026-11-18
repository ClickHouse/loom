<!-- i18n: source=docs/architecture/dify-adapter.md blob=34e577e662d3664872b0d8163d92fc018b884cd1 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md), which is authoritative.

<a id="dify-tools-adapter"></a>

# Dify 工具适配器

Dify 插件通过 Python SDK 调用 Loom，本地不存储任何内容。它的三个原生工具分别对应 `memory.set`、`memory.search` 和 `knowledge.search`。可选的独立软件包要求 0.11 插件 SDK 次版本系列和 Loom 1.x SDK；服务器和核心 SDK 不依赖 Dify。

<a id="invariants"></a>

## 不变量

提供方凭据绑定端点、组织、命名空间、令牌和 agent。必填的 User ID 表单参数在模型参数之外绑定终端用户。召回可以跨 Loom agent 读取该用户的记忆；文档则由命名空间共享。两种搜索操作都固定 `namespace_scope=this`。对于互不信任的应用，应使用相互独立的命名空间和令牌授权加以约束。Loom 对这些调用同样适用其通常的 actor、来源追溯、冲突处理、滚动 schema 和擦除规则。

HTTP 目标由管理员控制。插件会检查 URL 的格式，并拒绝内嵌凭据、查询字符串和片段；插件运行器能访问哪些主机，则由部署的网络策略限制。请求设有有限的超时时间，且只尝试一次。工具错误不会暴露凭据或远程响应的细节。插件不会新增任何数据库、缓存、模型提供方或遥测路径。

Dify 使用 gevent 协程（greenlet）对 Python 线程进行补丁。每个请求都在当前 hub 的原生线程池中构造、调用并关闭其 SDK 客户端，因此相互重叠的客户端不会在同一个原生线程上运行 asyncio 事件循环。凭据只在该请求内部使用。SDK 调用最多等待 30 秒；线程池准入、客户端初始化和关闭各有独立的等待时间，而不是整个调用共用一个截止时间。

Dify 从本地 `.difypkg` 安装该软件包。官方 CLI 会打包源代码、元数据、运行时依赖、图标、许可证、README 和隐私声明。其 ignore 文件会排除测试、环境、凭据和构建状态。CI 针对一个模拟的 SDK HTTP 传输层调用这些原生工具，通过本地回环 HTTP 检查相互重叠的原生调用，校验原生工具的 schema，并构建软件包。只有在显式提供私有 Loom URL 和令牌时，才会运行在线测试；平台上的执行情况与这些 SDK 测试分开记录。

<a id="source-entry-points"></a>

## 源码入口

- `integrations/dify/client.py`：限定范围的 SDK 调用和请求失败处理。
- `integrations/dify/provider/loom.py`：凭据校验。
- `integrations/dify/tools/`：原生工具的实现和 schema。
- `.github/workflows/dify-plugin.yml`：测试和打包。
