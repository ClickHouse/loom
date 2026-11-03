# Benchmarks

Each harness measures a distinct signal and runs standalone — no shared
fixtures, no test runner. Every script's module docstring is its manual: the
protocol, the flags and a run command. This file is the inventory and the
short rules for reading a number; [docs/benchmarks/](../docs/benchmarks/overview.md)
documents the suite: what it measures, the method, datasets, running, committed
results, interpretation and limitations.

The unit and integration checks every change runs are in
[`CONTRIBUTING.md`](../CONTRIBUTING.md); this page covers the harnesses that measure quality.

Run a harness from the repository root with the root first on `PYTHONPATH`:

```sh
PYTHONPATH=$PWD .venv/bin/python bench/<harness>.py ...
```

`python bench/x.py` puts `bench/` on `sys.path`, not the root, so the
`bench.*` and `server.*` imports resolve through whatever else is on the path.
With another checkout's virtualenv, whose editable install adds that checkout,
the harness silently runs that checkout's code. The `make bench-*` targets set
`PYTHONPATH` this way (`BENCH_PYTHON` in the Makefile). A shared HTTP helper
lives in `bench/_http.py`: one JSON POST policy (Bearer token, 429 waits for
`Retry-After`, bounded 5xx retries, errors that carry the start of the response
body).

## Metric comparison hygiene

The full method is [docs/benchmarks/methodology.md](../docs/benchmarks/methodology.md);
how to read a number is [docs/benchmarks/methodology.md](../docs/benchmarks/methodology.md).
The rules every quoted number follows:

- **Recall@K and answer accuracy are different axes.** Never put them in the same
  comparison; the harnesses label them separately.
- **Same reader and judge, or no comparison.** A QA-accuracy delta only means
  something when both numbers came from the same reader and judge. F1 / BLEU-1 /
  ROUGE-L are computed on the simplified answer and never stand in for the judge.
- **Pin the search mode** and say which one you quote.

## Reading a result

- **The null band.** Two identical single-trial runs of a judged bench differ by
  ±3–4 points on ~160 questions; `bench/_gate.py` (`--repeats`, the paired bootstrap
  in `bench/bootstrap.py`, `--judge-calibration`) decides pass / fail / unresolved.
- **A bench read is an evaluation read.** Harnesses send `auto_touch: false` and
  `reinforce: false` (`measurement_fields()` in `_gate.py`) unless `--credit`.
- **Bench namespaces are throwaway** (`bench/bench_namespaces.py`, `erase_on_exit`).
- **Re-baseline after an embedder change**: a new embedder moves the distance
  distribution, so recorded thresholds no longer apply.
- **Small slices are noisy.** On the 36-question conceptual slice of
  `code_intel_qa.py`, deltas under 3 points are noise; read the bootstrap lower bound.

## Recall and answer accuracy

How well Loom surfaces the right memory for a question, and whether the answer
built from it is right.

- **`longmemeval.py`** — LongMemEval (500 questions; `-S` by default, the
  oracle split via `--dataset`). Recall@K and LLM-judge answer accuracy under
  a pinned reader and judge (`BENCH_READER_MODEL` / `BENCH_JUDGE_MODEL`,
  `OPENAI_BASE_URL` for a gateway). `--top-k-sweep` answers every item at
  several k on one ingest; `--answerer loom-ask` grades loom's own
  `memory.ask` instead of the pinned reader, and `--ask-arms` compares
  `memory.ask` configurations on the same ingest with a paired bootstrap.
  `--evidence-position` / `--context-scale` apply the robustness variants of
  `lme_variants.py`; `--stale-check` labels wrong knowledge-update answers
  that returned the superseded value.
- **`lme_rescore.py`** — lexical columns for an existing hypotheses file, no
  ingest, no reader, no judge.
- **`lme_diagnose.py`** — failure-mode dump for `longmemeval.py` misses, each
  labelled with a failure stage (`bench/failure_taxonomy.py`).
- **`matrix.py`** — sweep search mode × rerank × pool width on LongMemEval
  (`make bench-matrix`).
