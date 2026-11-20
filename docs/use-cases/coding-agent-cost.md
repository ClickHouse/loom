# Lower model cost for coding agents

With the Loom plugin and code index, Claude Code answered questions about the ClickHouse
codebase at 6% to 21% lower model cost than without it on all three measured question
sets. The savings were 20% on 24 answer-graded questions (answer quality 0.84 against
0.78), 21% on 36 conceptual questions and 6% on 24 multi-hop questions, where tracing a bug
to its fix on a release branch cost the same both ways.

## What was measured

Both arms ran Claude Code (`claude-sonnet-5`) on the same checkout: one plain session, one
with the Loom plugin and the `coding` toolset. Cost is the whole session's model bill and
excludes Loom's own server-side model calls, hosting and indexing. The comparison measures
the plugin as a whole; it does not separate the code index from memory. Results are
specific to the tested workloads. With Loom a question takes longer on the clock: the
plugin's per-prompt search and the MCP round trips add latency. How per-question cost is
compared against run-to-run noise is on the results page.
[With and without Loom](../benchmarks/results.md#with-and-without-loom) has every set, the
protocol and the artifacts, and [evaluate Loom](../getting-started/evaluate.md) shows how
to measure it on your own repository.

For memory read by a model instead of a whole chat history, see
[agent memory](agent-memory.md#measured).

## How the plugin is designed to save

- **Index answers instead of file reads.** `code.symbol`, `code.callers` and `code.search`
  return the lines that answer a structure question. [Code index](code-intelligence.md)
- **Saved context instead of rediscovery.** Decisions and findings a session saves are
  loaded at the next session's start. [Shared memory](agent-memory.md)

## Decide whether retrieval pays for your workload

Loom is worth evaluating when agents repeatedly need the same repository structure or
verified project decisions. A smaller tool response alone does not establish a lower
bill or a correct answer.

| Workload                                              | Check before choosing memory or indexed retrieval                                         |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Repeated questions about a repository                 | Can a definition and its callers replace the file reads needed for a correct answer?      |
| Repeated project decisions across sessions            | Does scoped recall recover a still-valid decision instead of repeating the investigation? |
| A short task with all needed context already supplied | Does search add calls or latency without avoiding other work?                             |
| A changing repository or an unfamiliar task           | Can the agent verify the indexed revision and reject irrelevant or stale context?         |

### Compare completed tasks, including overhead

Use the same repository revision, questions, model settings, permissions and answer
checks in both arms. Keep failed and incomplete tasks in the report. Balance execution
order and record whether each arm starts with a warm or cold provider cache.

For each task record:

- **Task quality and completion.** Apply the same acceptance check to both answers;
  fewer tokens are not a win when the task fails.
- **The coding model's bill.** Include every turn, tool round trip and retry. Separate
  uncached input, cache reads, cache writes and output; equal token counts can have
  different prices.
- **Loom's costs.** Record its provider charges, including indexing, separately from
  the coding model. Add the deployment's hosting cost and state how indexing and
  hosting are allocated across tasks. Count each charge once.
- **Elapsed time.** Report time to an accepted answer as well as model cost.

The [usage summary](../features/observability/cost-and-usage.md#the-coding-agents-own-spend)
reports model token classes, cost and unpriced turns. Unpriced usage is unknown, not
free. Preserve missing prices and missing task outcomes rather than count them as zero.
The [evaluation guide](../getting-started/evaluate.md) describes the paired setup.

Compare total cost across the full task set, including failures. Choose a configuration
only if it also meets your quality and latency requirements.
Report setup cost separately so a one-off task and repeated use are not compared as
though they amortize indexing equally. This checklist is a measurement procedure, not
an additional measured result or a guarantee of savings.

## Request code in smaller steps

For an indexed repository on the `coding` toolset, ask the agent to call
`loom_code_search` with:

```json
{
  "namespace": "<indexed namespace>",
  "query": "where are request quotas enforced?",
  "top_k": 3,
  "bodies": "none",
  "token_budget": 1200
}
```

`bodies: "none"` withholds code bodies. Choose a hit by its path, span and tier, when
present. If its `next` is `code.symbol name=...`, pass that name to `loom_code_symbol`
in the same namespace with `include_body: true` to request source. A name can resolve
to several definitions, so check their paths and tiers before using one.

Read `elided`, `granularity` and `next`: a card or class skeleton is not the complete
implementation. Follow the [definition and revision check](code-intelligence.md#a-coding-agent-that-reads-the-definition-it-needs)
before relying on the source; missing bodies or freshness metadata need a local read.
The budget estimates the code answer's size, not the model's whole context or bill.
Measure cost and task quality for the complete workflow on your own repository.

Loom also records what it spends on embedding and LLM calls per operation.
[Cost and usage](../features/observability/cost-and-usage.md)
