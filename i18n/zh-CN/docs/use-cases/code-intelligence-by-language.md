<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=0ed7353714a116300c522e3520e5b1986b56ca81 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# C++、Python、TypeScript、Go、Rust、Java 和基础设施代码的代码智能

Loom 的代码索引可以回答 coding agent 关于代码结构的问题，覆盖仓库中包含的各种语言：符号在哪里定义、谁调用了它，以及一次改动会影响哪些内容。需要先添加仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。每种语言都有快速层，使用 tree-sitter 语法或手写的扫描器解析；C/C++、Python、TypeScript 和 Go 还可以增加编译器层，按编译器的方式解析名称。完整的扩展名和边列表见[语言支持表](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)，编译器层的设置见[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)。

每个答案都会标明所用的层（编译器层为 `tier: "scip"`，快速层为 `"treesitter"`），`code.coverage` 会按语言和层报告命名空间中包含的内容。如果你还没有在某种语言上使用过 Loom，在信任该语言的 callers 或 impact 结果之前，请先查看它。

<a id="c-and-c"></a>

## C 和 C++

符号、调用、include、继承和重写来自快速层；编译器层（`scip-clang`）根据项目的构建来解析重载、模板和命名空间，因此对限定名（例如 `DB::IMergeTreeDataPart::getBytesOnDisk`）使用带 `uses_brief: true` 的 `code.symbol`，会返回唯一的定义、其调用点数量，以及主要调用点及其所在的函数，每一项都标记为 `tier: "scip"`。编译器层需要项目的 CMake 构建配置。

<a id="python"></a>

## Python

调用、导入、继承和重写关系来自快速层，其中也包括 notebook 的代码单元；编译器层（`scip-python`）负责跨模块解析名称。在 Loom 自身的代码仓库中，对 `writer_actor` 调用 `code.symbol`，会在编译器层返回它位于 `server/memory/app/provenance.py` 中的定义，并附带其签名和 docstring；加上 `uses_brief: true` 后还会返回它的调用位置。

<a id="typescript-and-javascript"></a>

## TypeScript 与 JavaScript

调用（包括 `new`）、导入、继承和重写来自快速层，Vue、Svelte 和 Astro 组件的 script 块也是如此；Angular 的 `.component.html` 模板会与其组件作为一个单元一起建立索引。编译器层（`scip-typescript`）通过项目的 `tsconfig.json`、`jsconfig.json` 或 `package.json` 解析名称。

<a id="go"></a>

## Go

调用、接口嵌入和结构化实现来自快速层，Go 字符串中的 SQL 会转换为表引用；编译器层（`scip-go`）可以跨包解析名称，包括 monorepo。

<a id="rust-and-java"></a>

## Rust 和 Java

Rust 支持调用、`use` 导入和 trait 实现；Java 支持调用，导入会绑定名称，并支持 Java 字符串中的 SQL。两者都只有快速层：在同一文件中绑定的调用置信度为 1.0，通过所在类绑定的为 0.8，通过全仓库名称匹配绑定的为 0.6，无法绑定的调用会被舍弃而不是猜测，并且 `code.impact` 会在文件级别补充已映射的测试和共同变更的伙伴文件。

<a id="sql-and-migrations"></a>

## SQL 与迁移

`.sql` 文件默认会被索引，可用于搜索和 grep。开启 `LOOM_CODE_SQL_STATEMENT_RECORDS` 后，每条语句都会成为一条带有表引用的记录，agent 因此可以查询哪些迁移和查询读取或写入了某张表。若要为已建立索引的命名空间开启该选项，需要对这些 SQL 文件执行一次 `loom code backfill --paths-file`（[开关](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)）。

<a id="infrastructure-as-code"></a>

## 基础设施即代码

Terraform 和 HCL、Kubernetes manifest、Helm chart 和 Go 模板、Jsonnet、CMake、Protocol Buffers 以及 shell 脚本，都会连同其中的引用、include 和调用一起被索引，因此 agent 可以从 Helm 的 `include` 跳转到对应的 `define`，也可以从 Terraform 的引用跳转到它所指向的对象。开启 `LOOM_CODE_HOST_ISLANDS` 后，Dockerfile、Makefile、Jenkinsfile 和 CI YAML 作业会成为符号；如果命名空间已经建立过索引，则需要先对这些文件执行一次回填。

<a id="start"></a>

## 开始

1. [为仓库建立索引](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)；当仓库包含编译器层级所需的构建文件时，这些层级会自动启动。
2. 在 `coding` 工具集上[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。
3. 提出一个结构类问题；检查 `code.coverage`，确认你所依赖的语言已被覆盖。

<a id="related"></a>

## 相关内容

- [面向编码 agent 的代码索引](code-intelligence.md)
- [代码操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [添加语言支持](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
