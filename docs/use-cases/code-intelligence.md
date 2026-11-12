# A code index for coding agents

Loom gives coding agents a shared index of your git repositories, served over MCP, so an
agent asks a structure question and gets the lines that answer it instead of reading whole
files. It needs a repository added and `LOOM_CODE_INGEST_ENABLED=true`; how the index is
built and kept fresh is in [the code index overview](../features/code-index/overview.md).

## What you get

- **Definitions, callers and impact.** `code.symbol`, `code.callers` and `code.impact`
  answer with the file and line, and each answer names the commit and index tier behind
  it. [Code operations](../features/code-index/operations.md)
- **Search by meaning and by exact string.** `code.search` finds where a mechanism lives;
  `code.grep` finds an exact message or identifier.
- **History and pull requests.** Merged-PR history with review comments, a pre-PR
  duplicate check and bisect candidates for a regression. Needs
  `LOOM_CODE_PR_HISTORY_ENABLED=true`; review comments also need a
  GitHub token (`LOOM_CODE_GITHUB_TOKEN` or the repository entry's `github_token`).
  [History, PRs and releases](../features/code-index/history-and-prs.md)
- **Freshness you can see.** The index trails the branch by merged commits, and a stale
  answer says so. [Freshness](../features/code-index/freshness.md)
- **Plugins.** The Claude Code and Codex plugins connect on the `coding` toolset, which
  lists the code tools; Cursor and other MCP clients use `?toolset=coding`.
  [Connect a coding agent](../getting-started/connect-a-coding-agent.md)

## Measured

On the ClickHouse repository, Claude Code with Loom named the right file among its three for
96.4% of 36 conceptual questions (10 paired runs) against 91.4% without it, at 21% lower model cost.
On 200 SWE-bench Verified issues, `code.search` ranked a changed file in its first 10
results for 82% (file localization, not issue resolution),
against 57% for BM25 over every indexed file and 79% for BM25 over `.py` files only; the `.py`-only gap is within noise.
[With and without Loom](../benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified file localization](../benchmarks/results.md#swe-bench-verified-file-localization)

## Start

[Adding repositories](../features/code-index/repositories.md) turns the index on for a
repository; [the code index overview](../features/code-index/overview.md) explains the
tiers.
