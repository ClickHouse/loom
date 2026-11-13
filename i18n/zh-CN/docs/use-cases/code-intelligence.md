<!-- i18n: source=docs/use-cases/code-intelligence.md blob=71addec3496d26800b8e03234eb0c0adc578b52c model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# 面向编码 agent 的代码索引

Loom 为编码 agent 提供一个共享的 git 仓库索引，通过 MCP 对外提供服务，这样 agent 提出结构性问题时，得到的是能回答问题的那几行代码，而不必读取整个文件。需要已添加仓库，并设置 `LOOM_CODE_INGEST_ENABLED=true`。索引如何构建、如何保持最新，见[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)；各语言下能提供哪些能力，见[按语言划分的代码索引](code-intelligence-by-language.md)。

<a id="what-you-get"></a>

## 你能得到什么

- **定义、调用方和影响范围。** `code.symbol`、`code.callers` 和 `code.impact` 返回文件和行号，每个答案都会注明其所依据的提交和索引层级。[代码操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **按语义搜索和按精确字符串搜索。** `code.search` 用于找到某个机制所在的位置；`code.grep` 用于查找精确的消息或标识符。
- **历史和 pull request。** 包含评审评论的已合并 PR 历史、提交 PR 前的重复检查，以及回归问题的 bisect 候选提交。这些功能需要开启 PR 历史（`LOOM_CODE_PR_HISTORY_ENABLED`）；评审评论和未合并的 PR 还需要 GitHub 令牌。[历史、PR 和发布](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)说明了每项功能的需求。
- **可见的新鲜度。** 索引落后于分支的程度以已合并提交数计，过期的答案会明确说明。[新鲜度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **插件和 MCP 客户端。** Claude Code 插件使用 `coding` 工具集连接，Codex 插件使用 `core,coding,tracker,tasks` 连接；两者都会列出代码工具。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode 及其他 MCP 客户端，在 URL 上加 `?toolset=coding` 即可列出这些工具（`loom install` 会为 Cursor 添加该参数；对其写入配置的其他客户端，则由 `--toolset coding` 添加）。[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) · [MCP 客户端](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## 实测结果

在 ClickHouse 仓库上，要求 Claude Code 在每个答案末尾最多给出三个文件。使用 Loom 时，在 36 个概念性问题（10 次配对运行）中，96.4% 指出了正确的文件，不使用 Loom 时为 91.4%，且使用 Loom 的运行成本低 21%（指 agent 的模型费用，不含 Loom 自身的模型调用）。在 200 个 SWE-bench Verified issue 上，`code.search` 在前 10 条结果中排到了被修改文件的比例为 82%（这是文件定位，不是 issue 解决），对比之下，对所有已索引文件做 BM25 为 57%，仅对 `.py` 文件做 BM25 为 79%；与仅 `.py` 的差距在噪声范围内。[使用与不使用 Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) · [SWE-bench Verified 文件定位](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 开始使用

[添加仓库](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)会为某个仓库开启索引；[代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)解释了各个层级。

本页中的部分操作在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上标记为实验性。
