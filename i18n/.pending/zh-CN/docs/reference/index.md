<!-- i18n: source=docs/reference/index.md blob=fb131cfc15bed6b11b4ba0dd63862e173de7bcf3 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md), which is authoritative.

<a id="reference"></a>

# 参考

参考页面说明有哪些内容，以及每个选项的含义。设计背后的原因见[概念](../concepts/memory.md)和[架构](../architecture/system-overview.md)。操作步骤见[指南](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)和[运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)部分。

<a id="generated-from-the-code"></a>

## 由代码生成

这些页面（或页面中的表格）由定义它们的源码渲染而成。每个生成器都有 `--check` 模式，`make lint` 会运行它，因此与代码不一致的页面会导致 CI 失败。请修改源码后重新生成，不要手工编辑生成的表格。

| 页面                                | 列出的内容                                                                                                    | 源码                                                                                 | 生成器                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------- |
| [配置](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) | 按领域列出每个 `LOOM_*` 设置及其默认值和含义。                                                                | `server/settings/` 中字段上方的注释                                                  | `python scripts/codegen/settings_reference.py` |
| [操作](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)     | 分组列出每个操作，包含角色、类型、MCP `all` 列表和审计标志；另说明注册表的工作方式。                          | `server.operations.registry.ALL_OPERATIONS`                                          | `python scripts/codegen/docs_reference.py`     |
| [工作进程](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)            | 每个后台工作进程及其开关、默认值和运行周期；另说明 supervisor 如何运行它们。                                  | `server/workers/registry.py` (`_WORKERS`)                                            | `python scripts/codegen/docs_reference.py`     |
| [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)                     | 每个 `loom` 命令和子命令及其标志；另说明 CLI 的运行位置和常用命令。                                           | `loom` 解析器树 (`tests/unit/snapshots/cli_tree.json`)                               | `python scripts/codegen/docs_reference.py`     |
| [错误](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)                 | 服务器返回的每个错误码；另说明错误信封、重试规则和常见错误码的修复方法。                                      | `server/core/errors.py` 以及 `server/` 下每个 `"code"` 字面量                        | `python scripts/codegen/docs_reference.py`     |
| [状态](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)                 | 每个操作组中稳定操作的数量、每个实验性操作，以及每个默认加载为 false 的布尔设置。                             | 注册表中的操作 `maturity` 和 `server/settings/catalog.py` 的布尔字段                 | `python scripts/codegen/docs_reference.py`     |
| [模式](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)                 | 每张表和物化视图及其引擎、键、TTL 和索引；另说明列的约定。                                                    | `schemas/schema.sql`                                                                 | `python scripts/codegen/docs_reference.py`     |

在 `docs_reference.py` 生成的页面中，只有 `<!-- BEGIN GENERATED: … -->` 和 `<!-- END GENERATED: … -->` 标记之间的区域是生成的，其周围的文字为手工编写。SDK 包中的操作表（`sdk/python/loom/ops.py`、`sdk/typescript/src/generated/ops.ts`）也是生成的，由 `scripts/codegen/sdk_py_gen.py` 和 `sdk_ts_gen.py` 生成；参见[代码生成](../development/code-generation.md)。

<a id="written-by-hand"></a>

## 手写页面

| 页面                                          | 内容                                                                                                                                                     |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                     | 所有路由（ops、发现、健康检查、流、登录、OAuth、控制台、运维），网关顺序，请求头，未知字段，幂等性，分页，速率限制。 |
| [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)                               | `/mcp` 端点，认证与 OAuth，会话与身份，工具列表与工具集，结果，错误，action schema 导出。                       |
| [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)                 | 安装，构造，调用 ops，类型化结构，会话与工具，框架适配器，错误，重试。                                                         |
| [TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)         | 安装，客户端与选项，类型化 ops，会话与工具，分页，Strands 适配器，错误，重试。                                                     |
| [环境变量](https://github.com/ClickHouse/loom/blob/main/docs/reference/environment.md)     | 在设置模型之外读取的变量：提供方密钥，镜像，compose 文件，CLI，SDK 和运维脚本。                                    |
| [SQL 接口](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)               | 基于租户数据的只读 `sql_v1_*` 视图：列，兼容性规则，隐藏的内容，新鲜度，`loom sql-access`。                                |
| [提取器契约](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md) | 提取器服务所提供的 HTTP 契约的第 1 版：能力，提取，错误码，制品的字段与规则。                                |

手写页面只提及代码树中实际存在的内容，并链接到其所描述的源码。

<a id="which-page-answers"></a>

## 该看哪一页

- “这个 op 叫什么，谁可以调用它？” → [操作](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)。
- “这个设置有什么作用？” → [配置](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)。
- “这个错误码是什么意思，应该重试吗？” → [错误](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)。
- “这个 op 是稳定版还是实验性的，这个设置默认是否开启？” → [状态](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)。
- “用哪个请求头、哪个路由，如何分页？” → [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)。
- “为什么我的 MCP 客户端没有列出某个工具？” → [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)。
- “这个 worker 是否在运行，如何开启它？” → [后台任务](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)。
- “下次部署后我的客户端会出问题吗？” → [未知字段和已移除字段](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)以及[查看部署提供的内容](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)。
- “这张表里有什么？” → [数据库表结构](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)。
- “`loom <command>` 接受哪些参数？” → [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)。