- **`locomo_qa.py`** — LoCoMo question-answering accuracy (10 conversations,
  1,986 questions, five categories). One namespace per conversation, one
  `memory.set_from_messages` per session in session-date order. Reader
  `gpt-4.1-mini` and judge `gpt-4.1` are pinned; `--reader ask` scores
  `memory.ask`, `--reader-prompt loom` hands the external reader the server's
  `reader_block`. Prints `overall_comparable` (categories 1–4, the published
  column) and `overall_all` (adversarial scored as abstain), evidence-span and
  evidence-distance buckets, plane mix, abstention both ways, deployment
  columns (latency, footprint, construction time) and, with
  `--second-judge-model`, judge agreement and Cohen's kappa. `--policy`
  compares write-policy arms with per-operation provenance. Dataset:
  `snap-research/locomo` `data/locomo10.json` in the gitignored
  `bench/datasets/`.
- **`evidence_alignment.py`** — within-query AUC of rank against gold
  evidence, and gold-session recall@k, on LoCoMo.
- **`k_curve.py`** — gold-evidence recall and context size by top_k, LLM-free.
- **`plane_precision.py`** — per retrieval plane, how many hits come from the
  gold evidence sessions.
- **`chunk_granularity.py`** — which raw-chunk unit (turns, characters,
  session, topic segment) finds the evidence.
- **`judge_calibration.py`** — the LoCoMo judge's TPR / FPR on answers of
  known truth, for `--judge-calibration`.
- **`memoryagentbench.py`** — MemoryAgentBench: accurate retrieval,
  test-time learning, long-range understanding and conflict resolution under
  the incremental protocol. `--memorize knowledge|extract`,
  `--answer reader|ask`; scores per competency next to build time, query time
  and reader tokens. `fact_consolidation.py` is its FactConsolidation task as
  a bi-temporal regression.
- **`membench.py`** — MemBench: participation and observation scenarios,
  factual and reflective levels, multiple-choice accuracy, evidence recall and
  precision, read/write seconds, and a capacity curve under `--noise K`.
- **`beam.py`** — BEAM: ten memory abilities on 128K–10M-token chats, graded
  with BEAM's rubric judge plus its event-ordering metric. `--fetch` pulls
  chats; `--dry` runs with no network.
- **`musique.py`** — MuSiQue-Ans multi-hop QA (200-question sample):
  supporting-paragraph recall@k reported separately from fixed-reader EM/F1.
- **`msc_dmr.py`** — Multi-Session Chat deep-memory retrieval, the public
  bench with a built-in summary-only control: `ask`, `search`, `digest`,
  `search_recall`, `search_reader` arms.
- **`knowledge_retrieval.py`** — `knowledge.search` on a small synthetic
  document corpus (`bench/fixtures/knowledge_retrieval/`): recall@1/5/10, MRR,
  nDCG@10 and all-gold recall per query kind (shared section layouts, error
  codes, paraphrases, old and current policy versions, multi-document
  questions), and the top-hit similarity on answerable and unanswerable queries; `--param KEY=VALUE`
  sets any search field the harness does not fix.
- **`narrativeqa.py`** — NarrativeQA through the knowledge plane:
  long-document QA with F1 / ROUGE-L, build time and tokens per question.
- **`time_dialog.py`** — Time-Dialog temporal reasoning over multi-session
  dialogues, with the published scoring ported.
- **`personamem.py`** — PersonaMem-v2: memories, memories + rendered profile,
  profile alone, under a fixed reader.
- **`perltqa.py`** — PerLTQA: per-character memory banks, anchor reproduction,
  reference recall and an optional correctness judge.
- **`pi_llm.py`** — proactive interference: an attribute updated n times,
  then asked for its current value; reports the stale-retrieval rate.
- **`dialogue_consistency.py`** — a dialogue run autoregressively through
  `memory.ask`, each turn graded by an entailment judge
  (`bench/rubrics/entailment.md`); consistency by turn index.
- **`retrieval_recall.py`** — ranking regression gate over a corpus that
  **already exists**; seeds nothing. Recall@k, MRR, rank@1 and
  `best_cos_rank`; `--min-recall` / `--min-mrr` gate; `--param KEY=VALUE`
  overrides any `memory.search` field. Request failures are excluded from the
  ratios, never counted as misses.
- **`relied_on_set.py`** / **`relied_on_sweep.py`** — a relevance set labelled
  by what agents actually relied on (search traces joined to usage events),
  and every ranking arm swept against it on the paired bootstrap bound.
- **`hard_distractors.py`** — near-copies of the gold that differ in the one
  fact that matters: recall, MRR, intrusion and cite score before and after.
- **`plane_ablation.py`** — the same questions under `execution_planes` masks:
  which retrieval planes earn their keep.
- **`retrieval_determinism.py`** — is `memory.search` a function of its
  inputs? Same questions N times, nothing written in between.
