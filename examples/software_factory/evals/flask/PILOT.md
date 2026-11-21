# Factory comparison protocol

Measure Claude-selected Loom against the same factory without Loom on pinned
public historical tasks. Provider calls and model-backed grading require an
approved frozen protocol. Offline scripted replays establish controller behavior;
they do not measure Loom quality, cost savings or adoption.

Use three study arms: `baseline`, `loom-treesitter` and `loom-scip`. There are two
engine modes: baseline and natural. Both Loom arms run in natural mode and expose
the same five optional read-only tools: symbol, grep, search, outline and enclosing.
Baseline receives no Loom client or tools. Claude chooses every Loom call; there
is no host prefetch, enforced call, minimum use count or cross-task memory write.
All arms receive the same public source, requirements, test capabilities, model
and budgets. The two Loom scopes contain the same pinned source files; SCIP adds
compiler declarations and references without additional repository content.

Freeze three tasks, three repeats and three arms for 27 primary attempts. Add six
baseline A/A attempts, one pair for each task, for 33 total. Persist the schedule
with seed 741. Each task uses balanced interleaved triplets: each arm appears in
each position once across three repeats. Shuffle whole triplets and A/A blocks.
Retain failures, nonuse and unknown cost. Compare both Loom tiers against baseline
and SCIP against Tree-sitter with paired task-level bootstrap intervals; report
A/A separately.
Three task clusters cannot establish a general win or statistical significance.

Before inference, freeze the source, public tasks, independent grading inputs,
provider/model, prices, image digests, prompts, budgets, exact repository/index
identities and reader scopes. Qualify source and evaluator sandbox boundaries,
base/reference controls and tool behavior. Provider retries are disabled. The
public reproduction entrypoint and complete field contract are in `REPRODUCE.md`.

Report hidden task success, generation and retrieval cost, full attempt time,
agent-loop time, model-selected tests, automatic final tests, selected Loom tools
and physical dispatches separately. Agent-loop time excludes environment setup
and the independent verifier. Memory rows written are zero because the allowed
Loom operations only read. Report independent judge outcomes and full-suite
regressions only when those checks actually ran; otherwise retain unknown values.
No authored regression or successful available test establishes final success.

A later chronological memory study would need a separate design and freeze.
Nothing in this comparison measures memory writing or learning across tasks.
