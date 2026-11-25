# Software factory evaluations

Use the [Claude Code setup recipe](SETUP.md) to configure your own factory with
the same direct Loom MCP connection and source instructions used in the benchmark.

Run the Python E2E example with a local Docker daemon:

```bash
python3 -m examples.software_factory plan
python3 -m examples.software_factory plan --pilot-task pallets__flask-4230
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --pilot-task pallets__flask-4230 --output ./factory-pilot
```

Harbor runs its built-in Claude Code agent in a normal checkout. Claude follows shared project instructions and chooses
its tool calls, edits, test commands and stopping point. The evaluator supplies the
original issue, a shared submission note, and the environment, then grades
the submitted implementation in a separate container. It adds no coding loop, custom source tools, retrieval hooks or completion review.

The comparison has five arms:

- Claude Code without Loom.
- Claude Code with Loom's tree-sitter code index.
- Claude Code with Loom's tree-sitter code index and historical memory.
- Claude Code with Loom's SCIP code index.
- Claude Code with Loom's SCIP code index and historical memory.

Every repetition completes all five arms in this order for each scenario
before the next repetition starts.

All arms use the same model, pinned Claude Code version, issue, base checkout,
resources and hidden verifier. Loom-enabled arms use the native MCP server and
scoped credentials. Code-only arms have no memory namespace. Each memory attempt
has its own writable namespace seeded with the same 30 historical facts, 14
linked to code. Facts come from public sources before the issue's base revision;
none are inserted into the issue prompt. The memory contrast measures both access
to historical knowledge and native memory capability, rather than linking alone.

All arms start from the same agent image. The four Loom arms add one image
layer with [project instructions](harbor/project-guidance.md) in
`/workspace/CLAUDE.md`. Claude uses Loom for covered unchanged source and
linked project history. Local tools handle edits, tests, Git, working-copy reads
and uncovered source. The baseline uses the common image and local tools. The
protocol freezes the instruction text and hash. Published runs retain the
guidance from their serving revision.

This example uses MCP directly; it does not install the Loom plugin or its hooks.
The server supplies initialization guidance. Reports record startup connection
state, advertised Loom tools and loaded Loom skills separately from selected calls.

A Loom session must receive indexed source through MCP to qualify. Harbor keeps
every trial result. The routing audit withholds comparative target verdicts when
any Loom session lacks source evidence. Local grep remains available for files
with working-copy changes, untracked files and paths outside Loom’s coverage.

Three public issues, five arms and three repeats produce 45 coding sessions.
Six base/reference controls qualify the grader without model calls. Nine blind
judgments assess submitted fixes after coding. Each attempt starts a fresh
Claude Code session. The report averages the session cost and agent time and
retains Anthropic’s cache token counts.

`--pilot-task` runs one selected issue once in each of the five arms, with two
base/reference controls and one blind judgment. It preserves the issue, historical
facts, hidden tests and full regression checks. Setup builds compiler artifacts,
native indexes and memory namespaces for the selected task. A single-issue
pilot gives descriptive results. Keep every pilot, including
negative and failed attempts, and declare the task before running. Resume uses
the selection frozen in the launch record.

Run the callback-order pilot with provider prompt caching disabled:

```bash
python3 -m examples.software_factory run --pilot-task pallets__flask-4230 --loom-schema-profile compact --disable-prompt-caching --output ./factory-uncached
```

For fresh provider cache starts, set `FACTORY_ANTHROPIC_WORKSPACE_MANIFEST` to the
path of a private JSON file. Supply `{"key_files": ["first.key", "second.key", "..."]}`
for workspace-scoped keys, or `{"key_file": "service.key", "workspace_ids": ["wrkspc_...", "..."]}`
for an unscoped key. Relative key paths start in the manifest's directory. List
one unused workspace per scheduled attempt. Use `ANTHROPIC_API_KEY` from a separate
workspace for setup and judging. The launcher checks the provider's workspace
headers before running. The report checks first-request cache reads for every
attempt, and Claude Code keeps caching within each session.

Use `--disable-prompt-caching` for an uncached comparison with the same
`ANTHROPIC_API_KEY` for every attempt. The pilot skips expiry waits and checks
Anthropic's cache reads and writes for each complete Claude Code session.

Omit `--pilot-task` to run the full matrix:

```bash
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --output ./factory-run
```

Harbor's native trajectory supplies total tool calls, Loom tool calls, input,
output and cache tokens, reported generation cost and agent time, including
recorded subagent activity. Loom's server ledger supplies attributed provider
usage and persisted memory writes. Missing measurements stay unknown. Provider
cost combines solver and Loom usage; setup and posthoc judging stay separate.
Infrastructure and invoice reconciliation are outside that cost measure.
The blind judge uses the pinned Claude Code image with structured output and
separate native usage receipts. It receives the public issue, pre-task project
documents, base source and anonymized candidate changes in a read-only
environment. Its rubric asks the judge to justify decisions from that evidence.
The independent verifier provides the executable issue grade.

The launcher automates dependencies, isolated services, indexes, historical
memory and native Harbor jobs. Coding, control solutions, indexing, builds and
grading have no execution deadline. Native coding runs have no dollar cap; capped
launches are refused. Harbor does not retry failed trials. Explicit resume preserves frozen
inputs and refuses interrupted paid requests that need reconciliation:

```bash
ANTHROPIC_API_KEY=... python3 -m examples.software_factory resume --output ./factory-run
python3 -m examples.software_factory cleanup --output ./factory-run
```

Cleanup removes the run's recorded dependency, compiler and Harbor images along
with its Loom services. Build receipts and results stay in the output directory.
Reports and charts are generated before coding, after each finished attempt and
at completion. See [Harbor reports](harbor/README.md).

[Flask](evals/flask/README.md) is the implemented Python profile; its
[reproduction instructions](evals/flask/REPRODUCE.md) describe the public issues,
source pins and historical memory. `--eval flask` selects it explicitly.
The [evaluation findings](evals/flask/LEARNINGS.md) link observations to fixes,
validation limits and retained negative results.

Shared execution, accounting and reports live in `harbor/`. Project-specific source,
dependencies, index recipes, historical facts and grading live in `evals/<id>/`.
A suite declares `languages` and `source_roots`, allowing a future profile to cover
several languages in one checkout. `factory/` supplies data types and evaluator
utilities; it does not execute the benchmark's coding agent.
