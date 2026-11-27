# Software factory evaluation

The evaluator asks a coding agent to fix a public issue in its ordinary checkout.
Harbor runs its built-in Claude Code agent, collects its native trajectory and
passes the resulting implementation to a separate hidden verifier. The evaluator
provides no custom coding loop, source tools, hooks, forced retrieval or completion
review. Claude chooses its reads, edits, commands, retrieval and stopping point.

## Shared execution and project profiles

`examples/software_factory/harbor/` owns environment setup, frozen schedules,
measurement and reports. `evals/<id>/` supplies public issues, repository revisions,
source roots, dependency locks, historical facts, index recipes and grading. The
implemented profile is a Python E2E evaluation of Flask. Language and source-root
lists allow a future profile to describe a repository with several languages.

The public command builds pinned dependencies and isolated services, prepares the
indexes and historical memory, qualifies the grading contract and starts Harbor.
It records exact owned resources for cleanup. Native coding runs are uncapped;
capped launches are refused. Provider credentials and raw sessions stay outside
public result bundles.

## Treatments

Five arms receive the same original issue, model, pinned agent version, base
checkout image, resources and hidden grading contract:

- No Loom connection.
- Loom with a tree-sitter code index.
- Loom with a tree-sitter code index and historical memory.
- Loom with a SCIP code index.
- Loom with a SCIP code index and historical memory.

Loom connections use the native coding MCP roster. A connection header selects
its code scope. Native namespace permissions and caller-home resolution select
its optional memory scope. Code-only arms have no historical memory namespace or
memory write permission; they see the same coding roster as memory arms. The
model decides whether to call any advertised tool. No result-count or response
budget override is supplied by the evaluation.

Every attempt has separate credentials. Each memory attempt starts with its own
writable history, preventing earlier attempts from supplying solutions to later
ones. The historical facts have public sources and cutoffs before each issue's
base. Some facts link to native code symbols. Setup replays an older source head,
advances to the buggy base, checks changed anchors and revises one independently
sourced fact. This verifies a replayed memory lifecycle, rather than measuring
months of autonomous learning. Historical knowledge is extra information, so the
contrast does not isolate linking from access to that knowledge.

Historical revision transitions apply Loom's native file deletion to text paths
present in the earlier verified Git source and absent from the next. Source
receipts retain those exact paths and retirement counts. Full index audits check
the resulting live rows against the current source before coding begins.

## Grading and accounting

Base and reference controls use Harbor's built-in oracle without model calls.
Unchanged source must fail the hidden checks; the published fix must pass. These
controls qualify the same frozen grading contract before coding begins.

The coding environment has the public base checkout and its tests. Hidden tests
and reference fixes remain outside it. Harbor collects implementation files as a
native artifact and restores them into the separate verifier. The verifier checks
source paths and sizes, reconstructs its own workspace and runs the fixed hidden
tests. Agent-authored tests do not replace the hidden tests. Full-suite regressions
are measured against the unchanged base. A posthoc blind judge sees the original
issue, public historical facts and candidate source differences; it cannot send
feedback to the coding agent.

Harbor's trajectory supplies total tool calls, Loom calls, input/output/cache
tokens, generation cost and agent time, including recorded subagent activity.
Tokens already include cache usage in native prompt totals; reporting does not
add cache tokens twice. Explicit test commands are observable in shell calls;
tests hidden inside scripts are not classified as test runs.

Authenticated attempt subjects attribute Loom provider events and persisted
memory versions. Initialization and refusal events retain their native scope
semantics and cost. Initialization events do not stand in for selected tool
calls. Missing usage remains unknown. Source index fingerprints before and after
each attempt detect mutation. Setup and judging costs remain separate from agent
cost; infrastructure and invoice reconciliation are outside that measure.

## Comparison and reproducibility

The profile contains three issues, three repeats and five arms: 45 primary
attempts. Six baseline A/A attempts measure variability separately. The schedule
balances arm positions and runs serially without Harbor retries. Every failure
remains included. Repeats do not create independent issues, and three issues do
not establish a general result across repositories.

Native results stay immutable. Derived Harbor views align matching issue/repeat
rows across arms and record the original identifiers and source hashes. Reports
contain per-arm, per-scenario and per-attempt measurements, with matched partial
comparisons and explicit pending values. Public export derives allowlisted
measurements from the original artifacts and publishes source fingerprints.
No cost, time or quality benefit is established before qualified measurements.

See the [example](../../examples/software_factory/README.md),
[reproduction instructions](../../examples/software_factory/evals/flask/REPRODUCE.md)
and [report layout](../../examples/software_factory/harbor/README.md).

The Python profile indexes the original implementation, tests and documentation
for every Loom arm. Its compiler tree uses the same public text roots. Hidden paths
and binary fixtures stay local; native repository exclusions match the public text
exporter's hidden-path rule. The verifier accepts implementation changes only; index
coverage does not widen the candidate artifact boundary. Historical source hashes
cover the indexed text at each revision. Expanded coverage is recorded separately
from cohorts whose index held only implementation files.
