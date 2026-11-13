<!-- i18n: source=docs/use-cases/code-intelligence.md blob=42cc2c3e3e4af87c1d93ff61f15c3e5f7c8f6117 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 面向 coding agent 的代码索引

Loom 为 coding agent 提供一个共享的 git 仓库索引，通过 MCP 对外提供服务，这样 agent 提出结构性问题时，得到的是能回答问题的那几行代码，而不必读取整个文件。使用前需要先添加一个仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。索引如何构建并保持最新，请参阅[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)。

<a id="what-you-get"></a>

## 你能得到什么

- **定义、调用方和影响范围。** `code.symbol`、`code.callers` 和 `code.impact` 的返回都带有文件和行号，并注明该结果所依据的 commit 和索引层级。[Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **按语义搜索和按精确字符串搜索。** `code.search` 用于找到某个机制所在的位置；`code.grep` 用于查找精确的消息或标识符。
- **历史和 pull request。** 包含评审评论的已合并 PR 历史、提交 PR 前的重复检查，以及回归问题的 bisect 候选项。这些功能需要开启 PR 历史（`LOOM_CODE_PR_HISTORY_ENABLED`）；评审评论和未合并的 PR 还需要 GitHub 令牌。[History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) 说明了每项功能的具体要求。
- **可见的新鲜度。** 索引落后于分支的程度以已合并的 commit 数计，过期的结果会明确标注。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **插件。** Claude Code 插件通过 `coding` toolset 连接，Codex 插件通过 `core,coding,tracker,tasks` 连接；两者都会列出代码工具。Cursor 和其他 MCP 客户端使用 `?toolset=coding`。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 实测结果

在 ClickHouse 代码库上，我们要求 Claude Code 在每个回答的末尾给出最多三个文件。使用 Loom 时，在 36 个概念性问题（10 次配对运行）中，它有 96.4% 指出了正确的文件，不使用 Loom 时为 91.4%，而且使用 Loom 的运行成本降低了 21%（指 agent 的模型费用，不含 Loom 自身的模型调用）。在 200 个 SWE-bench Verified issue 上，`code.search` 在前 10 个结果中排入了被修改文件的比例为 82%（这是文件定位，而非 issue 解决），而对所有已索引文件使用 BM25 为 57%，仅对 `.py` 文件使用 BM25 为 79%；与仅 `.py` 的结果之间的差距在噪声范围内。
[使用与不使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified 文件定位](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 开始

[添加仓库](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)会为该仓库启用索引；[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)介绍了各个层级。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。
