<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=414b69b208ddf6610590f99678d2dc8e9bff8780 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# 面向 C++、Python、TypeScript、Go、Rust、Java 和基础设施代码的代码智能

Loom 的代码索引用仓库所含的各种语言，回答编码 agent 提出的结构问题：符号在哪里定义、谁调用了它，以及一次改动会影响哪些内容。需要先添加仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。每种语言都有快速层级，使用 tree-sitter 语法或手写的扫描器解析；C/C++、Python、TypeScript 和 Go 还可以增加编译器层级，按编译器的方式解析名称。完整的扩展名和边列表见[语言支持表](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)，编译器层级的设置见[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)。

每个回答都会标明其背后的层级（编译器层级为 `tier: "scip"`，快速层级为 `"treesitter"`），`code.coverage` 会按语言和层级报告命名空间中的内容。对于你尚未在 Loom 上使用过的语言，在信任 `code.callers` 或 `code.impact` 的回答之前，请先查看它。

<a id="c-and-c"></a>

## C 和 C++

符号、调用、include、继承和重写来自快速层级；编译器层级（`scip-clang`）根据项目的构建解析重载、模板和命名空间，因此对 `DB::IMergeTreeDataPart::getBytesOnDisk` 这样的限定名调用带 `uses_brief: true` 的 `code.symbol`，会返回唯一的定义、调用点数量，以及最主要的调用点及其所在函数，每一项都标记为 `tier: "scip"`。编译器层级需要项目的 CMake 构建配置。

<a id="python"></a>

## Python

调用、导入、继承和重写来自快速层级，其中包括 notebook 的代码单元；编译器层级（`scip-python`）跨模块解析名称。在 Loom 自己的仓库中，对 `writer_actor` 调用 `code.symbol`，会在编译器层级返回它位于 `server/memory/app/provenance.py` 中的定义，以及签名和 docstring，加上 `uses_brief: true` 还会返回它的调用点。

<a id="typescript-and-javascript"></a>

## TypeScript 和 JavaScript

调用（包括 `new`）、导入、继承和重写来自快速层级，Vue、Svelte 和 Astro 组件的脚本块也是如此；Angular 的 `.component.html` 模板与其组件作为一个单元建立索引。编译器层级（`scip-typescript`）通过项目的 `tsconfig.json`、`jsconfig.json` 或 `package.json` 解析名称。

<a id="go"></a>

## Go

调用、接口嵌入和结构化实现来自快速层级，Go 字符串中的 SQL 会变成表引用；编译器层级（`scip-go`）跨包解析名称，包括 monorepo。

<a id="rust-and-java"></a>

## Rust 和 Java

Rust 支持调用、`use` 导入和 trait 实现；Java 支持调用（由导入绑定名称）以及 Java 字符串中的 SQL。两者都只有快速层级：在同一文件内绑定的调用置信度为 1.0，通过所在类绑定的为 0.8，通过全仓库名称匹配绑定的为 0.6，无法绑定的调用会被舍弃而不是猜测，`code.impact` 还会在文件级别补充映射到的测试和共同变更的伙伴文件。

<a id="sql-and-migrations"></a>

## SQL 和迁移

默认情况下，`.sql` 文件会被建立索引，用于搜索和 grep。开启 `LOOM_CODE_SQL_STATEMENT_RECORDS` 后，每条语句都会成为带有表引用的记录，这样 agent 就可以询问哪些迁移和查询读取或写入了某张表。要对已建立索引的命名空间开启它，需要对这些 SQL 文件执行 `loom code backfill --paths-file`（[开关](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)）。

<a id="infrastructure-as-code"></a>

## 基础设施即代码

Terraform 和 HCL、Kubernetes 清单、Helm chart 和 Go 模板、Jsonnet、CMake、Protocol Buffers 以及 shell 脚本，都会连同其引用、include 和调用一起建立索引，因此 agent 可以从 Helm 的 `include` 追到它的 `define`，或从 Terraform 引用追到它所指向的对象。开启 `LOOM_CODE_HOST_ISLANDS` 后，Dockerfile、Makefile、Jenkinsfile 和 CI YAML 作业会变成符号；如果命名空间已建立索引，需要先对这些文件执行 backfill。

<a id="start"></a>

## 开始使用

1. [为仓库建立索引](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)。它会运行哪些编译器层级，见[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)。
2. 在 `coding` 工具集上[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。
3. 提出结构问题；对你所依赖的语言，查看 `code.coverage`。

<a id="related"></a>

## 相关内容

- [面向编码 agent 的代码索引](code-intelligence.md)
- [代码操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [添加语言支持](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
