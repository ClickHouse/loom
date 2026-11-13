<!-- i18n: source=docs/use-cases/code-intelligence.md blob=af7c5d7d9b5c5333fc328a23fcfef0116c9b06ce model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 面向编码 agent 的代码索引

Loom 为编码 agent 提供一份通过 MCP 对外服务的、跨 git 仓库共享的索引，因此 agent 提出一个结构性问题，就能得到回答该问题的那几行代码，而不必读取整个文件。需要先添加仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。索引如何构建并保持最新，参见[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)；各语言下能提供哪些能力，参见[按语言划分的代码索引](code-intelligence-by-language.md)。

<a id="what-you-get"></a>

## 你能得到什么

- **定义、调用方与影响范围。** `code.symbol`、`code.callers` 和 `code.impact` 会返回文件和行号，每个回答都会标明其所依据的 commit 和索引层级。[代码操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **按语义搜索与按精确字符串搜索。** `code.search` 用来找出某个机制所在的位置；`code.grep` 用来查找精确的消息或标识符。
- **历史与 pull request。** 包含评审评论的已合并 PR 历史、提交 PR 前的重复检查，以及用于定位回归的 bisect 候选。这些功能需要开启 PR 历史（`LOOM_CODE_PR_HISTORY_ENABLED`）；评审评论和未合并的 PR 还需要 GitHub 令牌。每项功能的具体要求参见[历史、PR 与发布](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)。
- **可见的新鲜度。** 索引会落后于分支上已合并的 commit，过期的回答会明确说明这一点。[新鲜度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **插件与 MCP 客户端。** Claude Code 插件通过 `coding` 工具集连接，Codex 插件通过 `core,coding,tracker,tasks` 连接；两者都会列出代码工具。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode 以及其他 MCP 客户端使用 `?toolset=coding`。
  [连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [各客户端的配置](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#other-editors-and-agents)

<a id="measured"></a>

## 实测结果

在 ClickHouse 仓库上，我们要求 Claude Code 在每个回答的末尾给出最多三个文件。使用 Loom 时，在 36 个概念性问题（10 次配对运行）上，它有 96.4% 指出了正确的文件，不使用 Loom 时为 91.4%，而且使用 Loom 的运行成本低 21%（指 agent 的模型费用，不包括 Loom 自身的模型调用）。
在 200 个 SWE-bench Verified issue 上，`code.search` 在前 10 条结果中排到了被修改文件的比例为 82%（这是文件定位，不是 issue 解决），而对所有已索引文件做 BM25 为 57%，仅对 `.py` 文件做 BM25 为 79%；与仅 `.py` 的差距在噪声范围之内。
[使用与不使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified 文件定位](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 开始使用

[添加仓库](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)会为某个仓库开启索引；[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)介绍了各个层级。

本页中的部分操作在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。
