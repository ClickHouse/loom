<!-- i18n: source=docs/use-cases/code-intelligence.md blob=4f52e242dcb612150541843af58b15f37c31a265 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 面向编码 agent 的代码索引

Loom 为编码 agent 提供一个共享的 git 仓库索引，通过 MCP 提供服务，这样 agent 只需提出结构方面的问题，就能得到能回答问题的那几行代码，而不必读取整个文件。需要先添加仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。索引如何构建并保持最新，见[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)；各语言下索引能提供什么，见[按语言划分的代码索引](code-intelligence-by-language.md)。

<a id="what-you-get"></a>

## 你能得到什么

- **定义、调用方和影响范围。** `code.symbol`、`code.callers` 和 `code.impact` 的回答会给出文件和行号，并注明回答所依据的 commit 和索引层级。[代码操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **按语义搜索和按精确字符串搜索。** `code.search` 用来找到某个机制所在的位置；`code.grep` 用来查找精确的消息或标识符。
- **历史和 pull request。** 包含评审评论的已合并 PR 历史、提交 PR 前的重复检查，以及用于定位回归的 bisect 候选项。这些功能需要开启 PR 历史（`LOOM_CODE_PR_HISTORY_ENABLED`）；评审评论和未合并的 PR 还需要 GitHub 令牌。每项功能的具体要求见[历史、PR 和发布](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)。
- **可见的新鲜度。** 索引落后于分支的程度以已合并的 commit 数计，过期的回答会明确说明。[新鲜度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **插件。** Claude Code 插件通过 `coding` toolset 连接，Codex 插件通过 `core,coding,tracker,tasks` 连接；两者都会列出代码工具。Cursor 和其他 MCP 客户端使用 `?toolset=coding`。[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 实测结果

在 ClickHouse 仓库上，要求 Claude Code 在每次回答的末尾给出最多三个文件。使用 Loom 时，在 36 个概念性问题（10 次配对运行）中，96.4% 的问题指出了正确的文件，不使用 Loom 时为 91.4%，而且使用 Loom 的运行成本低 21%（指 agent 的模型费用，不含 Loom 自身的模型调用）。在 200 个 SWE-bench Verified issue 上，`code.search` 在前 10 个结果中排入了被修改文件的比例为 82%（这是文件定位，而非 issue 解决），对比之下，对所有已索引文件做 BM25 为 57%，仅对 `.py` 文件做 BM25 为 79%；与仅 `.py` 的差距在噪声范围之内。[使用与不使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified 文件定位](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 开始使用

[添加仓库](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)会为某个仓库开启索引；[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)解释了各个层级。

本页中的部分操作在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上标记为实验性。