- **`quality.py`** — self-seeding recall@K by substring match, no judge cost;
  the release gate below.
- **`extraction_eval.py`** / **`graph_eval.py`** — frozen extraction and
  triple-extraction regressions (`make bench-algorithm-gate`).
- **`retention_curve.py`** / **`entity_drift.py`** — do seeded facts survive N
  periods of unrelated writes; how far an entity's summary moves between
  versions.
- **`okf_roundtrip.py`** — export a namespace as an OKF bundle, import it into
  a fresh namespace, and measure what was lost.
- **`forget_switch_eval.py`** — forget / switch / correction cases
  through `set_from_messages`.

External harnesses with loom plugged in live in their own folders:
`memoryarena/` (MemoryArena's gym; see its README) and `memorydata/`
(the MemoryData testbed; see its README).

- **`cross_tool_validate.py`** — 6-query cross-tool check. Writes as
  `agent='claude-desktop'`, reads as `agent='cursor'` through MCP: memory
  follows the user across tools. Mints its tokens with the checkout's signing
  key, so it needs an explicit `--base-url`; exits 1 under 80% top-3 recall.
- **`nested_kv.py`** — nested key-value lookup: where loom's multi-hop cliff
  is on identifiers that carry no semantics (`ask`, `search_k`, `chain`,
  `recall_chain` arms).
- **`memory_integrity.py`** — write-side quality: Memory Integrity (gold
  evidence turns cited by at least one memory) and False Memory Rate (a judge
  over each memory against its source turns), via `memory.evidence`.
  `--digests` audits session digests, `--derived-only` audits derived rows,
  and every day-precise relative date is checked against the server's
  resolver by arithmetic.
- **`supersession_audit.py`** — what each supersession did to the fact
  (restates, extends, corrects, …), from `loom.memory_versions`.
- **`conflict_resolver_long_session.py`** — one user's twelve sessions in a
  `newest_wins` namespace, against a server with the conflict resolver on and
  one with it off: the share of changing attributes left with one live value,
  stale top hits, false retirements and flags, and the resolver's calls, cost
  and drain time. The scenario and its ground truth are
  `conflict_resolver_scenario.py`.
- **`fama_forgetting.py`**, **`mutation_sequence.py`**,
  **`temporal_assertions.py`** — bi-temporal invariants: a closed validity
  interval is never surfaced after it closes and stays queryable inside it.

## Agents, operations and cost

- **`op_planning.py`** — did the agent pick the right loom operation (tool,
  required args, literal args, no extras), and does the gold plan leave the
  store in the right state? Tasks in `fixtures/op_planning_tasks.jsonl`, or
  the public Text2Mem set via `text2mem_convert.py`.
- **`op_trajectories.py`** — create / read / update / delete mix per task,
  from `agent_events`, grouped by run, session or pause.
- **`route_replay.py`** — freeze `memory.search` once, then replay the
  synthesis tier offline: strong, cheap and cascade arms, priced and graded
  against a reference.
- **`skill_eval.py`** — the memory guidance is graded by the namespace it
  produces: an agent runs a scripted investigation with the skill loaded and
  `namespace.audit` grades what it left behind (tasks in `skill_eval/tasks/`).
- **`procedure_replay.py`**, **`procedure_match_axes.py`**,
  **`procedure_explore_readout.py`** — the procedure plane: trace replay with
  the utility term, two-axis matching on real embeddings, and what showing a
  procedure did to a run.
- **`lesson_grade_attribution.py`** — do the lessons a run relied on go with
  graded passes?
- **`ask_cost_rollup.py`** — what a `memory.ask` outcome costs per consumer
  per day, joined to the `used` / `evidence` events.
- **`session_ab.py`** — cost per session on a consumer's own traffic, paired
  by PR / issue, from the agent's invocation ledger.

## Workload

Access patterns where vector-only memory falls down.

- **`aggregate.py`** — `memory.aggregate` over typed records landed by
  `memory.set_from_messages(record_schema=…)`.
- **`coding_agent_qa.py`** — coding-agent end-to-end QA plus fix-reuse ("does
  retrieval surface the prior fix at small k?"), under the same pinned reader
  and judge as `longmemeval.py`. `--dry` runs with no server and no LLM spend
  on `fixtures/coding_agent_qa.json`.

## Safety and privacy

- **`erasure_compliance.py`** — erasure canary: does a deletion reach every
  tier? `--behavioral` plants a canary per substrate, erases through the saga
  and drives every read-shaped op.
- **`memory_extraction_redteam.py`** — how much of a namespace a prompt-only
  attacker can pull out through the agent (RN / EN / EE / CER / AER per arm).
- **`personalization_skew.py`** — does personalization move attention between
  two otherwise-equal groups, or crowd candidates out with the user's history?
- **`sensitivity_leakage.py`** — does labelled content leave through ordinary
  use? Leakage normalised by opportunity.
- **`memory_poison.py`** — write authorization: injection survival, steering
  and extractor steering, beside the benign false-positive rate.

## Code plane

- **`code_intel_qa.py`** — the rollout gate for the code plane: structural
  exactness, conceptual recall, hub recall, abstention, token cost, per-lane
  facets (`make bench-code-intel`, `make bench-code-intel-dry`).
  `--compare-extra` runs a paired B arm with a bootstrap lower bound
  (`make bench-code-legs`); `--track-misses` asks the server where each missed
  gold path stood in each leg's pool.
- **`make bench-code-portability-dry`** — exercises every representative public
  repository fixture (polyglot, SQL/shell, OSS annotations and Langfuse) through the
  scorer in CI. Before a retrieval release, run those same datasets against complete
  indexed checkouts and keep the per-namespace output with the release evidence.
- Fixture generators: `code_intel_mine_prs.py` (issue-shaped and history
  lanes from merged PRs), `code_intel_ka_slice.py` (the keyword-agnostic
  slice), `gen_code_intel_qa_langfuse.py`.
- **`code_plane_ablation.py`** (with `agent_traj/`) — the same coding-agent
  harness with the plugin toggled, judged by what the agent actually read;
  recorded transcripts in `fixtures/code_plane_ablation/`.
- **`code_shunt_tokens.py`** — token cost of the bulk-read reflex against
  asking the index.
- Grep: `code_grep_replay.py`
  (wrong-abstain replay), `code_grep_handler.py`.
- Edges and oracles: `code_python_layouts.py`, `go_scip_gold.py`,
  `code_dependents_replay.py`.
- History and impact: `owners_precision.py`.
- Ingest: `code_search_stage_latency.py`.
- `code_ask_cache_threshold/` — re-measures the `code.ask` answer-cache
  threshold for the served embedder.

## Tracker

- **`tracker_similar_bench.py`** — `tracker.similar` per leg, paired per query.

## Consumer replays

- **`clickgap_code_replay.py`** — replays a PR-review bot's exact call
  sequence (context pack, then symbol / callers / impact per changed symbol)
  against historical PRs: pack hit rate, tokens, structural coverage, latency
  (`make bench-clickgap-replay`).
- **`clickgap_golden_replay.py`** — the bot's 30 frozen golden PR fixtures
  through loom's citation, impact and coverage checks, with a negative recall
  gate on rejected findings (`make bench-clickgap-golden GOLDEN_DIR=…`; the
  fixtures are private and not in this repository).

## Perf

Latency, throughput and storage at scale.

- **`search_latency.py`** — `memory.search` wall time by namespace size band,
  with the per-stage breakdown and event-loop lag every response carries.
  Point it at an isolated server from the branch under test, never at a
  shared process.
- **`write_rate.py`** — writes at a fixed rate (`memory.set` and
  `conversation.append` against a server, or the append's inserts in-process:
  batched by default, as `ch_batched_inserts` sends them, and committed with
  `--committed`, which sets the switch off) and reports latency and the ClickHouse
  parts each table got from `system.part_log`, with the `async_insert` value
  each conversation insert carried from `system.query_log`. Free with the fake
  providers. Writes go under a per-run org (`write-rate-<run>`); the in-process
  run deletes its conversation rows at the end, the server run keeps its rows,
  so point it at a private ClickHouse.
- **`ch_cost_by_op.py`** — ClickHouse cost per loom operation from
  `system.query_log`, joined through the `log_comment` tag every loom query
  carries (`"<op> <request_id>"` / `"worker.<name>"`).
- **`vector_precision.py`**, **`vector_index_health.py`**,
  **`vector_tenant_recall.py`**, **`ann_final_staleness.py`** — does a
  narrower vector change results; would an HNSW index prune anything (no
  table has one); do small tenants get the same top-k as brute force; does
  an HNSW index return stale versions under FINAL.
- **`vector_index_vs_exact.py`** — exact scan, codes-first and an HNSW index
  on synthetic vectors under a tenant filter: whether the index runs, its
  recall@10 and the bytes each read takes. Needs ClickHouse 26.8 or later and
  the `clickhouse-connect` package.
- **`search_switch_ab.py`** — a one-switch A/B on a running server.

`scripts/ops/benchmark.py` (not in `bench/`) load-tests a single op: "does
throughput cliff at N concurrent calls?".

- **`locomo.py`** — wire-level p50/p99 for `memory.set` and `memory.search`
  at 1k / 10k memories on LoCoMo-_shaped_ synthetic text. The accuracy bench
  is `locomo_qa.py`.

## Acceptance benches

`acceptance/` holds one runnable bench per consumer archetype — `code_agent`,
`memory_app`, `plugin`, `harness` — each against any deployment
(`--base-url --token --namespace`), scoring the precision of what loom serves
first and the archetype's economy second, with a one-screen pass / fail
report.

## Migration

Validating that a bulk import preserved retrieval quality. These evaluate an
already-loaded namespace; they do not seed.

- **`clickgap_migration_validate.py`** — the same for a `loom import-clickgap`
  load, whose export carries no embeddings: the source's native
  `recall_count` ranking is the baseline.

## Datasets

`datasets/synthetic_memory_qa.json` and the fixtures in `bench/fixtures/` are
committed and synthetic. Third-party corpora go in the gitignored `bench/datasets/`;
where each comes from and its licence are in
[docs/benchmarks/running.md](../docs/benchmarks/running.md#datasets). The non-commercial ones
(LoCoMo, PerLTQA) are for research runs only.

## Blocking a release on a retrieval regression

`bench/quality.py` seeds its own corpus, so it is the one bench suitable as a release
gate:

```bash
make bench-quality-gate                      # threshold 0.80
MIN_RECALL=0.85 make bench-quality-gate      # stricter
```

It is deliberately not in CI: CI runs fake providers, and recall measured there says
nothing about real retrieval. See [docs/benchmarks/running.md](../docs/benchmarks/running.md).

## Recording runs in Langfuse (run comparison)

`python -m bench.langfuse_dataset push <file>` pushes a dataset once;
`retrieval_recall.py --langfuse-run <name>` records each run against it, per query, so
Langfuse's run-comparison view shows which queries moved. Details:
[docs/benchmarks/running.md](../docs/benchmarks/running.md#recording-runs-in-langfuse).

## When to run what

| Question                                                                       | Run                                                                           |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| Did extraction regress on a standard benchmark?                                | `longmemeval.py` (slow, 500 questions)                                        |
| Does recall hold when the evidence is early, or the history is 2× longer?      | `longmemeval.py --evidence-position early --context-scale 2`                  |
| Where does loom sit on the published LoCoMo table?                             | `locomo_qa.py` (10 conversations, ~40 min, ~$5)                               |
| Did extraction prompt output drift?                                            | `extraction_eval.py`                                                          |
| Did graph/triple extraction drift?                                             | `graph_eval.py`                                                               |
| Why did query X miss?                                                          | `lme_diagnose.py` after longmemeval                                           |
| Is recall holding without paying LLM-judge cost?                               | `quality.py`                                                                  |
| Did ranking regress on a corpus that already exists?                           | `retrieval_recall.py` (recall@k + MRR + rank@1)                               |
| Did the code plane regress?                                                    | `code_intel_qa.py` (`make bench-code-intel`)                                  |
| Does throughput cliff under concurrency?                                       | `scripts/ops/benchmark.py` (not bench/)                                       |
| How much of a namespace can a prompt-only attacker echo out through the agent? | `memory_extraction_redteam.py` (RN/EN/EE per arm; `--max-rn-fraction` gate)   |
| Does personalization move attention between two equal groups?                  | `personalization_skew.py` (A(alpha)/A(beta) gap, intrusion; `--max-gap` gate) |
| Does the memory guidance leave a clean namespace?                              | `skill_eval.py` (graded by `namespace.audit`)                                 |
| Did I just regress cross-tool retrieval?                                       | `cross_tool_validate.py` (fast, 6 queries)                                    |
| Did the extractor write what the turns say, and every gold turn?               | `memory_integrity.py` (MI + FMR via `memory.evidence`)                        |
| Does aggregation still work?                                                   | `aggregate.py`                                                                |
| Does a coding agent get the right answer and reuse prior fixes?                | `coding_agent_qa.py` (`--dry` for the offline gate)                           |
| Does write/search latency degrade at scale?                                    | `locomo.py`                                                                   |
