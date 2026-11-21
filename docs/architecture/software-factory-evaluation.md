# Software factory evaluation

Harbor owns historical software-factory trial scheduling, Docker environments,
artifact transfer and independent verification. The factory is a custom external
agent. Its model client, prompts, source allowlist and retrieval policy belong
to the factory, so changing evaluation infrastructure does not select a different
coding agent. Evaluation dependencies are pinned separately from the server.

Each task freezes a repository revision, dependency image, source fingerprint
and pytest selectors. Agent and verifier images share that dependency image.
The agent image contains base tests. The verifier image contains the independent
hidden tests. The factory can read and edit implementation source and request
the fixed visible checks; it has no shell tool or verifier capability.

The agent publishes candidate source as JSON. The separate verifier checks the
base fingerprint, exact source allowlist, file sizes and candidate fingerprint.
It reconstructs its own checkout and overlays only those implementation files.
It runs pytest without provider credentials or network access. A successful
reward requires hidden checks, the agent's final visible check and normal agent
completion. Missing or malformed test results are infrastructure failures.

The cohort protocol freezes inputs before inference. It disables retries and
concurrent trials and preserves the paired task order. No-Loom trials construct
no Loom backend and receive no Loom tool definitions. Optional Tree-sitter and SCIP
retrieval retain their model-selected call and dispatch counters. Read-only task tokens,
repository and revision checks, and index fingerprints constrain retrieval.
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

The Flask development regression study uses the same three real issues in five
arms. Each optional memory arm receives 24 curated historical facts, ten linked to
code, in a private namespace. Other arms receive no seeded history. The issue,
source, ordinary tools, model and tests remain paired. No historical facts are
inserted into the prompt; the model chooses memory and code tools.

Historical source hashes and publication/edit cutoffs qualify before ingestion.
Target discussions, reference fixes and hidden requirements cannot supply memory.
Native write receipts, exact read-back values, versions, content hashes and fresh
code links prove the snapshot actually exists in the task's certified source scope.
It represents replayed historical knowledge, not autonomous elapsed learning.

The comparison measures the combined contribution of historical information and
its retrieval. Linking in isolation needs an additional matched-facts control.
Earlier outcomes on these development tasks were inspected, so their results are
reported as regression evidence rather than unseen-task generalization.
