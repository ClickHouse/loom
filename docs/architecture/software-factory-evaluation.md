# Software factory evaluation

Harbor owns historical software-factory trial scheduling, Docker environments,
artifact transfer and independent verification. The factory is a custom external
agent. Its model client, prompts, source allowlist and retrieval policy belong
to the factory, so changing evaluation infrastructure does not select a different
coding agent. Evaluation dependencies are pinned separately from the server.

Each task freezes a repository revision, dependency image, source fingerprint
and pytest selectors. Agent and verifier images share that dependency image.
The agent image contains base tests. The verifier image contains the independent
hidden tests. The factory can read the same public base-revision implementation,
docs, examples and test sources in every arm. Other repository text is read-only;
only implementation source can enter the candidate patch. List, read and search
tools discover this evidence without exposing Git objects or verifier inputs.
The factory can request fixed visible checks; it has no shell tool or verifier capability.
An ordinary read outside the repository allowlist returns a refusal without
content so the model can correct the path. Native authorization and index-scope
failures still stop the attempt.

The common prompt identifies the index boundary: Loom code tools cover only
`source_paths`; ordinary search and read also cover the public repository evidence.
In the two memory arms, all five optional code reads request lessons from the
attempt's bound memory scope, including `code.enclosing`. The memory audit checks
that scope on every code read. Code-only arms disable memory tools and lessons;
the four Loom arms share the same code-tool schemas.

Ordinary search is available to every arm with optional RE2 regular expressions,
literal matching by default, path prefixes and result pagination. Optional Loom
grep accepts the same `regex` flag. Both tools document RE2 alternation syntax.
The standalone controller pins the engine alongside its evaluation dependencies.

Memory search keeps the native abstention reason, retrieval confidence and risk.
Hits keep similarity, rank components, verification and source class. These fields
describe retrieval and provenance; similarity is not a fact's truth probability.
Namespace and source-path checks still apply before the model receives a result.

An elided definition keeps its navigation card, separate from verified bodies.
Cards are accepted only for unchanged allowed files with valid spans. They never
count as body inspection. Symbol reads accept either a name or a native symbol
definition identifier, so the model can follow a definition card without repeating
name discovery. Search cards can identify chunks; their native `next` or name
guides the follow-up instead.

The agent publishes candidate source as JSON. The separate verifier checks the
base fingerprint, exact source allowlist, file sizes and candidate fingerprint.
It reconstructs its own checkout and overlays only those implementation files.
It runs pytest without provider credentials or network access. A successful
reward requires hidden checks, the agent's final visible check and normal agent
completion. Missing or malformed test results are infrastructure failures.

The cohort protocol freezes inputs before inference, including separate hashes
for editable implementation and read-only repository evidence. It disables retries and
concurrent trials and preserves the paired task order. No-Loom trials construct
no Loom backend and receive no Loom tool definitions. Optional Tree-sitter and SCIP
retrieval retain their model-selected call and dispatch counters. Read-only task tokens,
repository and revision checks, and index fingerprints constrain retrieval.

Every arm receives one final contract-review turn after the model first finishes
its patch. The common review text presents the consolidated candidate diff and
asks the model to inspect gaps, revise and check the result with its existing
tools. It supplies no verifier feedback or additional project knowledge. The
review stays in the attempt's conversation; it is a deliberate second pass by
the same model. Its requests, tokens, tool calls, cost and time remain included.
The journal records the candidate state and diff hash. The fairness audit
requires one review and reconstructs the exact subsequent provider messages.
Four named index-status caches and the memory-link checked-head marker are
excluded from the source fingerprint;
unknown state keys and every source table remain covered.

The study retains raw Harbor results, factory event journals, model request
receipts, Loom request identifiers and ATIF trajectories. Its scheduled-attempt
report includes invalid rewards and infrastructure failures, alongside Harbor's
valid-reward statistics. Partial cohorts remain partial. Generation estimates,
backend provider charges and unpriced infrastructure are separate quantities.
Timing includes environment setup and teardown, artifact transfer, independent
verification and index audits, with native phase timing retained for diagnosis.

The Python adapter keeps the existing factory runtime and uses Harbor's native
Docker and verifier interfaces. This replaces the factory's custom live
scheduler without adding evaluation machinery to the server's dependencies.
Recorded cohorts remain separate and are never pooled with a migrated cohort.

Live graders also expose a bounded, self-contained pytest regression tool in
every arm. Authored tests run as a nonroot sandbox child in scratch space,
outside the candidate source artifact. Their failures guide revision; they
never change the independent verifier's task-success criteria or evaluator
test inputs. The offline expression grader exposes no such execution tool.

A frozen Compose policy limits each agent and verifier to 64 processes and no
network, drops capabilities except identity changes and process-group cleanup,
and disables privilege escalation. Task configuration sets one CPU and 512 MiB.
The agent receives the job overlay. The separate verifier carries the same
policy in its task-owned Compose definition because Harbor clears job overlays
when creating that environment. Both copies are covered by the protocol hashes.

The example is repository-first: `examples/software_factory/factory/` holds the
shared agent, `harbor/` holds execution and reporting, and `evals/flask/` holds
the current Python E2E evaluation. Its suite lock declares a language list and
source roots; a repository can span languages without splitting one task into
separate language benchmarks. Harbor loads the frozen suite adapter explicitly.
The Flask profile owns pytest grading and Python compiler evidence. Authored
Python smoke fixtures live under tests and are not repository evaluations.

## Historical memory treatment

The Flask historical issue study uses the same three real issues in five
arms. Each optional memory arm receives 30 curated historical facts, 14 linked to
code, in a private namespace. Only these two arms expose optional memory tools
and code-linked lessons. Code-only arms receive no memory tools or namespace. The issue,
source, ordinary tools, model and tests remain paired. No historical facts are
inserted into the prompt; the model chooses memory and code tools.

Historical source hashes and publication/edit cutoffs qualify before ingestion.
Target discussions, reference fixes and hidden requirements cannot supply memory.
Native write receipts, exact read-back values, versions and content hashes prove
the history exists. The indexed source advances across real public revisions;
native workers must flag changed anchors before a source-backed memory update
relinks fresh. No synthetic source tree supplies the task. This represents
replayed historical knowledge, rather than autonomous elapsed learning.
Qualification compares native fingerprints for each linked symbol across the
two revisions. An edit elsewhere in its file does not make an unchanged symbol
stale. The qualifier uses the memory worker's complete declaration fingerprints,
rather than retrieval chunk hashes. Native file links track existence and moves.

The comparison measures the combined contribution of historical information and
its retrieval. Linking in isolation needs an additional matched-facts control.
The harder task selection precedes its solver outcomes. Earlier development
outcomes informed harness changes, and three tasks cannot establish generalization.

The profile lock pins the solver and blind-judge model for every arm. The shared
ledger prices that model and the resolved deployment model separately, including
cache writes and reads. Unknown models refuse dispatch. A model change starts a
new cohort; reports do not pool results across models.
