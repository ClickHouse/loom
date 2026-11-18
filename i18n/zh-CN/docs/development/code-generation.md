<!-- i18n: source=docs/development/code-generation.md blob=3f6e27200eccce850139d9114282249d0644e35f model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md), which is authoritative.

<a id="code-generation"></a>

# 代码生成

仓库中有若干文件是由代码派生出来的：SDK 的操作表和 wire 类型、参考页面、Codex 插件的副本、op reach manifest、schema dump。这些文件都已提交到仓库，因此读者无需运行任何命令就能看到它们。下面标注的生成器带有 `--check` 模式，当已提交的文件与生成器将要写出的内容不一致时，该模式以退出码 1 退出。`make lint` 会运行其中每一项检查，CI 则在 `lint` 和 `sdk-typescript` 任务以及单元测试套件中运行这些检查，因此文件过期时会失败，并在失败信息中给出需要运行的命令。

**为什么要提交生成的文件。** 只在需要时才构建的生成文件在评审中是不可见的：修改某个操作的输入模型会改变 SDK 类型，却没有任何人会阅读的 diff。提交这些文件，可以把变更的影响放在变更旁边，而 `--check` 检查则防止两者逐渐偏离。代价是，修改源头的那次变更中需要多一个重新生成的步骤。

<a id="the-generators"></a>

## 生成器

