---
title: MCP code search and caller graphs for coding agents
description: Search indexed repositories over MCP, inspect definitions and callers, check tier-specific commits and follow code-linked lessons before changing code.
---

# A code index for coding agents

Loom gives coding agents a shared index of your git repositories, served over MCP, so an
agent asks a structure question and gets the lines that answer it instead of reading whole
files. Needs a repository added and `LOOM_CODE_INGEST_ENABLED=true`. How the index is
built and kept fresh is in [the code index overview](../features/code-index/overview.md);
what it gives in each language is in [code index by language](code-intelligence-by-language.md).

## What you get

- **Definitions, callers and impact.** `code.symbol`, `code.callers` and `code.impact`
  locate code with file and line references. `code.index_status` reports each tier's
  indexed commit. [Code operations](../features/code-index/operations.md)
- **Search by meaning and by exact string.** `code.search` finds where a mechanism lives;
  `code.grep` finds an exact message or identifier.
- **History and pull requests.** Merged-PR history with review comments, a pre-PR
  duplicate check and bisect candidates for a regression. These need PR history turned on
  (`LOOM_CODE_PR_HISTORY_ENABLED`); review comments and open PRs also need a GitHub token.
  [History, PRs and releases](../features/code-index/history-and-prs.md) says what each
  needs.
- **Lessons on the code.** `code.symbol` returns the memories linked to a definition, and
  `code.search`, `code.callers`, `code.callees`, `code.impact`, `code.outline` and a task's
  `code.context_pack` attach the lessons for the files they return, each with whether its
  code moved, changed or is gone. [Memory linked to code](memory-linked-to-code.md)
- **Freshness you can check.** Each tier can trail the branch. Inspect `as_of` when
  returned, or use `code.index_status` for the relevant tier's watermark. Missing
  freshness metadata is unknown. [Freshness](../features/code-index/freshness.md)
- **Plugins and MCP clients.** The Claude Code plugin connects on the `coding` toolset and
  the Codex plugin on `core,coding,tracker,tasks`; both list the code tools. Cursor, GitHub
  Copilot, Gemini CLI, Cline, Kilo Code, OpenCode and other MCP clients list them with
  `?toolset=coding` on their URL (`loom install` adds it for Cursor; `--toolset coding` adds
  it for the others it writes).
  [Connect a coding agent](../getting-started/connect-a-coding-agent.md) ·
  [MCP clients](../features/integrations/mcp-clients.md)

## Measured

On the ClickHouse repository, Claude Code was asked to end each answer with up to three
files. With Loom it named the right file for 96.4% of 36 conceptual questions (10 paired
runs), against 91.4% without Loom, and the runs with Loom cost 21% less (the agent's model
bill; Loom's own model calls excluded).
On 200 SWE-bench Verified issues, `code.search` ranked a changed file in its first 10
results for 82% (file localization, not issue resolution), against 57% for BM25 over every
indexed file and 79% for BM25 over `.py` files only; the `.py`-only gap is within noise.
[With and without Loom](../benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified file localization](../benchmarks/results.md#swe-bench-verified-file-localization)

## A coding agent that reads the definition it needs

For an edit to a known definition, [`code.edit_context`](../reference/operations/code.md#codeedit_context)
reads the implementation, indexed test excerpts, and linked lessons together.
Use the returned source revision and each lesson's code state when applying it.

Start with a repository you trust. On the released stack, follow the
[operator-configured index steps](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack);
the dashboard's add-repository flow needs a sandbox that stack does not enable. Connect
Claude Code or Codex with its plugin and start a session inside that repository's checkout
([repository matching](../getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)).

1. Ask the agent to call `loom_op` with `op: "code.index_status"` and
   `arguments: {"namespace": "<indexed namespace>", "brief": true}`. Record `git_head`,
   the fast tier's indexed commit; an empty value means that tier has no watermark.
2. Pick a function whose definition you know. Ask the agent to call `loom_code_symbol`
   in that namespace with the function's name and `include_body: true`. Check the
   returned `path`, `start_line`, `end_line`, `body` and `tier`.
3. Continue this check only for one unambiguous definition with `tier: "treesitter"`
   and a complete, non-elided body. Compare its path, span and body with the source at
   the recorded `git_head` before relying on it. Use a local read if the watermark or
   body is missing, the definition differs, or another tier is returned. Compiler tiers
   advance independently; their watermarks need a separate revision check
   ([freshness](../features/code-index/freshness.md#the-mirrors-own-age)).
4. Start a fresh session in the same checkout and repeat both calls. The index supplies
   the definition again; the agent need not load the previous chat or the whole file.

This checks one fast-tier definition. Uncommitted edits still need local reads, and a
successful lookup is not a measurement of task accuracy or cost savings. See
[code operations](../features/code-index/operations.md) for search, callers and impact.

## Check one caller and its impact

This check is independent of the fast-tier definition check above: the caller may come
from either the fast tier or a compiler tier. Use an indexed repository and choose a
function with one direct caller you can identify in source. Its language needs call edges
([language support](../features/code-index/overview.md#language-support)). Replace the
placeholders below with your organization, indexed namespace and qualified function name.

1. Call `loom_op` with `op: "code.index_status"` and
   `arguments: {"org": "<org>", "namespace": "<indexed namespace>", "brief": false}`.
   Record `git_head` and the returned `compiler_tiers` commits. Fast and compiler tiers
   can describe different revisions.
2. Ask the agent to call `loom_op` with this request:

   ```json
   {
     "op": "code.callers",
     "arguments": {
       "org": "<org>",
       "namespace": "<indexed namespace>",
       "name": "<qualified function name>",
       "depth": 1,
       "max_nodes": 20,
       "include_ambiguous": false,
       "explain": true
     }
   }
   ```

   The inner `arguments.explain: true` requests the full code-op result instead of the
   compact MCP shape, which can omit fields needed for these checks. It does not change graph
   coverage or freshness.

   Check a returned `edges` row's `from_symbol`, `path`, `line`, `edge_kind`, `tier`
   and `confidence`. Verify that the source at that tier's recorded commit contains
   the direct call; a reference to the name alone does not establish a call. If
   `ambiguous` is true, choose the intended qualified name from `candidates` first.

3. Call `loom_op` again with `op: "code.impact"` and
   `arguments: {"org": "<org>", "namespace": "<indexed namespace>", "name": "<qualified function name>", "depth": 1, "per_hop_limit": 20, "explain": true}`.
   Look for the checked caller in `impacted_symbols`: `qualified_name`, `path`,
   `hop: 1` and `tier`. This request resolves the supplied name; `files` would
   seed every function in those files. `impacted_tests` lists mapped tests;
   the op does not execute them.
4. Read `epistemic`, `causes`, `counts_floor` and `truncated` on both answers, and
   repeat the status request. Compare returned locations with source at the relevant
   tier's commit. Missing fields or watermarks, changed watermarks, a missing expected
   caller or a source mismatch leave this check unverified
   ([freshness](../features/code-index/freshness.md#the-mirrors-own-age)).

This checks one caller's membership in the indexed graph. Counts remain floors,
including when `epistemic` is `exact`: runtime dispatch can be absent. Empty edges or
impact rows do not prove that a function has no callers or that a change is safe.

## Start

[Adding repositories](../features/code-index/repositories.md) turns the index on for a
repository; [the code index overview](../features/code-index/overview.md) explains the
tiers.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
