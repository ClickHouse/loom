<!-- i18n: source=docs/features/integrations/grep-shim.md blob=1ce5e2990b99fc9c2e3b50a80f6b06f33fa415f0 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md), which is authoritative.

<a id="the-grep-shim"></a>

# grep shim

grep shim 是一个 `grep`/`rg` 包装器，它会根据代码索引，把每个命中所在的外层函数追加到匹配行的末尾。它需要 Node 18 或更高版本、一个 viewer 令牌，以及一个已收录在[代码索引](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)中的仓库。它适用于只使用 shell grep 的 agent，或运行环境中 hook 不生效的情况（`claude --bare` 会跳过 [Claude Code 插件](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)的代码分流）。agent、其提示词和运行框架都无需改动。

```text
$ grep -rn "getSettingsRef().max_threads" src/Processors
src/Processors/QueryPlan/ReadFromMergeTree.cpp:1466:    ... max_threads ...   ⟵ DB::ReadFromMergeTree::selectRangesToRead
src/Processors/Executors/PipelineExecutor.cpp:298:      ... max_threads ...   ⟵ DB::PipelineExecutor::initializeExecution
loom: 2/2 hits scoped to their enclosing function via code.enclosing, index at c7a9fe7c9f, 180 ms
```

<a id="install"></a>

## 安装

```bash
export PATH=/path/to/loom/integrations/grep-shim/bin:$PATH
export LOOM_BASE_URL=https://loom.example.com   # or ~/.loom/config.json {url, token}
export LOOM_API_KEY=<viewer token>              # or ~/.loom/token
export LOOM_CODE_NAMESPACE=code-myrepo
export LOOM_AGENT=pr-analyst                    # optional: names the caller in the ledger
```

`which grep` 必须指向该 shim。请在该命名空间所索引的检出目录中运行 agent：路径会相对于 `git rev-parse --show-toplevel` 转换。分支不一致时，表现为命中无法解析，而不是解析出错误结果。

shim 会运行 `PATH` 中第一个不是它自身的 `grep` 或 `rg`（可用 `LOOM_GREP_REAL`、`LOOM_RG_REAL` 强制指定路径）。

<a id="guarantees"></a>

## 保证

- **真实工具输出的每个字节都会按原顺序输出。** 唯一的改动是：对于索引能确定其外层函数的匹配行，追加一个可选后缀 `   ⟵ <qualified name>`，并在 stderr 输出一行摘要。
- **退出码保持不变**（grep：0 表示有命中，1 表示无命中，2 表示出错）。
- **只对两种输出形式添加注解：** `path:line:text`（多个操作数、对目录使用 `-r`、`-H`），以及只有一个文件操作数时的 `line:text`（`grep -n pattern file.cpp`，这是 agent 了解文件后通常会采用的形式；路径就是该操作数，因此这里使用 `-h` 也没有问题）。其他情况一律原样透传：没有 `-n`，或使用 `-l`、`-L`、`-c`、`-o`、`-z`、`-Z`、`-b`、`-q`、`--color=always`（rg：`-N`、`--json`、`--files`、`-0`），读取 stdin，或多个文件搭配 `-h`。
- **严格的延迟上限。** `LOOM_GREP_SHIM_TIMEOUT_MS`（默认 2500）。出现任何错误、超时、缺少令牌，或输出超过 8 MB（`LOOM_GREP_SHIM_MAX_BYTES`）时，都会输出原始结果。
- **服务端负载有界。** 每 200 个不同的命中发起一次 `code.enclosing` 调用，最多 5 个并行；最多对 1,000 个命中添加注解，其余的不加注解直接输出。

<a id="the-dependency-graph-block"></a>

## 依赖图块

