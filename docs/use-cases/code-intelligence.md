# A code index for coding agents

Loom indexes your git repositories and answers coding agents' structure questions over MCP:
where a symbol is defined, who calls it, what a change reaches, which tests cover it and
which merged pull request touched a line. Every agent reads the same index, so a
repository is parsed and embedded once per merge, not once per session.

## What you get

- **Definitions, callers and impact.** `code.symbol`, `code.callers` and `code.impact`
  answer with the file and line, and each answer names the commit and index tier behind
  it. [Code operations](../features/code-index/operations.md)
- **Search by meaning and by exact string.** `code.search` finds where a mechanism lives;
  `code.grep` finds an exact message or identifier.
- **History and pull requests.** Merged-PR history with review comments, a pre-PR
  duplicate check and bisect candidates for a regression.
  [History, PRs and releases](../features/code-index/history-and-prs.md)
- **Freshness you can see.** The index trails the branch by merged commits, and a stale
  answer says so. [Freshness](../features/code-index/freshness.md)
- **Plugins.** The Claude Code and Codex plugins connect on the `coding` toolset, which
  lists the code tools; Cursor and other MCP clients use `?toolset=coding`.
  [Connect a coding agent](../getting-started/connect-a-coding-agent.md)

## Measured

On the ClickHouse repository, Claude Code with Loom found the right file for 96.4% of 36
conceptual questions (10 paired runs) against 91.4% without it, at 21% lower model cost.
On 200 SWE-bench Verified issues, a changed file was among the first 10 results for 82%,
against 57% for BM25. [With and without Loom](../benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified file localization](../benchmarks/results.md#swe-bench-verified-file-localization)

## Start

[Adding repositories](../features/code-index/repositories.md) turns the index on for a
repository; [the code index overview](../features/code-index/overview.md) explains the
tiers.
