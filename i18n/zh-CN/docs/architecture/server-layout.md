<!-- i18n: source=docs/architecture/server-layout.md blob=a488f9e3096ee9ff0f09303b9a382a9ce950bac7 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md), which is authoritative.

<a id="server-layout"></a>

# 服务端布局

`server/` 按领域组织。每个产品平面（plane）对应一个包，包内包含其 wire 声明、处理器、纯逻辑、ClickHouse 访问和后台循环，并由一项测试固定导入的允许方向。本页可作为放置新模块时的参考，也可用于解读契约测试报出的导入错误。

<a id="packages"></a>

## 包

| 包                            | 职责                                                                                                                                                                                                        | 可导入                                                                                                      |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/main.py`              | 应用装配：lifespan、路由、workers、传输层。与 `server/boot.py`、`server/health.py`、`server/wiring.py`、`server/worker_main.py` 和 `server/__init__.py` 一起，是仅有的根模块                              | 任意                                                                                                        |
| `server/boot.py`              | lifespan 的启动和关闭步骤：串行化的启动迁移、leader 的 tracker 命名空间行、吊销快照和密钥环、supervisor 租约循环、写入排空                                                                                 | 任意                                                                                                        |
| `server/health.py`            | `GET /v1/health`：多层级健康检查应答（ClickHouse、providers、启动快照、校准、速率限制器，按需包含 workers 和可观测性）                                                                                     | 任意                                                                                                        |
| `server/wiring.py`            | `wire()`：为每个平台接缝提供其领域部分（认证网关的命名空间目录、状态指标的事实数据）；由 `main.py`、CLI 和测试套件运行                                                                                     | 任意                                                                                                        |
| `server/worker_main.py`       | workers 子进程的组合根（`python -m server.worker_main`）：supervisor 及其 workers 所用的部分，不含对外服务的 lifespan                                                                                      | 任意                                                                                                        |
| `server/core/`                | 横切基础设施：ClickHouse 客户端（`ch`）、`errors`、预算、日期时间、哈希、命名、游标、幂等、速率限制、向量存储策略、`schema/`（迁移和 DDL 工具）                                                           | `core`、`settings`                                                                                          |
| `server/settings/`            | 所有设置都在 `catalog.py` 中声明，每个领域一个分区，组合成 `Settings`                                                                                                                                     | `core`、`settings`                                                                                          |
| `server/operations/`          | 注册表核心：`Operation` 和 `Role`（`base.py`）、扁平化注册表（`registry.py`：`ALL_OPERATIONS`、`OPERATIONS_BY_NAME`）、`toolsets.py`、读取投影、覆盖范围普查                                               | `core`、平台包、每个领域的 `api`                                                                            |
| `server/transport/`           | HTTP 和 MCP：准入、请求绑定、列表、primer                                                                                                                                                                  | `core`、平台包；领域的 `api`、`app` 和 `domain`（绝不导入其 `store` 或 `workers`）；不可导入 `cli`          |
| `server/cli/`                 | `loom` 命令，一个外层适配器                                                                                                                                                                                | 任意                                                                                                        |
| `server/workers/`             | worker 注册表（`registry.py`）、supervisor、pass 上下文、状态及其存储、首轮调度、成本事件。worker 循环位于各自的领域中                                                                                     | `core`、平台包；领域的 `workers`（注册表中的模块字符串）                                                    |
| `server/observability/`       | 账本、事件、审计行、指标、追踪导出                                                                                                                                                                         | `core`、平台包                                                                                              |
| `server/auth/`                | 令牌的签发与验证、角色和命名空间网关、每请求身份                                                                                                                                                           | `core`、平台包                                                                                              |
| `server/embedding/`、`llm/`   | 可插拔的 embedder 和 LLM providers                                                                                                                                                                         | `core`、平台包                                                                                              |
| `server/clients/`、`storage/` | 出站 HTTP（共享连接池、GitHub）；保留期子句                                                                                                                                                                | `core`、平台包                                                                                              |
| `server/memory/`              | 记忆平面：`memory.*`、搜索、ask、提取、事实、流程、画像、工作记忆、上下文包、冲突、黑名单、`loom_search`                                                                                                   | 参见[层次](#layers)                                                                                         |
| `server/code/`                | 代码平面：`code.*`、索引（`app/index/`、`domain/index/`、`store/index/`）、其解析器（`domain/extract/`）、摄取和编译器层级的 workers                                                                       | 参见[层次](#layers)                                                                                         |
| `server/tracker/`             | Tracker 命名空间：`tracker.*`、重复检查、实时来源读取器及其客户端、loom 自身的 tracker 表                                                                                                                  | 参见[层次](#layers)                                                                                         |
| `server/knowledge/`           | 知识文档和分块、对话、仓库文档                                                                                                                                                                             | 参见[层次](#layers)                                                                                         |
| `server/erasure/`             | 擦除 saga、其普查、联合后端、恢复 worker                                                                                                                                                                   | 参见[层次](#layers)                                                                                         |
| `server/tasks/`               | 任务收件箱、运行账本、其唤醒和 webhook 循环                                                                                                                                                                | 参见[层次](#layers)                                                                                         |
| `server/identity/`            | 命名空间、授权、历史策略、读取范围、受限访问、控制台页面和调用方 API（`/v1/me/*`）                                                                                                                         | 参见[层次](#layers)                                                                                         |
| `server/insights/`            | 运维界面：`ops.*`、`cost.*`、`agents.*`、`audit.list`、`ui.vitals`、`mentor.brief`、`/v1/admin/*` 路由                                                                                                     | 参见[层次](#layers)                                                                                         |
| `server/assistant/`           | 助手：其 LibreChat 服务用户和 agent 定义，在启动时设置                                                                                                                                                     | 参见[层次](#layers)                                                                                         |

平台包指 `core` 以及上表中其他非领域包：`settings`、`operations`、`transport`、`cli`、`workers`、`observability`、`auth`、`embedding`、`llm`、`clients` 和 `storage`。除 `core` 和 `settings` 外，每个平台包都可以导入任何其他平台包，除表中所列情形外，没有平台包会导入领域包。

<a id="layers"></a>

## 层级

一个领域包最多包含五个子包。模块放在其主要职责所在的子包中；较小的领域子包更少。

| 层级       | 内容                                                                                                      | 可导入                                                                                                                                                                  |
| ---------- | --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api/`     | 对外接口：Pydantic 输入和输出模型（`models*.py`），以及该领域各 op 组的 `Operation` 声明（`ops*.py`）       | `core`、`operations`、`settings`；任意领域的 `domain` 和 `api`；`Operation` 所绑定的处理函数所在的本领域 `app`                                                          |
| `app/`     | 处理函数与编排：一个 op 或一次 worker 执行过程按顺序做什么，包括 I/O                                      | 本领域的 `api`、`app`、`domain`、`store`；其他领域的 `app`、`domain` 和接口模型（`api/models*.py`）；`core` 以及除 `transport` 和 `cli` 之外的所有平台包                  |
| `domain/`  | 纯逻辑、类型化的值和策略：不涉及 ClickHouse，不涉及网络                                                   | `core`（不含其 I/O 模块：`ch`、`schema`、`idempotency`、`ratelimit_shared`、`namespace_write_marks`）、`settings`、任意领域的 `domain`                                   |
| `store/`   | ClickHouse 仓库和行构建器：该领域对其表执行的每一条语句                                                   | `core`、`settings`、`observability`、`storage`、本领域的 `store`、任意领域的 `domain`                                                                                   |
| `workers/` | 后台循环，每个注册表行对应一个模块                                                                        | 与 `app` 相同，只是用 `workers` 取代 `api`；其他领域的 `app`、`domain` 和接口模型                                                                                       |

有一个 `domain/` 模块会启动进程：`server/code/domain/extract/bounded_parse.py`。`domain/` 中的每个 tree-sitter 解析器都会调用它，它先在受资源限制的本地子进程中解析每个源文件。

有两条依赖边被刻意排除。`store/` 从不调用 `app/`：仓库接收值并返回行，因此可以用假的 store 来测试 app。另外，任何领域都不读取其他领域的 `store/`：它调用该领域的 `app/` 或读取其 `domain/` 类型，这样一张表的所有者就是其语句的唯一写入方。要调用其他领域的处理函数，`app/` 和 `workers/` 可以导入该领域的接口模型（`api/models*.py`），但绝不能导入它的 `api/ops*.py`。`app/` 导入接口模型，而不导入 `ops` 模块，这样 `Operation` 声明就可以绑定处理函数而不产生导入循环。

<a id="the-import-contract"></a>

## 导入约定

`tests/unit/test_package_contracts.py` 使用 `ast` 解析 `server/` 下的每个模块，并按两端所属的包和层级对每条导入进行分类，包括顶层导入、嵌套在 `if`/`try` 下的导入、函数内部的导入，以及 `TYPE_CHECKING` 下的导入。上述规则不允许的导入会使测试失败，并给出导入方和被导入的模块。

目前违反该约定的导入列在 `tests/unit/package_contract_allowlist.txt` 中，按模块路径以 `importer -> imported` 的形式记录。这份列表只会缩减：如果某个已允许的导入对不再出现，测试同样会失败，因此修复一条导入就意味着删除对应的那一行。

旁边还有第二项检查：只有 `server/transport/`、`server/cli/` 和 `server/main.py` 可以导入 `server.transport`（`tests/unit/test_transport_layering.py`）。任何领域模块都不得导入 `server.cli`。

<a id="adding-a-module"></a>

## 添加模块

1. 选择该模块所服务的表、op 组或平面所属的领域。服务于所有平面的模块（如 ClickHouse 辅助函数、哈希规则）放在 `core/`；服务于两个领域的模块，放在它所读取的行所属的领域，另一个领域调用它。
2. 按模块的职责选择层级，而不是按调用方选择：涉及 I/O 或 await 某个对象的 `async def` 放在 `app/` 或 `store/`；仅依赖参数的函数放在 `domain/`。
3. 在包内按模块所存放的内容命名，不要重复路径：用 `server/erasure/app/saga.py`，而不是 `server/erasure/app/erasure_saga.py`。
4. 包的 `__init__.py` 只保留一行 docstring，说明该包负责什么。只有当它是该包的公开接口时，才重新导出名称（`server/memory/store/`、`server/code/store/index/`）。
5. 后台循环是 `<domain>/workers/` 中的一个模块，外加 `server/workers/registry.py` 中的一行，写明其完整模块路径。
6. mock 或 monkeypatch 的目标、注册表中的行以及覆盖率下限，都通过路径来指定模块；请使用模块的真实路径，不要通过重新导出来指定。
7. 通过 `server.core.paths` 查找服务器自带的文件（迁移、`schema.sql`、前端构建产物），不要通过数 `__file__` 的父目录层数来查找（`tests/unit/test_repo_paths.py`）。

<a id="extractor-modules"></a>

## Extractor 模块

`extractor_stamp.STAGE_MODULES` 中列出的文件是代码索引的版本戳。该版本戳对每个文件的名称和代码 token 做哈希，其中 import 按其绑定的名称计入，而不是按所来自的包计入。版本戳一旦变化，每个代码命名空间的索引都会重新生成。这些文件与 `extractor_stamp.py` 一起位于 `server/code/domain/extract/` 中，由后者从自身所在目录读取它们，这些文件保持原有的文件名。各语言通道模块位于它们旁边。把其中一个文件在不同包之间移动，不会改变版本戳；重命名、重新格式化（`ruff format` 会排除它们）或修改其代码，则会改变版本戳。

<a id="related"></a>

## 相关内容

- [系统概览](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) — 控制平面的组件和请求流程。
- [代码索引架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/code-index-architecture.md) — extractor 戳记与重新索引。
- [请求路径](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md#request-path) — 请求经过传输层、注册表和处理程序的路径。