所有生成器都位于 [`scripts/codegen/`](https://github.com/ClickHouse/loom/blob/main/scripts/codegen)，并共用 `scripts/codegen/common.py`（其中包含仓库根目录、SDK 表为每个 op 携带的信息、ruff 格式化、原子写入，以及写入或检查协议）。

| 脚本                        | 写入                                                                                                                                                                                                                   | 来源                                                                                                                                                                                                  | `make lint` 中的 `--check` |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `sdk_py_gen.py`             | `sdk/python/loom/ops.py`：`OpName`、`OPS` 表（幂等性、角色、成熟度、延迟预算），以及每个输入和输出模型各一个 `TypedDict`                                                                                               | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | 是                         |
| `sdk_py_client_gen.py`      | `sdk/python/loom/client.py`：`OpsClient`，每个 op 一个方法（`agent.client.tracker.source_add`），使用 `ops.py` 中的名称进行类型标注                                                                                    | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | 是                         |
| `sdk_py_admin_gen.py`       | `sdk/python/loom/admin_routes.py`：`AdminRoutes`，每个 `/v1/admin/*` 路由一个方法（`agent.deployment.routes.settings_set`）                                                                                            | `/v1/admin/*` 路由器：每个路由的方法、路径、访问限制和文档字符串                                                                                                                                      | 是                         |
| `sdk_ts_gen.py`             | `sdk/typescript/src/generated/ops.ts`：每个模型一个 interface、`OPS` 表、`OpMap` 类型                                                                                                                                  | `server.operations.registry.ALL_OPERATIONS`（模型的 JSON Schema）                                                                                                                                     | 是                         |
| `agent_plugin_sync.py`      | Codex 插件中的钩子脚本副本（`hooks/*.mjs`）、共享库（`lib/*.mjs`）、skills 和列表图标（`assets/`），与源文件逐字节一致，并保留可执行位                                                                                  | `integrations/claude-code-plugin/`                                                                                                                                                                    | 是                         |
| `settings_reference.py`     | 生成的[配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)                                                                                                                                                 | `server/settings/catalog.py`：每个字段的环境变量、类型、默认值，以及其上方的注释                                                                                                                      | 是                         |
| `settings_schema.py`        | `schemas/loom-settings.schema.json`，即 YAML 设置文件的 JSON Schema                                                                                                                                                    | 设置字段的类型、取值范围、默认值和注释，不含密钥                                                                                                                                                      | 是                         |
| `settings_example.py`       | `loom.example.yaml`，设置文件模板：除密钥外的每个设置，按区域分组，以默认值注释掉并附在其说明之下                                                                                                                      | 设置目录中的字段、默认值、注释和 live 标记                                                                                                                                                            | 是                         |
| `docs_reference.py`         | `docs/reference/` 中的生成区域：`operations`、`workers`、`cli`、`errors`、`schema`、`status_ops`、`status_switches`、`admin_routes`（[操作员路由表](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#operator-routes)） | 注册表、`server/workers/registry.py`（`_WORKERS`）、`tests/unit/snapshots/cli_tree.json`、`server/core/errors.py`、`schemas/schema.sql`、`server/settings/catalog.py`、`/v1/admin/*` 路由器            | 是                         |
| `op_reach_census.py`        | `server/operations/reach_manifest.py`（`--write-manifest`）；否则输出一张统计表                                                                                                                                        | 注册表和 MCP 工具集                                                                                                                                                                                   | 是                         |
| `wire_changelog.py`         | 不写入任何文件；输出两个修订版本之间对客户端的变更                                                                                                                                                                     | 在每个修订版本上构建的能力目录                                                                                                                                                                        | 否                         |
| `gen_py_library_methods.py` | `server/code/domain/extract/py_library_methods.py`                                                                                                                                                                     | 正在运行的解释器的标准库                                                                                                                                                                              | 否（依赖解释器）           |
| `push_langfuse_prompts.py`  | `server/observability/langfuse_prompts.lock.json`（`--write`）                                                                                                                                                          | 服务器中的提示词常量；并将它们推送到 Langfuse                                                                                                                                                         | 否                         |

配置参考由注释渲染而成，因此设置上方的注释就是它的文档：请修改注释，而不是页面。`docs/reference/` 下的页面是围绕 `<!-- BEGIN GENERATED: <name> -->` … `<!-- END GENERATED: <name> -->` 区域手写的文字；只有每对标记之间的文本会被重写，因此页面可以对其中的表格加以说明。

<a id="sdk_py_gen-and-sdk_ts_gen"></a>

### sdk_py_gen 和 sdk_ts_gen

Python SDK 仍然是字典进、字典出；生成的文件只是补充了名称，这样编辑器就能补全 `agent.call("memory.search", …)`，类型检查器也能看到字段名，而无需手工维护模型层。这些字典是 `total=False`，服务器要求的字段使用 `Required[...]`。TypeScript 生成器把每个模型的 JSON Schema 转换为 interface（枚举和常量转为字面量联合类型，`anyOf` 转为联合类型，共享的 `$defs` 只提升一次），因此 `client.call("memory.search", input)` 端到端都有类型。修改任何操作的注册或其模型之后，请运行它们：

```bash
python scripts/codegen/sdk_py_gen.py
python scripts/codegen/sdk_py_client_gen.py
python scripts/codegen/sdk_ts_gen.py
```

两个 SDK 把每个 op 都暴露为同一位置的方法：分组是 op 名称的第一段，方法是其余部分并以 `.` 连接，Python 中为 snake_case（`agent.client.tracker.source_add`），TypeScript 中为 camelCase（`loom.tracker.sourceAdd`）。当两张 op 表或任一 op 的两个方法名不一致时，`tests/unit/test_sdk_parity.py` 会失败。

<a id="agent_plugin_sync"></a>

### 插件同步（agent_plugin_sync）

两个编码 agent 插件只在清单和钩子接线（`hooks/hooks.json`）上有所不同。Codex 插件携带的是副本，而不是 `../` 引用，这样每个插件都是一个自包含的目录，应用市场可以单独检出。向 Claude Code 插件添加钩子脚本或 skill 之后，请运行同步并提交这些副本；如果希望 Codex 运行新的钩子，请手动把它接入 Codex 的 `hooks.json`。

<a id="op_reach_census"></a>

### 触达统计（op_reach_census）

每个操作都要声明成熟度，并且必须有一条除原始 HTTP `POST` 之外的触达路径，否则必须声明为 `experimental`。触达类型（`server/operations/reach.py`，`REACH_KINDS`）包括：MCP 列表（`mcp_core`、`mcp_atomic`、`mcp_all`，由 `server/operations/toolsets.py` 实时推导）、SDK 方法（`sdk_method`）、随附的示例或操作员脚本（`sdk_example`）、插件钩子或集成（`plugin_hook`），以及控制台调用点（`dashboard`）。后四种来自对源码树的扫描，而已部署的服务器并不携带源码树，因此它们被提交为 `reach_manifest.STATIC_REACH`。`--check` 会强制执行这条规则，要求每个没有控制台调用点的 op 都列在 `server/operations/dashboard.py` 中（并附上原因），并要求已提交的清单与源码树一致；`--write-manifest` 会刷新清单。`--call-counts --days 30` 会从 `loom.agent_events` 中加入每个 op 的调用次数（这是唯一会连接 ClickHouse 的模式），`--owners` 会给出引入每个 experimental op 声明的提交。

<a id="wire-changelog"></a>

### 客户端/服务器协议（wire）变更日志

```bash
python scripts/codegen/wire_changelog.py <rev_a> <rev_b>           # markdown
python scripts/codegen/wire_changelog.py --since-hours 24 [rev_b]   # rev_a = rev_b's ancestor N hours back
python scripts/codegen/wire_changelog.py <rev_a> <rev_b> --json
python scripts/codegen/wire_changelog.py --snapshot <rev> > snap.json
python scripts/codegen/wire_changelog.py --diff a.json b.json
```

它用 `git archive` 展开每个修订版本，在子解释器中导入其注册表，并比较两个修订版本之间的操作、请求和响应字段、角色、预算以及 MCP 层级，输出新增或移除的操作以及请求和响应字段，另外还有角色、预算和 MCP 层级的变化。其输出会写入发布说明（[发布流程](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)）；发布说明中“对客户端的变更”一节是由它生成的，不是手写的。

<a id="gen_py_library_methods"></a>

### Python 标准库方法生成（gen_py_library_methods）

Python 提取器使用的标准库方法名集合被固定在源码树中，这样升级解释器就不会改变调用边。重新生成它会修改一个提取器模块，从而改变代码索引的版本戳，并对每个已索引的命名空间重新分块；请审查差异，并且只在确有需要时才重新生成。

<a id="push_langfuse_prompts"></a>

### 推送 Langfuse 提示词（push_langfuse_prompts）

服务器通过在已提交的锁文件中查找提示词的内容哈希，把一次 LLM 生成关联到某个 Langfuse 提示词版本，运行时不进行任何调用。不带 `--write` 时，脚本只针对锁文件离线制定计划；`--write` 会推送已变更的提示词（需要 `LOOM_LANGFUSE_URL` 和 `LOOM_LANGFUSE_ADMIN_TOKEN`）并重写锁文件。只要锁文件缺少某个已注册的提示词，单元测试就会失败。

<a id="the-schema-dump"></a>

## Schema 导出

`schemas/schema.sql` 由 `scripts/ops/dump_schema.py` 生成，生成时使用的 ClickHouse 已应用全部迁移：

```bash
make schema-dump    # python -m scripts.ops.dump_schema
make schema-check   # --check: exit 1 with the head of the diff; writes nothing
```

相关内容见[迁移](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#schema-dump-and-check)，因为该文件会随每次迁移变更而更新，并且必须基于 CI 所固定的 ClickHouse 版本导出。

<a id="snapshots-that-are-tests-not-generators"></a>

## 作为测试而非生成器的快照

CLI 接口（`tests/unit/snapshots/cli_tree.json`、`cli_help.json`）和 worker 注册表（`tests/unit/snapshots/worker_registry.json`）由单元测试固定，并通过测试运行时的环境变量重新生成（[测试](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md#ci-gates)）。CLI 参考文档由 `cli_tree.json` 渲染而成，因此重新生成 CLI 快照后，还需要运行 `docs_reference.py`。

<a id="before-you-push"></a>

## 推送之前

```bash
make lint            # every --check above, plus ruff, the gates and Biome's dashboard accessibility rules
```

如果检查失败，错误信息会指出需要运行的脚本；运行该脚本，检查生成的 diff，并将重新生成的文件与其源文件放在同一次变更中提交。

<a id="related"></a>

## 相关内容

- [添加操作](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-an-operation.md)：op、handler 和 SDK 表
- [文档风格](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md#generated-pages)：哪些页面是生成的
- [测试](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)
