<!-- i18n: source=docs/use-cases/code-intelligence.md blob=9f10f6e9bc93b39e97d3aee93cedeeb7260a7297 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 面向编码 agent 的代码索引

Loom 为编码 agent 提供一份通过 MCP 提供服务的、共享的 git 仓库索引，agent 提出结构性问题，得到的是能回答问题的那几行代码，而不是整份文件。需要先添加一个仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。索引如何构建并保持最新，见[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)；各语言支持的能力，见[按语言划分的代码索引](code-intelligence-by-language.md)。

<a id="what-you-get"></a>

## 你能得到什么

- **定义、调用方与影响范围。** `code.symbol`、`code.callers` 和 `code.impact` 以文件和行号引用的形式定位代码。`code.index_status` 报告各层级已索引的提交。[代码操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **按语义搜索与按精确字符串搜索。** `code.search` 查找某个机制所在的位置；`code.grep` 查找精确的消息或标识符。
- **历史与拉取请求。** 带评审评论的已合并 PR 历史、提交 PR 前的重复检查，以及回归问题的 bisect 候选。这些功能需要开启 PR 历史（`LOOM_CODE_PR_HISTORY_ENABLED`）；评审评论和未合并的 PR 还需要 GitHub 令牌。每项功能的具体要求见[历史、PR 与发布](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)。
- **关于代码的经验。** `code.symbol` 返回与某个定义关联的记忆；`code.search`、`code.callers`、`code.callees`、`code.impact`、`code.outline` 以及任务的 `code.context_pack` 会附上其返回文件相关的经验，并标明对应代码是已移动、已修改还是已不存在。[与代码关联的记忆](memory-linked-to-code.md)
- **可核查的新鲜度。** 每个层级都可能落后于分支。若返回了 `as_of`，请查看它；也可以用 `code.index_status` 查看相应层级的水位线。缺少新鲜度元数据即表示未知。[新鲜度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **插件与 MCP 客户端。** Claude Code 插件使用 `coding` 工具集连接，Codex 插件使用 `core,coding,tracker,tasks`；两者都会列出代码工具。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode 及其他 MCP 客户端，需在其 URL 上加 `?toolset=coding` 才会列出这些工具（`loom install` 会为 Cursor 添加该参数；`--toolset coding` 会为它写入配置的其他客户端添加该参数）。
  [连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## 实测结果

在 ClickHouse 仓库上，我们要求 Claude Code 在每个回答的末尾列出最多三个文件。使用 Loom 时，在 36 个概念性问题（10 次配对运行）中，它有 96.4% 指出了正确的文件，不使用 Loom 时为 91.4%，且使用 Loom 的运行成本低 21%（指 agent 的模型费用，不含 Loom 自身的模型调用）。
在 200 个 SWE-bench Verified 问题上，`code.search` 在前 10 条结果中排出了被修改文件的比例为 82%（这是文件定位，而不是问题解决），对比之下，对所有已索引文件做 BM25 为 57%，仅对 `.py` 文件做 BM25 为 79%；与仅 `.py` 的差距在噪声范围之内。
[使用与不使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified 文件定位](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="a-coding-agent-that-reads-the-definition-it-needs"></a>

## 只读取所需定义的编码 agent

从一个你信任的仓库开始。在已发布的技术栈上，请按照[由运营方配置的索引步骤](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)操作；控制台的添加仓库流程需要沙箱，而该技术栈并未启用沙箱。用插件连接 Claude Code 或 Codex，并在该仓库的检出目录内启动会话（[仓库匹配](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)）。

1. 让 agent 调用 `loom_op`，参数为 `op: "code.index_status"` 和 `arguments: {"namespace": "<indexed namespace>", "brief": true}`。记录 `git_head`，即快速层级已索引的提交；若其值为空，表示该层级没有水位线。
2. 选一个你清楚其定义的函数。让 agent 在该命名空间中调用 `loom_code_symbol`，传入函数名和 `include_body: true`。检查返回的 `path`、`start_line`、`end_line`、`body` 和 `tier`。
3. 仅当结果是唯一明确的定义、`tier: "treesitter"` 且函数体完整、未被省略时，才继续此检查。在依赖该结果之前，将其路径、行范围和函数体与所记录的 `git_head` 处的源码比对。若水位线或函数体缺失、定义不一致，或返回了其他层级，则改用本地读取。编译器层级各自独立推进，其水位线需要单独核对修订版本（[新鲜度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#the-mirrors-own-age)）。
4. 在同一检出目录中启动新会话，重复上述两次调用。索引会再次给出该定义，agent 无需加载之前的聊天记录或整份文件。

这只是检查一个快速层级的定义。未提交的修改仍需本地读取，而一次成功的查找并不能衡量任务准确率或成本节省。搜索、调用方和影响范围见[代码操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)。

<a id="start"></a>

## 开始

[添加仓库](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)会为某个仓库开启索引；[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)介绍了各层级。

本页中的部分操作在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。