`LOOM_GREP_SHIM_GRAPH=on`（默认关闭）会在真实输出之后追加一个块：`[Related from loom dependency graph]`，随后每个外层函数一行，包含其所属模块、最多 `LOOM_GREP_SHIM_GRAPH_K`（5）个调用者、被调用者和测试，并在依赖图不完整的地方给出 `(lower bound: …)` 提示。从标题行开始截掉后面的内容，即可还原真实的原始字节。该块使用相同的 `code.enclosing` 调用、超时和失败时放行的规则；当函数数量超过 `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`（20），或输出以流式方式输出（超过 8 MB）时，该块会被丢弃。

<a id="switches"></a>

## 开关

| 变量                                    | 默认值                                 | 作用                                                                                    |
| --------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_GREP_SHIM`                        | on                                     | `off` 使 shim 变为纯透传                                                                |
| `LOOM_GREP_SHIM_QUIET`                  | 未设置                                 | 设置后不再输出 stderr 摘要行                                                            |
| `LOOM_GREP_SHIM_TIMEOUT_MS`             | 2500                                   | 延迟上限                                                                                |
| `LOOM_GREP_SHIM_MAX_BYTES`              | 8 MB                                   | 超过该大小的输出将原样透传                                                              |
| `LOOM_GREP_SHIM_GRAPH`                  | off                                    | `on` 会追加依赖图块                                                                     |
| `LOOM_GREP_SHIM_GRAPH_K`                | 5                                      | 每个函数的相邻节点数（1–20）                                                            |
| `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`      | 20                                     | 函数数量超过该值时丢弃该块                                                              |
| `LOOM_CODE_NAMESPACE`, `LOOM_AGENT`     |                                        | 代码命名空间；调用方的 agent 名称（默认 `grep-shim`）                                   |
| `LOOM_CONFIG_FILE`, `LOOM_API_KEY_FILE` | `~/.loom/config.json`, `~/.loom/token` | 环境变量未设置时，从这里读取 URL、命名空间和令牌                                        |

<a id="server-side-codeenclosing"></a>

## 服务端：`code.enclosing`

shim 只是围绕一个操作的轻量客户端。`code.enclosing`（`POST /v1/code.enclosing`，viewer）接受 `locations: ["src/F.cpp:12", …]`（最多 200 个），并按输入顺序为每个位置返回一个结果；如果该行位于文件作用域，或路径未被索引，则 `symbol == ""`。每个已解析的行都带有 `symbol_id`。

- `include_body: true`（配合 `token_budget`，默认 4000）会把每个外层函数的完整函数体放到对应行上，每个不同的函数只获取一次，并按首次出现的顺序填充，直到预算用完；其余的行带有可直接粘贴的 `next`（`code.symbol(symbol_id=…)`）。shim 不启用此选项。
- `neighbours: true`（配合 `neighbour_k`，默认 5）会返回依赖图块所用的数据：每个不同的外层函数一项（最多 20 项；解析出的函数更多时会设置 `anchors_capped`），包含 `callers`、`callees`（优先使用编译器层级；仓库之外的符号以及置信度低于 0.8 的解析器猜测会被丢弃）、`tests`、`module`，以及 `epistemic` / `causes` 信封。

`code.symbol` 也接受用于单个位置的 `name: "src/F.cpp:12"`，Python SDK 则提供 `agent.code.enclosing([...], include_body=True)`。参见[代码索引操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)。

<a id="limits"></a>

## 限制

- **仅限已合并的代码。** 索引只了解它所索引的那个分支；位于未提交修改中的命中，会按文件已索引的版本解析，或者根本无法解析。
- **尽力而为。** 任何意外情况都会输出原始结果；某一行没有注解，并不意味着 grep 失败了。
- **搜索方式不变。** agent 仍然是在用 grep。能够调用 MCP 工具的 agent 应直接使用 `code.search`、`code.callers` 和 `code.impact`。

<a id="related"></a>

## 相关内容

- [代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)。
- [Claude Code 插件](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)：基于 hook 的代码分流，适用于确实会运行 hook 的 agent。
- [集成概览](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)。
