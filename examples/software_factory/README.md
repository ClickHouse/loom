# Software factory evaluations

Run the Python E2E example with a local Docker daemon:

```bash
python3 -m examples.software_factory plan
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --output ./factory-run
```

Harbor runs its built-in Claude Code agent in a normal checkout. Claude follows shared project instructions and chooses
its tool calls, edits, test commands and stopping point. The evaluator supplies the
original issue and environment, then grades the submitted implementation in a
separate container. It adds no coding loop, custom source tools, retrieval hooks or completion review.

The comparison has five arms:

- Claude Code without Loom.
- Claude Code with Loom's tree-sitter code index.
- Claude Code with Loom's tree-sitter code index and historical memory.
- Claude Code with Loom's SCIP code index.
- Claude Code with Loom's SCIP code index and historical memory.

All arms use the same model, pinned Claude Code version, issue, base checkout,
resources and hidden verifier. Loom-enabled arms use the native MCP server and
scoped credentials. Code-only arms have no memory namespace. Each memory attempt
has its own writable namespace seeded with the same 30 historical facts, 14
linked to code. Facts come from public sources before the issue's base revision;
none are inserted into the issue prompt. The memory contrast measures both access
to historical knowledge and native memory capability, rather than linking alone.

Every arm receives identical [project instructions](harbor/project-guidance.md)
in `/workspace/CLAUDE.md`. With Loom configured, these direct source discovery
through Loom for unchanged indexed roots; without Loom, local tools supply that
context. Local tools remain available for edits, tests, Git, unindexed files,
changed files and source Loom reports as unavailable. This is an instructed
comparison. It does not measure tool adoption without project instructions.
The protocol freezes the instruction text and hash; task images use those bytes.
The instructions contain no issue-specific hints or required retrieval sequence.

This example uses MCP directly; it does not install the Loom plugin or its hooks.
The server supplies initialization guidance. Reports record startup connection
state, advertised Loom tools and loaded Loom skills separately from selected calls.

There are three public issues, three repeats and five arms: 45 primary attempts.
Six A/A attempts measure baseline variation separately. Six base/reference controls
qualify the grader without model calls. Twelve blind judgments assess submitted
fixes after coding; judgments do not provide feedback to the solver.

Harbor's native trajectory supplies total tool calls, Loom tool calls, input,
output and cache tokens, reported generation cost and agent time, including
recorded subagent activity. Loom's server ledger supplies attributed provider
usage and persisted memory writes. Missing measurements stay unknown. Provider
cost combines solver and Loom usage; setup and posthoc judging stay separate.
Infrastructure and invoice reconciliation are outside that cost measure.

The launcher automates dependencies, isolated services, indexes, historical
memory and native Harbor jobs. Coding, control solutions, indexing, builds and
grading have no execution deadline. Native coding runs have no dollar cap; capped
launches are refused. Harbor does not retry failed trials. Explicit resume preserves frozen
inputs and refuses interrupted paid requests that need reconciliation:

```bash
ANTHROPIC_API_KEY=... python3 -m examples.software_factory resume --output ./factory-run
python3 -m examples.software_factory cleanup --output ./factory-run
```

Cleanup removes recorded owned resources and retains results. It does not prune
unrelated Docker data. Reports and charts are generated before coding, after each
finished attempt and at completion. See [Harbor reports](harbor/README.md).

[Flask](evals/flask/README.md) is the implemented Python profile; its
[reproduction instructions](evals/flask/REPRODUCE.md) describe the public issues,
source pins and historical memory. `--eval flask` selects it explicitly.

Shared execution, accounting and reports live in `harbor/`. Project-specific source,
dependencies, index recipes, historical facts and grading live in `evals/<id>/`.
A suite declares `languages` and `source_roots`, allowing a future profile to cover
several languages in one checkout. `factory/` supplies data types and evaluator
utilities; it does not execute the benchmark's coding agent.
