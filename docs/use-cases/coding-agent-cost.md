# Lower token and model cost for coding agents

With the Loom plugin and code index, Claude Code answered questions about the ClickHouse
codebase with lower model cost than without it: 20% lower on 24 answer-graded questions
(answer quality 0.84 against 0.78) and 21% lower on 36 file-localization questions. The
savings come from answering structure questions from an index instead of reading whole
files, and from loading saved context instead of rediscovering it.

## Where the savings come from

- **Index answers instead of file reads.** `code.symbol`, `code.callers` and `code.search`
  return the lines that answer the question, so the agent reads fewer files.
  [Code index](code-intelligence.md)
- **Memory instead of rediscovery.** Decisions and findings a session saves are loaded at
  the next session's start, so the agent does not search for them again.
  [Shared memory](agent-memory.md)
- **Retrieved memory instead of whole history.** For a reader model, Loom's top memories
  used 91% fewer prompt tokens per LongMemEval-S question than the whole chat history.
- **A small default tool listing.** The default MCP listing keeps tool definitions short;
  other operations stay callable by name. [MCP toolsets](../reference/mcp.md#toolsets)

## How it was measured

Both arms ran Claude Code (`claude-sonnet-5`) on the same checkout; cost is the whole
session's model bill and excludes Loom's own server-side model calls, hosting and
indexing. Results are specific to the tested workloads.
[With and without Loom](../benchmarks/results.md#with-and-without-loom) has the protocol,
and [evaluate Loom](../getting-started/evaluate.md) shows how to measure it on your own
repository.

Loom also records what it spends on embedding and LLM calls per operation.
[Cost and usage](../features/observability/cost-and-usage.md)
