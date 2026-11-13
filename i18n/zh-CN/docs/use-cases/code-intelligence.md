<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 面向编码 agent 的代码索引

Loom 为编码 agent 提供一个共享的 git 仓库索引，通过 MCP 对外提供服务，这样 agent 提出结构性问题时，得到的是能够回答问题的那几行代码，而不必通读整个文件。使用前需要先添加一个仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。索引如何构建并保持最新，请参阅[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)。

<a id="what-you-get"></a>

## 你能得到什么

- **定义、调用方和影响范围。** `code.symbol`、`code.callers` 和 `code.impact` 的回答都带有文件和行号，并且每个回答都会注明其所依据的提交和索引层级。[Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **按语义搜索和按精确字符串搜索。** `code.search` 用于找到某个机制所在的位置；`code.grep` 用于查找精确的消息或标识符。
- **历史和 pull request。** 包含评审评论的已合并 PR 历史、提交 PR 前的重复检查，以及用于定位回归的 bisect 候选。这些功能需要开启 PR 历史（`LOOM_CODE_PR_HISTORY_ENABLED`）；评审评论和未合并的 PR 还需要 GitHub 令牌。[History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) 说明了各项功能的具体要求。
- **可见的新鲜度。** 索引会落后于分支上已合并的提交，过期的回答会明确标注这一点。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **插件。** Claude Code 插件通过 `coding` toolset 连接，Codex 插件通过 `core,coding,tracker,tasks` 连接；两者都会列出代码工具。Cursor 和其他 MCP 客户端使用 `?toolset=coding`。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 实测结果

在 ClickHouse 仓库上，对 36 个概念性问题（10 次配对运行），使用 Loom 的 Claude Code 在三个候选文件中选出正确文件的比例为 96.4%，不使用 Loom 时为 91.4%，同时模型成本降低 21%（指 agent 的模型费用，不含 Loom 自身的模型调用）。在 200 个 SWE-bench Verified issue 上，`code.search` 在前 10 条结果中排到某个被修改文件的比例为 82%（这是文件定位，而非 issue 解决），对比之下，对所有已索引文件做 BM25 的比例为 57%，仅对 `.py` 文件做 BM25 的比例为 79%；仅 `.py` 文件这一组的差距在噪声范围内。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified file localization](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 开始

[添加仓库](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)会为仓库启用索引；[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)介绍了各个层级。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上标记为实验性。
