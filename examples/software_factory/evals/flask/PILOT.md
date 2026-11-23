# Factory comparison protocol

Compare five arms on the same historical Flask issue and base revision:
`baseline`, `loom-treesitter`, `loom-treesitter-memory`, `loom-scip`, and
`loom-scip-memory`. Claude chooses every Loom call. Baseline has no Loom client or
schemas. Every arm uses the same native coding agent, issue, model and test capabilities.

Three issues and three repeats produce 45 primary attempts. Add one baseline A/A
pair per issue for 51 attempts. Freeze seed 741 and retain the interleaved arm
blocks and balanced positions. Retain nonuse, failures, invalid rewards and
unknown costs. Analyze paired task clusters and report A/A controls separately.

For a five-attempt development check, pass `--pilot-task pallets__flask-4230` to
`plan` and `run`. The selected issue runs once per arm with unchanged grading and
history. Its task and marker are frozen in the launch and schedule; a truncated
full schedule is rejected. A pilot has no A/A pair or confidence interval. Keep
all pilot outcomes and hold the full matrix until the development target is met.
These reused development issues are not held-out confirmation data.

Task selection preceded solver outcomes on these three harder issues. Earlier
development outcomes informed the harness. The report discloses both facts.
Historical notes must have independent public provenance and precede the task's
cutoff. Target discussions, fixing PRs, reference patches, hidden requirements and
later information cannot supply memory. Freeze the corpus before inference and
never revise it based on that run's results.

The memory treatment adds 30 short historical notes, 14 linked to code. Baseline and code-only arms receive no historical memory. Every Loom arm sees
the same native coding roster; only memory arms have a historical memory namespace
and write permission. Their code tools may return linked lessons.
The treatment therefore measures historical knowledge plus retrieval. A claim
about linking alone requires another control with identical facts.

Each memory-enabled attempt has a private mutable memory namespace. Code-only and memory arms
share their certified read-only source index for that case and tier. Parser and
compiler tiers index the same source allowlist. Native memory writes, exact reads,
current code links, source hashes and serving build must qualify before inference.
The public source advances from an earlier real revision to the task base.
Native workers must flag changed anchors before a source-backed memory revision
relinks fresh. Code-only arms receive no memory namespace. This replays past knowledge
and code evolution, rather than elapsed autonomous learning.

Freeze source, tasks, evaluator inputs, model, prices, images, schema exposure,
prompts, provider limits and all namespace identities. Require base controls to
fail and reference controls to pass the hidden verifier. Harbor retries are disabled. Claude Code retains its native provider behavior
and ordinary checkout tools.

Report hidden success, fixed-only blind judgments, full-suite regressions, total
agent and Loom provider cost, agent time, full end-to-end time, tokens, agent tests,
Loom requests and persisted memory writes. Judge and setup costs are separate.
Setup writes are not agent writes. Missing billing evidence is unknown. All chart
bars retain the scheduled denominator and distinguish pending cells.

The targets are all nine primary attempts solved per arm and at least 20% lower
provider cost and agent time than paired baseline. Three task clusters cannot
establish a general win. Historical results and the current frozen run remain
separate. Follow [REPRODUCE.md](REPRODUCE.md) for the executable route.
