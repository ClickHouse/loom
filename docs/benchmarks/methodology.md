# Benchmark methodology

This page covers what the harnesses compute, how a delta is judged against noise, when
two numbers compare, and where the numbers are biased. The rules live in code:
`bench/_gate.py` (shared gate), `bench/bootstrap.py`, `bench/judge_calibration.py`,
`bench/bench_namespaces.py` and `bench/failure_taxonomy.py`
([source entry points](#source-entry-points)).

## Recall and answer accuracy are separate axes

**Recall@K** asks whether a relevant memory landed in the top K. **Answer accuracy** asks
whether the synthesized answer is correct. One can saturate while the other stays low.
`longmemeval.py` prints recall@K and judged accuracy as separate metrics;
`coding_agent_qa.py` prints R@5 / R@10 / MRR apart from judged QA accuracy. A headline
from an embedder with no reader and no judge is a recall figure. Do not set it beside an
accuracy number.

## Task categories

Each harness keeps its source's category labels, so a result reads per category.

| Harness               | Categories                                                                                                                                                      |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `locomo_qa.py`        | 1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial. `overall_comparable` covers 1–4 (the published column); `overall_all` scores 5 as abstain. |
| `longmemeval.py`      | The dataset's `question_type` (`--question-type`); `--stale-check` labels wrong knowledge-update answers that returned the superseded value.                    |
| `memoryagentbench.py` | Accurate retrieval, test-time learning, long-range understanding, conflict resolution.                                                                          |
| `beam.py`             | Ten memory abilities, graded with the dataset's rubric judge; event ordering adds Kendall τ_b × F1.                                                             |
| `membench.py`         | Participation and observation scenarios at factual and reflective levels.                                                                                       |
| `code_intel_qa.py`    | Row `type`: `symbol_def`, `callers`, `conceptual`, `hub`, `history`, `test_set`, `abstain_probe`; plus `lane` and `facets`.                                     |
| `coding_agent_qa.py`  | Bug-fix landing, infra fix, refactor location, flaky test, perf leak, schema migration, multi-session causal, temporal, team convention.                        |
| `op_planning.py`      | Each task's `class`: `instruction_type` × `structure`.                                                                                                          |

## Ground truth

| Kind                            | Where                                                                                                | How it is built                                                                                                                    | Weakness                                                                       |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Substring gold keys             | `bench/datasets/synthetic_memory_qa.json`, `extraction_regressions.json`, `graph_regressions.json`   | Hand-written; a hit is any gold string in the returned `memory_key` or excerpt (case-folded, `_` as space)                         | Lenient on purpose; a verbose excerpt can match by accident                    |
| Published gold answers          | Third-party datasets ([datasets](./running.md#datasets))                                             | The dataset's answer and evidence labels                                                                                           | Inherits the dataset's label errors and saturation                             |
| Hand-labelled query → key pairs | `retrieval_recall.py`, `plane_ablation.py`, `hard_distractors.py` `--dataset`                        | A consumer labels queries against an existing corpus                                                                               | Small sets; labels stay out of the repository                                  |
| Labelled by use                 | `relied_on_set.py` → `bench/datasets/relied_on/`                                                     | A search whose returned keys an agent later relied on (`used` / `used_cited` events) inside a short window                         | Joined by time, not id; favours what the current ranking already surfaced      |
| Mined from merged PRs           | `bench/fixtures/code_intel_mined_*.jsonl`, `code_intel_history.jsonl`, `code_plane_ablation.py mine` | Query = issue or PR title; gold = changed `src/` files. Rows whose text names a gold path stem are dropped, and the share reported | The index contains every fix: absolutes are an upper bound, read paired deltas |
| Model-authored, model-verified  | `bench/fixtures/code_intel_oss.jsonl`                                                                | A third-party corpus whose queries and paths a model wrote and a model judge checked                                               | A miss may be the label's; `secondary` paths are never credited                |

Structural code gold (`symbol_def`, `callers`) is exact but was written from knowledge of
the upstream repository. After every fresh backfill, run once without gates, read the
mismatches and correct the gold: a wrong gold is a corpus bug.

Judge calibration builds its own probes (`build_probes`): an item's gold phrased as a
reader answers is a known-correct probe; another item's gold from the same category is a
known-wrong probe. Adversarial items are excluded.

## Judges

The reader and judge are part of the instrument, not the system under test.

- **Pinned per harness.** `longmemeval.py` and `coding_agent_qa.py` read with
  `gpt-4o-mini` and judge with `gpt-4o-2024-08-06` (the LongMemEval judge snapshot).
  `locomo_qa.py` reads with `gpt-4.1-mini` and judges with `gpt-4.1`.
  `BENCH_READER_MODEL` / `BENCH_JUDGE_MODEL` override them and print a comparability
  warning; `OPENAI_BASE_URL` points at a compatible gateway.
- **Grading Loom's answer.** `longmemeval.py --answerer loom-ask` and
  `locomo_qa.py --reader ask` grade `memory.ask`; `--ask-arms` compares `memory.ask`
  configurations on one ingest.
- **Rubrics are files.** `bench/rubrics/entailment.md` is the NLI prompt
  `dialogue_consistency.py` loads. `beam.py` uses the dataset's rubric judge.
- **Agreement.** `locomo_qa.py --second-judge-model` re-grades a sample and reports
  agreement and Cohen's kappa.

Lexical scores exist only to sit beside published lexical tables.
`longmemeval.py --lexical-scores` adds token F1, BLEU-1 and ROUGE-L on a **simplified**
answer (`--simplify-model`, default the judge; else a labelled heuristic). Without
simplification they grade verbosity: a wrong date can outscore a correct sentence
([reference values](./results.md#lexical-scoring-reference-values)). They never replace
the judge.

## Metrics

| Metric                     | Definition                                                                                                                                                  | Computed in                                      |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| recall@K                   | Share of questions with a gold hit in the top K                                                                                                             | `quality.py` `summarise`, `retrieval_recall.py`  |
| MRR                        | Mean of 1 / rank of the first gold hit (0 when none)                                                                                                        | same                                             |
| rank@1                     | Share of questions whose gold ranked first                                                                                                                  | same                                             |
| precision@K                | Gold hits among returned hits, averaged                                                                                                                     | `quality.py`                                     |
| `best_cos_rank`            | Rank of the highest-cosine hit; a gap from rank 1 means fusion put a weaker match first                                                                     | `retrieval_recall.py`                            |
| exact@1, caller-set F1     | Structural code exactness                                                                                                                                   | `code_intel_qa.py`                               |
| abstain precision / recall | Recall = probes abstained on / probes; precision = probes abstained on / all abstains                                                                       | `code_intel_qa.py`                               |
| `placebo@k`                | Per question, as many paths drawn (seeded) from the pool the system returned across all conceptual questions. Recall below it is pool coverage, not ranking | `code_intel_qa.py`                               |
| tokens per answer          | Mean / p90 `tokens_returned` per answered question                                                                                                          | `code_intel_qa.py`, acceptance benches           |
| judged accuracy            | Majority label CORRECT over repeats                                                                                                                         | `_gate.py`                                       |
| flip rate, discordance     | Within-arm share of repeated items whose labels disagreed; between-arm share of pairs whose majority labels differ                                          | `_gate.py` `flip_rate`, `GateResult.discordance` |
| failure stage              | `extraction`, `graph_resolution`, `retrieval_ranking`, `compression_loss`, `reader`, `not_verbatim`                                                         | `failure_taxonomy.py`                            |

The failure stage is computed per missed item, in pipeline order: not recalled is
`retrieval_ranking`; gold in a retrieved excerpt but answered wrong is `reader`; gold only
in the raw evidence behind a retrieved memory is `compression_loss`; gold nowhere
verbatim is `not_verbatim`. `lme_diagnose.py` dumps LongMemEval misses with these labels.
A `reader` regression under unchanged retrieval is the reader's.

Request failures are excluded from ratios, never counted as misses (`code_intel_qa.py`
prints `errors(excluded)`). `quality.py` aborts on any HTTP failure.

## Baselines

Loom compares against techniques and its own arms, under the same instrument on the same
ingest.

| Baseline                                                          | Where                                                                              |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Lexical-only, lexical plus query expansion, vector-only retrieval | `make bench-code-legs`: each a paired B arm against the hybrid default             |
| Summary-only memory                                               | `msc_dmr.py` `digest` arm beside `ask`, `search`, `search_recall`, `search_reader` |
| Store verbatim vs extract facts                                   | `memoryarena/` and `memorydata/` `verbatim` / `extract` modes                      |
| Long context, BM25, plain dense retrieval                         | `memoryarena/memory_server.py` arms                                                |
| Coding agent with the plugin off                                  | `code_plane_ablation.py` arm A                                                     |
| Bulk file reads                                                   | `code_shunt_tokens.py` "cold" arm                                                  |
| Brute-force nearest neighbours                                    | `vector_tenant_recall.py`                                                          |
| One strong or one cheap synthesis model                           | `route_replay.py` strong, cheap and cascade arms on frozen retrievals              |
| The source system's native ranking                                | `clickgap_migration_validate.py`: the export's `recall_count` order                |
| Per-plane ablation                                                | `plane_ablation.py` `execution_planes` masks                                       |

## Statistics

### Paired, clustered bootstrap

A ranking change ships on the **lower bound** of the paired delta, not the mean.
`paired_bootstrap` takes per-item deltas (B minus A), resamples whole clusters with
replacement (10,000 resamples, `seed=7`), and reads the 2.5th and 97.5th percentiles. A
gain counts only when the lower bound is above zero. Items about one mechanism move
together, so harnesses pass a cluster key: a conversation, a question type, or for code
the first two path segments (`cluster_of_path`). Too few clusters make the interval too
narrow; read `clusters` beside the bound. `code_plane_ablation.py` adds a paired Wilcoxon
signed-rank test over per-task means across seeds.

### Repeats and the majority label

The reader re-rolls even at temperature 0, and retrieval inside synthesis re-decomposes
the query. `--repeats N` asks and judges each item N times on the same retrieval and keeps
the **majority** label. A tie is WRONG; an item is ERROR only when every run errored.
Gate runs use three repeats (`GATE_REPEATS`). `identical_share` over the repeats'
`top_keys` shows whether a flip was the reader's or the retrieval's. Repeats cut label
noise ([simulation](./results.md#majority-voting-on-simulated-reader-noise)); only more
items cut sampling noise.

### The verdict

`paired_gate` runs the bootstrap over the arms' majority labels:

| Verdict      | Condition                                                                                                                                                        |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pass`       | the 95% lower bound clears `--gate-min-delta` (0 by default; a negative value is a non-inferiority gate)                                                         |
| `fail`       | the 95% upper bound is below the threshold                                                                                                                       |
| `unresolved` | the interval straddles it. Inside the null band (`--gate-band`, default `DEFAULT_BAND_PP` = 4.0 pp) that is the noise floor; outside it the run is under-powered |

Items unstable within either arm's repeats are also reported as a stable-only pair.

**Measure your own band.** The default came from one harness and one reader. Run the same
configuration as arms A and B with `--repeats 3 --gate`, and pass the result's wider side
(`band_from_null`) as `--gate-band` on the real comparison.

### How wide the interval is

Width depends on how many paired items the arms disagree on, not only on n. These rows are
`paired_bootstrap` on fixed deltas (+1 = B right and A wrong, −1 the reverse, 0 = agree):

| n                    | Wins / ties / losses             | Mean delta | 95% interval      | Reading                                        |
| -------------------- | -------------------------------- | ---------- | ----------------- | ---------------------------------------------- |
| 60                   | 1 / 59 / 0                       | +1.7 pp    | +0.0 .. +5.0 pp   | One question; the bound does not clear zero    |
| 30                   | 2 / 26 / 2                       | 0          | −13.3 .. +13.3 pp | A same-config pair: a 13-point swing is inside |
| 60                   | 5 / 50 / 5                       | 0          | −10.0 .. +10.0 pp |                                                |
| 164                  | 13 / 138 / 13                    | 0          | −6.1 .. +6.1 pp   |                                                |
| 500                  | 40 / 420 / 40                    | 0          | −3.6 .. +3.6 pp   |                                                |
| 60                   | 6 / 52 / 2                       | +6.7 pp    | −1.7 .. +16.7 pp  | A "7-point gain" that is unresolved            |
| 60                   | 8 / 50 / 2                       | +10.0 pp   | +0.0 .. +20.0 pp  | The bound only reaches zero: not a pass        |
| 500                  | 40 / 440 / 20                    | +4.0 pp    | +1.0 .. +7.0 pp   | A smaller gain that passes because n is larger |
| 60, 6 clusters of 10 | 8 / 50 / 2, wins in two clusters | +10.0 pp   | +0.0 .. +30.0 pp  | Correlated items widen the interval            |

```bash
.venv/bin/python - <<'EOF'
from bench.bootstrap import paired_bootstrap
def row(w, t, l, clusters=None):
    d = [1.0] * w + [-1.0] * l + [0.0] * t
    print(paired_bootstrap(d, clusters).line(unit="pp", scale=100))
row(1, 59, 0); row(2, 26, 2); row(5, 50, 5); row(13, 138, 13); row(40, 420, 40)
row(6, 52, 2); row(8, 50, 2); row(40, 440, 20)
row(8, 50, 2, [f"c{i // 10}" for i in range(60)])
EOF
```

### Set sizes

Small sets cannot rank close arms.

| Set                                    | Size                         | One item is                       |
| -------------------------------------- | ---------------------------- | --------------------------------- |
| `synthetic_memory_qa.json`             | 15 questions                 | 6.7 points: catches collapse only |
| `code_intel_qa.jsonl` conceptual slice | 36 of 60 questions           | 2.8 points                        |
| Algorithm-gate fixtures                | 19 extraction, 2 graph cases | a smoke test                      |
| `code.ask` cache pairs                 | 42 paraphrase, 43 different  | more than 2 points                |

### Judge calibration

`bench/judge_calibration.py` measures the judge's true- and false-positive rates on the
probes. Observed accuracy is `TPR·p + FPR·(1−p)`, so a paired delta shrinks by Youden's
`J = TPR − FPR`. With `--judge-calibration <json>` the gate reports `Δ/J` and widens the
interval by the calibration set's own variance (delta method). A judge with `J ≤ 0`
carries no information; inputs pass through unchanged.

## When two numbers compare

All of these must match. If one differs, label the comparison non-comparable.

| Must match                | Why                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------- |
| Axis                      | Recall@K and judged accuracy are different metrics                                          |
| Reader and judge          | A stronger reader moves reasoning-heavy categories; a different judge moves labels          |
| Dataset split and slice   | LongMemEval oracle vs S, LoCoMo categories 1–4 vs 1–5, a lane vs the whole file             |
| Search mode               | The unpinned default is `LOOM_MEMORY_SEARCH_DEFAULT_MODE`; another mode is an ablation      |
| Embedder and server build | An embedder swap moves the whole similarity scale; `quality.py`'s `.meta.json` records both |
| Index revision (code)     | `code_intel_qa.py` names the `as_of.commit` that served the answers and the stale count     |
| Crediting                 | A `--credit` run reinforces what it reads                                                   |
| Accuracy denominator      | Leaving abstentions out flatters an arm that abstains more                                  |

**Abstentions.** The judged gate counts an abstention as wrong. `route_replay.py` reports
both: `accuracy` = correct / (correct + wrong), and `accuracy_abstain_wrong` = correct /
(correct + wrong + abstained), the gate's denominator. Errored asks are out of both. Quote
the one the verdict reads, and name it.

**Upper bounds and placebos.** Mined code lanes run against an index that contains every
fix (`fix_in_index` on each ablation task): quote paired deltas, not absolutes. Recall
that does not beat `placebo@5` / `placebo@10` is pool coverage. A tiny set makes the
placebo trivially strong. A set most arms score near the ceiling on cannot separate them;
read per-category results and the stable-only pair.

## Fake providers make scores meaningless

A checkout's `.env` runs `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, CI runs `fake-ngram-1024`,
and both run `LOOM_LLM_PROVIDER=fake` (the released stack embeds with `local` and keeps the
fake LLM). The fake embedder (`server/embedding/fake.py`) hashes character
trigrams into buckets, so similarity tracks spelling, not meaning. The fake LLM
(`server/llm/fake.py`) returns shaped JSON keyed off an anchor phrase, so pipelines run
without extracting anything real. Under them:

- recall measures string overlap between question and stored text;
- extraction benches measure the fake's output, not the prompt;
- the similarity gates use their scalar defaults, because adaptive calibration skips fake
  embedders.

A green gate on fake embeddings is worse than no gate: it reports safety that was never
measured. So `make bench-quality-gate` and `make bench-code-intel` are not in CI
(enforcing them needs a paid provider secret), and CI runs only the pure scoring code
(`pytest bench`) and the gate functions' unit tests (`quality.gate`: the boundary passes,
0/0 fails). Set `LOOM_REQUIRE_REAL_PROVIDERS=true` on a bench server so a fake or
key-less provider makes `/v1/health` report `degraded` before you spend a run.

## Reproducibility controls

| Control                    | Mechanism                                                                                                                                                                          |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Deterministic resampling   | `paired_bootstrap` and `paired_gate` are deterministic under `seed`; the gate records `resamples` and `seed`                                                                       |
| Deterministic samples      | Sample flags take a seed (`musique.py --seed`, `judge_calibration.py`, mined-lane samples)                                                                                         |
| Recorded server instrument | `quality.py` writes a `.meta.json` with `embedding_model`, `embedding_dim`, `llm_model`, `llm_provider`, `build_commit` from `/v1/health`; `code_intel_qa.py` names `as_of.commit` |
| Pinned scope               | Search harnesses send `namespace_scope: "this"`, so rows from other readable namespaces never enter the window. Ad-hoc probes must too                                             |
| Evaluation reads           | `measurement_fields()` sends `auto_touch: false` / `reinforce: false`; `--credit` opts in; on a namespace read before, pass `reinforcement_weight: 0` or use fresh rows            |
| Fresh namespaces           | One throwaway namespace per item or conversation, erased at the end                                                                                                                |
| Warm-up                    | `search_latency.py --warm 1` discards the first call per query, so embedding-cache misses do not count as retrieval                                                                |
| Token lifetime             | Mint a token that outlives the run ([running](./running.md#2-mint-a-token-that-outlives-the-run))                                                                                  |

### Bench namespaces

Harnesses that seed data create throwaway namespaces (`lme-…`, `locomo-…`, `mab-…`,
`qabench-…`; new harnesses use `bench-`). `bench/bench_namespaces.py`:

- registers each before its first write with `traffic_class = "bench"`, `kind = "bench"`
  and `capture_generation_io = True` (default);
- runs the work inside `erase_on_exit`, which erases every minted name with one purging
  erasure when the run ends, fails or is interrupted. The body sets `external: false`, so
  no external backend is touched;
- with `--keep-namespaces`, keeps them for a later re-score (`--namespaces-json`) and
  registers them `kind = "memory"` so the idle collector leaves them.

Erasure needs an org-admin token. On a refusal the harness logs the `loom namespaces-gc`
command. The `namespace_gc` worker purges idle `kind = bench` namespaces after
`LOOM_NAMESPACE_GC_IDLE_HOURS`, and minted names start with a prefix in
`LOOM_NAMESPACE_BENCH_PREFIXES`, so aborted runs are found too.

### HTTP policy

Harnesses post through `bench/_http.py` (`post` / `apost` return the response,
`post_json` / `apost_json` the body):

| Response                                      | Policy                                                                                                         |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 429                                           | waits out `Retry-After` (1 s when absent, clamped to 0.2–60 s) and retries; a wait does not use the 5xx budget |
| 5xx                                           | retried with a `0.5 × 2^n` s backoff until `attempts` requests have been sent                                  |
| any other status ≥ 400, or the last 429 / 5xx | raises `httpx.HTTPStatusError` with the start of the body                                                      |
| transport error                               | propagates; a harness that rides out a restart keeps its own loop                                              |

## Sources of bias

| Source                   | Effect                                                                             | Mitigation in the code                                               |
| ------------------------ | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Author-written sets      | The people tuning the system wrote the synthetic set and chose its threshold on it | Keep it a smoke gate; decide rankings on public or use-labelled sets |
| Lenient substring gold   | A verbose excerpt can contain a gold word by accident                              | MRR, rank@1 and precision@K beside recall                            |
| Upper-bound code lanes   | The index already contains every fix                                               | Header warnings, `fix_in_index`; quote paired deltas only            |
| Model-authored gold      | The third-party code-search labels were written and checked by models              | `secondary` paths never credited; paired reading only                |
| Labels by use            | A relied-on set contains only what the current ranking surfaced                    | Paired sweep on the bootstrap bound; one source among several        |
| Judge leniency           | An LLM judge passes topical-but-wrong answers at some rate                         | `judge_calibration.py` TPR/FPR, `--second-judge-model`               |
| Judge and reader drift   | A provider revision behind a pinned name changes labels                            | Pinned snapshots where the dataset names one; run both arms together |
| Self-reinforcement       | Crediting reads reinforce what the next pass ranks                                 | Evaluation reads by default                                          |
| Excluded errors          | If failures cluster on hard items, the ratio rises                                 | Error counts printed; `quality.py` aborts on any failure             |
| Leakage in agent benches | An agent finds the fix through the network or git history                          | The ablation's exclusion ledger drops such cells, fail-closed        |
| Saturation               | Categories most systems ace cannot rank arms                                       | Per-category results; the stable-only pair                           |
| Harness parity           | External testbeds wrap every query in their own template for every method          | The adapters keep that parity                                        |

## Source entry points

- `bench/_gate.py`: `paired_gate`, `verdict_for`, `majority_label`, `flip_rate`,
  `JudgeCalibration`, `measurement_fields`, `add_gate_arguments`.
- `bench/bootstrap.py`: `paired_bootstrap`, `cluster_of_path`.
- `bench/judge_calibration.py`: `build_probes`.
- `bench/bench_namespaces.py`: `register_body`, `erase_on_exit`, `erase_on_exit_sync`.
- `bench/failure_taxonomy.py`: `classify_answer_failure`.
- `bench/answer_scoring.py`: `token_f1`, `bleu1`, `rouge_l`, `cohen_kappa`.
- Tests: `tests/unit/test_bench_gate.py`, `test_bench_bootstrap.py`,
  `test_bench_judge_calibration.py`, `test_bench_namespaces.py`,
  `test_bench_failure_taxonomy.py`, `test_answer_scoring.py`,
  `test_bench_gate_help_renders.py`.

## Related

- [Running](./running.md) and [results](./results.md).
- [Retrieval and ranking](../concepts/retrieval-and-ranking.md): what moves a ranking.
