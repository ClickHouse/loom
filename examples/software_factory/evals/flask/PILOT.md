# Factory comparison protocol

Measure Claude-selected Loom against the same factory without Loom on pinned
public historical tasks. Provider calls and model-backed grading require an
approved frozen protocol. Offline scripted replays establish controller behavior;
they do not measure Loom quality, cost savings or adoption.

Use five study arms: `baseline`, `loom-treesitter`, `loom-treesitter-memory`,
`loom-scip` and `loom-scip-memory`. Claude chooses every Loom call. Baseline has
ordinary source and project-note tools; memory arms add native linked memory.
The public facts, issue, source, tests, model and limits match across all arms.
There is no host prefetch or enforced call. Source scopes match; SCIP adds compiler
declarations and references without extra repository content.

The primary cohort contains three real historical Flask issues. The separate
secondary cohort contains three authored maintenance scenarios with simulated
project history. Never pool them for a Flask headline. Each cohort uses three
repeats across five arms, plus one baseline A/A pair per issue: 51 attempts per
cohort, 102 total. Persist the schedule with seed 741, interleave arm blocks, and
balance arm positions within each cohort and across the full run. Retain failures,
nonuse and unknown costs. Report paired task-level bootstrap intervals and A/A
controls separately for each cohort.

The pinned dataset contains eleven Flask issues; the historical cohort retains
the existing three-issue pilot selection. It is not a random sample. Excluded
issues are unmeasured, rather than declared unsuitable. Three task clusters and
repeated attempts do not establish a general Flask performance claim.

Before inference, freeze the source, public tasks, independent grading inputs,
provider/model, prices, image digests, prompts, budgets, exact repository/index
identities and reader scopes. Qualify source and evaluator sandbox boundaries,
base/reference controls and tool behavior. Provider retries are disabled. The
public reproduction entrypoint and complete field contract are in `REPRODUCE.md`.

Report hidden task success, generation and retrieval cost, full attempt time,
agent-loop time, model-selected tests, automatic final tests, selected Loom tools
and physical dispatches separately. Agent-loop time excludes environment setup
and the independent verifier. Count native memory persistence receipts independently from selected write tools;
setup writes do not count as agent writes. Report independent judge outcomes and full-suite
regressions only when those checks actually ran; otherwise retain unknown values.
No authored regression or successful available test establishes final success.

The authored cohort seeds 100 short notes, including 40 code-linked notes. Three
policies change between public source revisions. Native stale-link observation
and revision receipts must qualify before inference. Every arm can search the
same current and archived facts as ordinary notes. This measures access to
simulated history, not autonomous learning or memory quality over elapsed months.
