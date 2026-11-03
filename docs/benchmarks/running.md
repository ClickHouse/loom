# Running the benchmarks

Run every command from the repository root with the checkout's interpreter and the root
first on `PYTHONPATH`:

```bash
PYTHONPATH=$PWD .venv/bin/python bench/<harness>.py ...
```

`python bench/x.py` puts `bench/` on `sys.path`, not the root. With another checkout's
virtualenv on the path, the harness silently runs that checkout's code. The `make bench-*`
targets set `PYTHONPATH` this way (`BENCH_PYTHON` in the `Makefile`), and the commands
below assume it is exported. `python bench/<name>.py --help` lists a harness's flags.

## 1. Prepare a server with real providers

A bench on fake providers measures nothing
([why](./methodology.md#fake-providers-make-scores-meaningless)). The stock `.env` pins
fakes, so swap the provider block first:

```bash
# .env — the provider block the published results ran under
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
LOOM_REQUIRE_REAL_PROVIDERS=true   # a fake or key-less provider makes /v1/health "degraded"
```

Start it (`make server` runs `:7777` from source; `make dev` builds the compose stack) and
check what it resolved:

```bash
loom doctor                                  # llm_provider WARNs when the provider's key is missing
curl -s localhost:7777/v1/health | jq '{status, embedding_model, llm_model, llm_provider, build_commit}'
```

Run latency benches against an isolated server started from the branch under test, never
a shared one: its queue depth is part of what they measure. No harness records hardware.
For latency numbers, write down the host, the ClickHouse deployment (OSS single node or
Cloud) and `build_commit` yourself.

## 2. Mint a token that outlives the run

A token that expires mid-run turns later calls into errors, which show up as 429s on the
shared limiter rather than a clear failure. Long runs take hours, so mint with days of
lifetime and the bench traffic class:

```bash
export LOOM_BASE_URL=http://localhost:7777
export LOOM_API_KEY="$(loom mint-token --subject bench --org dev --namespaces '*' \
  --roles writer --traffic-class bench --ttl-days 3)"
```

`--traffic-class bench` puts requests in the bench rate bucket
(`LOOM_RATELIMIT_CLASS_FRACTIONS`, half the base cap by default). Erasing the run's
namespaces needs an org-admin role; with a writer token the harness logs the
`loom namespaces-gc` command, and the `namespace_gc` worker purges idle bench namespaces
anyway. Probe the token first:

```bash
curl -s -H "Authorization: Bearer $LOOM_API_KEY" -X POST "$LOOM_BASE_URL/v1/memory.search" \
  -d '{"org":"dev","namespace":"bench-probe","agent":"bench","query":"probe"}' | jq .abstain
```

Judged benches also need `OPENAI_API_KEY` in the bench process for the pinned reader and
judge. `OPENAI_BASE_URL` points them at a compatible gateway; `BENCH_READER_MODEL` /
`BENCH_JUDGE_MODEL` override them and mark the run non-comparable.

## 3. Run a target

### Offline (no server, no spend)

```bash
make test-unit                      # includes `pytest bench -q`: every harness's scoring core
make bench-code-intel-dry           # code_intel_qa scorer on a perfect synthetic retrieval
python bench/coding_agent_qa.py --dry
python bench/code_plane_ablation.py selftest
python bench/acceptance/code_agent.py --dry
```

These prove the scorers, not Loom. They exit non-zero when a self-check fails.

### Release and regression gates

| Command                                   | Runs                                                                 | Output                                                                                               | Exit 1 when                                      |
| ----------------------------------------- | -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `make bench-quality-gate`                 | `quality.py --top-k 10 --min-recall 0.80`                            | `/tmp/loom_quality_gate_<ts>.csv` and a `.meta.json` (instrument, recall, MRR, precision@10, rank@1) | recall@10 < `MIN_RECALL` (0.80), or no questions |
| `MIN_RECALL=0.85 make bench-quality-gate` | the same, stricter                                                   | as above                                                                                             |                                                  |
| `make bench-algorithm-gate`               | `extraction_eval.py` then `graph_eval.py` on the regression fixtures | `/tmp/loom_extraction_gate_<ts>.csv`, `/tmp/loom_graph_gate_<ts>.csv`                                | a harness fails                                  |
| `make bench-quick`                        | `longmemeval.py --limit 30 --shuffle --answer-judge --concurrency 1` | `/tmp/bench_quick_<ts>.csv`; per-category recall@30 and judged accuracy on stdout                    | a harness fails                                  |
| `make bench-quick LIMIT=100`              | a tighter interval                                                   | as above                                                                                             |                                                  |

A direct `quality.py` run does not gate (`--min-recall` defaults to 0); the Makefile sets
the floor.

`make bench-quick` reads the S split (`bench/datasets/longmemeval_s_cleaned.json`, about
47 sessions per item: 30 items ingest about 1,400 sessions, one extraction call each). The
oracle split is the cheap smoke run and is not comparable to S:

```bash
.venv/bin/python bench/longmemeval.py --dataset bench/datasets/longmemeval_oracle.json \
  --limit 30 --answer-judge --out /tmp/lme_oracle.csv
```

Run `make bench-quick` before changing the `LOOM_LLM_MODEL` or `LOOM_EMBEDDING_MODEL`
default, or making another LLM provider the default.

### Sweeps and A/B

```bash
make bench-matrix                               # search_mode × rerank × pool_unmatched on LongMemEval
BENCH_N=3 make bench-matrix                     # smoke: 3 items per config
.venv/bin/python bench/longmemeval.py --limit 100 --answer-judge --repeats 3 --gate \
  --answerer loom-ask --ask-arms '<JSON list of memory.ask overrides>'   # paired arms, one ingest
.venv/bin/python bench/locomo_qa.py --dataset bench/datasets/locomo10.json \
  --out bench/locomo_qa.csv                     # all 10 conversations; real provider calls
.venv/bin/python bench/judge_calibration.py --dataset bench/datasets/locomo10.json \
  --n 100 --judge-model gpt-4.1 --out bench/judge_calibration.json
```

`make bench-matrix` writes `bench/MATRIX_RESULTS.md` and per-config CSVs under
`bench/matrix_runs/`. Neither is gitignored; keep them out of a commit. Pass the
calibration JSON to any gated bench with `--judge-calibration`.

### Code index

The code benches need a server that indexes the fixture's repository (ClickHouse/ClickHouse
for the default fixture) and a real embedder. Org and namespace come from
`LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE` or `--org` / `--namespace`.

```bash
export LOOM_CODE_ORG=dev LOOM_CODE_NAMESPACE=<code namespace>
make bench-code-intel        # floors: symbol exact 0.95, callers F1 0.85, recall@5 0.75,
                             # abstain precision and recall 0.8, mean tokens 4000
make bench-code-legs         # lexical / lexical+expand / vector arms, each paired, with --track-misses
.venv/bin/python bench/code_intel_qa.py --dataset bench/fixtures/code_intel_qa_multilang.jsonl --verbose
```

Each floor has a variable (`MIN_SYMBOL_EXACT`, `MIN_CALLERS_F1`, `MIN_RECALL5`,
`MIN_ABSTAIN_PRECISION`, `MIN_ABSTAIN_RECALL`, `MAX_MEAN_TOKENS`). `--dump <file>` appends
one JSON line per (question, arm) so a paired run can be re-read without re-asking. On a
fresh index, run once without floors and correct stale gold
([ground truth](./methodology.md#ground-truth)).

```bash
.venv/bin/python bench/code_ask_cache_threshold/measure.py --env-file .env \
  --out bench/code_ask_cache_threshold/summary.json          # a few cents of embeddings
.venv/bin/python bench/code_shunt_tokens.py --base-url "$LOOM_BASE_URL" --token "$LOOM_API_KEY" \
  --git-dir <bare mirror> --prs 30 [--outline]
```

Benches that read ClickHouse directly (`code_grep_handler.py`, `code_grep_replay.py`,
`code_search_stage_latency.py` live mode, `code_plane_ablation.py`'s reader) load an env
file only when `--env-file` or `LOOM_ENV_FILE` names one, so a bare run never picks up a
deployment's `.env` credentials. `code_plane_ablation.py` has `mine`, `run` (`--driver dry`
spends nothing), `score` and `selftest` subcommands.

### Consumer replays

```bash
make bench-clickgap-replay REPLAY_DATA=/path/prs.jsonl     # floors MIN_PACK_HIT, MIN_SYMBOL_COVERAGE, MAX_MEAN_TOKENS
make bench-clickgap-golden GOLDEN_DIR=/path/tests/golden LOOM_API_KEY_FILE=/path/token \
  [GOLDEN_OUT=out.jsonl GOLDEN_MD=out.md]
```

Both need that consumer's data, which is not in this repository.

### Load and latency

```bash
make bench OP=memory.set C=10 D=30                    # scripts/ops/benchmark.py: p50/p95/p99, RPS, error codes
make bench OP=memory.search C=5 D=60                  # point --namespace at one a memory.set run filled
.venv/bin/python scripts/ops/benchmark.py --op memory.set --json > result.json
.venv/bin/python bench/search_latency.py --namespaces ns1,ns2 --queries queries.txt --warm 1
.venv/bin/python bench/locomo.py --base-url "$LOOM_BASE_URL" --corpus-sizes 1000,10000 \
  --concurrency 8 --search-queries 100 --out /tmp/locomo_latency.csv
```

`memory.set` and `knowledge.add` load runs write into a fresh `bench-…` namespace and leave
the rows. `scripts/ops/benchmark.py` records the base URL, build commit, schema version
and assigned traffic class; exit 0 means at least one call succeeded.

### Everything else

Every other harness is `python bench/<name>.py --base-url … --token …` plus its dataset
flag. [`bench/README.md`](../../bench/README.md) lists them by area with a "when to run
what" table.

## Datasets

The repository commits only data written for it: one synthetic memory QA set, fixtures
under `bench/fixtures/`, the skill-eval tasks and replays, the judge rubric and the
`code.ask` cache pairs. **Nothing third-party is committed.** `bench/datasets/` has its
own `.gitignore` that keeps only `synthetic_memory_qa.json`; downloads go there or
wherever `--dataset` / `--data` / `--data-dir` points. `tests/unit/test_public_copy.py`
skips paths with a `datasets` segment.

### In-house datasets and fixtures

| File                                                                             | Items                                                                           | Read by                                                                 | Provenance                                                                                                |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `bench/datasets/synthetic_memory_qa.json`                                        | 5 conversations, 15 questions                                                   | `quality.py`, `make bench-quality-gate`                                 | Invented conversations; no third-party text, no real person                                               |
| `bench/fixtures/extraction_regressions.json`                                     | 19 cases (`id`, `messages`, `expected`)                                         | `extraction_eval.py`, `make bench-algorithm-gate`                       | Hand-written regressions; a `_note` names the failure class                                               |
| `bench/fixtures/graph_regressions.json`                                          | 2 cases (`messages`, `expected_triples`, `as_of`)                               | `graph_eval.py`, `make bench-algorithm-gate`                            | Hand-written                                                                                              |
| `bench/fixtures/knowledge_retrieval/`                                            | 21 documents, 81 queries (5 kinds, 5 unanswerable)                              | `knowledge_retrieval.py`                                                | Synthetic: fictional products, runbooks, policies and notes                                               |
| `bench/fixtures/coding_agent_qa.json`                                            | 13 lessons, 16 questions                                                        | `coding_agent_qa.py`                                                    | Generic coding lessons                                                                                    |
| `bench/fixtures/dialogue_consistency_smoke.json`                                 | 7 dialogues                                                                     | `dialogue_consistency.py --dataset smoke`                               | Synthetic; a lost fact shows as a contradiction                                                           |
| `bench/fixtures/op_planning_tasks.jsonl`                                         | 42 tasks (retired ones in `op_planning_tasks_retired.jsonl`)                    | `op_planning.py`                                                        | Hand-written tasks with a gold plan and store assertions                                                  |
| `bench/fixtures/code_intel_qa.jsonl`                                             | 60 questions: 10 `symbol_def`, 4 `callers`, 36 `conceptual`, 10 `abstain_probe` | `code_intel_qa.py`, `make bench-code-intel`, `acceptance/code_agent.py` | Over the ClickHouse repository; structural gold grep-verified, conceptual gold checked on an index        |
| `bench/fixtures/code_intel_qa_multilang.jsonl`                                   | 27 questions                                                                    | `code_intel_qa.py --dataset …`                                          | Python / shell / SQL / CI lanes over ClickHouse's `tests/` and `ci/`                                      |
| `bench/fixtures/code_intel_qa_sqlsh.jsonl`                                       | 45 questions (mostly `test_set`)                                                | `code_intel_qa.py --dataset …`                                          | SQL and shell test lanes                                                                                  |
| `bench/fixtures/code_intel_qa_langfuse.jsonl`                                    | 341 questions                                                                   | `code_intel_qa.py --dataset …`                                          | Generated by `gen_code_intel_qa_langfuse.py` over a TypeScript monorepo                                   |
| `bench/fixtures/code_intel_hub.jsonl`                                            | 20 hub questions                                                                | `code_intel_qa.py`                                                      | Top-5 symbols by non-test call sites; regenerate when the index moves                                     |
| `bench/fixtures/code_intel_mined_issue.jsonl`, `code_intel_mined_pr_title.jsonl` | 687 and 300 rows                                                                | `code_intel_qa.py`                                                      | Mined from merged PRs by `code_intel_mine_prs.py`; upper-bound lanes                                      |
| `bench/fixtures/code_intel_mined_issue_ka.jsonl`                                 | 14 rows                                                                         | `code_intel_qa.py`                                                      | The keyword-agnostic slice (`code_intel_ka_slice.py`)                                                     |
| `bench/fixtures/code_intel_history.jsonl`                                        | 105 rows                                                                        | `code_intel_qa.py`                                                      | "What fixed issue N" questions mined from merged PRs                                                      |
| `bench/fixtures/code_intel_oss.jsonl`                                            | 304 rows                                                                        | `code_intel_qa.py`                                                      | An MIT-licensed third-party code-search corpus at a pinned revision; model-authored gold                  |
| `bench/fixtures/code_calibration_probes.jsonl`                                   | 20 query / positive pairs                                                       | code calibration (`server/code/app/admin.py`)                           | Over the ClickHouse repository                                                                            |
| `bench/fixtures/code_plane_ablation/`                                            | 2 tasks, arm A and arm C transcripts                                            | `code_plane_ablation.py selftest`, `acceptance/plugin.py`               | Synthetic transcripts that prove the scorer                                                               |
| `bench/acceptance/fixtures/shunt_decisions.json`                                 | 12 cases                                                                        | `acceptance/plugin.py`                                                  | Read / Grep decisions the plugin's code shunt must make                                                   |
| `bench/code_ask_cache_threshold/pairs.json`                                      | 42 paraphrase, 43 different, 22 different-identifier pairs                      | `code_ask_cache_threshold/measure.py`                                   | Written from a live `code.ask` cache's questions ([results](./results.md#codeask-answer-cache-threshold)) |
| `bench/skill_eval/tasks/*.md`, `replays/*.jsonl`                                 | 3 briefs, 2 recorded agents                                                     | `skill_eval.py`                                                         | See [skill eval](./skill-eval.md)                                                                         |

Each JSONL fixture opens with `//` lines stating its provenance, how its gold was built and
whether it is an upper bound. Read the header before quoting a number. `--dataset` picks
the file and each row's optional `namespace` routes it.

### Third-party datasets

Third-party corpora go in `bench/datasets/` and are never committed. The licences are the
ones each dataset states upstream. None is pinned to a revision, so record the one you
ran beside any number. LoCoMo and PerLTQA are non-commercial: research runs only.

| Dataset                    | Harness                                                                                                               | How to obtain                                                                                                                                                     | Licence (upstream)         |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| LongMemEval (S and oracle) | `longmemeval.py`, `lme_*.py`, `matrix.py`, `dialogue_consistency.py`                                                  | `longmemeval_s_cleaned.json` / `longmemeval_oracle.json` from the Hugging Face dataset `xiaowu0162/longmemeval-cleaned`; a missing file prints the `curl` command | MIT                        |
| LoCoMo                     | `locomo_qa.py`, `judge_calibration.py`, `k_curve.py`, `evidence_alignment.py`, `acceptance/harness.py`, `memorydata/` | `data/locomo10.json` from the `snap-research/locomo` repository                                                                                                   | CC BY-NC 4.0, research use |
| MemoryAgentBench           | `memoryagentbench.py`                                                                                                 | Export the Hugging Face dataset `ai-hyz/MemoryAgentBench` to JSONL (the docstring has the loop) or pass a parquet file                                            | MIT                        |
| MemBench                   | `membench.py`                                                                                                         | The released archive, unpacked so `--data-dir` holds `Categorical data/` and `NoiseData/`                                                                         | not recorded               |
| BEAM                       | `beam.py`                                                                                                             | `--fetch <scale>:<ids>` pulls chats from the benchmark's GitHub raw endpoint                                                                                      | not recorded               |
| MuSiQue-Ans dev            | `musique.py`                                                                                                          | `musique_ans_v1.0_dev.jsonl` from the MuSiQue release                                                                                                             | CC BY 4.0                  |
| MSC self-instruct          | `msc_dmr.py`                                                                                                          | `msc_self_instruct.jsonl`, the Hugging Face dataset `MSC-Self-Instruct`                                                                                           | Apache-2.0                 |
| NarrativeQA                | `narrativeqa.py`                                                                                                      | `--fetch N --data <file>` reads the test split through the Hugging Face datasets server                                                                           | Apache-2.0                 |
| PersonaMem-v2              | `personamem.py`                                                                                                       | Filled on first use into `--dataset-dir` (default `bench/datasets/personamem`)                                                                                    | CC BY 4.0                  |
| PerLTQA                    | `perltqa.py`                                                                                                          | `Dataset/en_v2/perltmem_en_v2.json` and `perltqa_en_v2.json` from the PerLTQA repository                                                                          | CC BY-NC 4.0, research use |
| Time-Dialog                | `time_dialog.py`                                                                                                      | `--data` and `--windows` files from the Memory-T1 repository                                                                                                      | MIT                        |
| Text2Mem                   | `text2mem_convert.py` → `op_planning.py`                                                                              | Convert `bench/data/benchmark/benchmark.jsonl` from the Text2Mem repository into `bench/datasets/op_planning_text2mem_en.jsonl`                                   | MIT                        |
| MemoryArena gym            | `bench/memoryarena/`                                                                                                  | Clone the gym and copy Loom's memory system in ([README](../../bench/memoryarena/README.md))                                                                      | not recorded               |
| MemoryData testbed         | `bench/memorydata/`                                                                                                   | Clone the testbed and add the adapter ([README](../../bench/memorydata/README.md))                                                                                | not recorded               |

`pi_llm.py`, `nested_kv.py`, `locomo.py` (LoCoMo-shaped turns for latency) and
`hard_distractors.py` generate their own corpus.

### Deployment data

These read data that belongs to a deployment and never enters the repository:

- `retrieval_recall.py`, `plane_ablation.py`, `hard_distractors.py`: a hand-labelled
  `--dataset` over an existing corpus; they seed nothing.
- `relied_on_set.py`: `loom.search_trace_log` and `loom.memory_usage_events`, written to
  `bench/datasets/relied_on/`.
- `tracker_similar_bench.py`: gold from the tracker source through Loom's read-only client
  (`--env-file` names the credentials).
- `clickgap_code_replay.py`, `clickgap_golden_replay.py`, `clickgap_migration_validate.py`:
  one consumer's PRs, golden fixtures or import.
- `search_latency.py --from-trace`, `ask_cost_rollup.py`, `session_ab.py`,
  `op_trajectories.py`, `ch_cost_by_op.py`: the deployment's traces, ledger and
  `system.query_log`.

### Slices

| Slice                     | Defined by                                                                                                                               |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| LongMemEval S vs oracle   | `--dataset`. Oracle (about 2 sessions per item) has near-zero retrieval pressure; S (about 47) is the default. Not comparable either way |
| LongMemEval by type       | `--question-type`, `--question-ids`                                                                                                      |
| LongMemEval robustness    | `--evidence-position`, `--context-scale` (variants built by `lme_variants.py`)                                                           |
| LoCoMo comparable         | Categories 1–4 (`overall_comparable`)                                                                                                    |
| LoCoMo acceptance presets | `acceptance/harness.py --slice conv26-30` (first conversation, first 30 questions) or `stratified-100` (first 10 of each conversation)   |
| Code conceptual slice     | The 36 `conceptual` rows of `code_intel_qa.jsonl`                                                                                        |
| Code keyword-agnostic     | Rows whose query shares no lexeme with any gold chunk (`code_intel_ka_slice.py`)                                                         |
| Code lanes                | `lane` on each row; filter with `--lane`                                                                                                 |
| MemoryAgentBench splits   | `--splits`                                                                                                                               |
| MuSiQue sample            | `--n` questions (default 200), deterministic under `--seed` (default 0)                                                                  |

## Where results land

| Harness                               | Default output                                                        |
| ------------------------------------- | --------------------------------------------------------------------- |
| Make targets                          | `/tmp/<name>_<unix ts>.csv` as listed above, or stdout                |
| `quality.py`                          | `--out` (default `bench/quality.csv`) plus `<out>.meta.json`          |
| `extraction_eval.py`, `graph_eval.py` | `--out` (default `bench/extraction_eval.csv`, `bench/graph_eval.csv`) |
| `matrix.py`                           | `bench/MATRIX_RESULTS.md`, `bench/matrix_runs/<config>.csv`           |
| `skill_eval.py`                       | `bench/out/skill_eval_<task>_<runner>_<ts>.json`                      |
| Most others                           | `--out` path, CSV or JSONL; a JSON summary on stdout                  |

`bench/out/`, `bench/*.csv`, `bench/longmemeval_*.json` and `bench/official/` are
gitignored. To keep a result, commit it with its instrument ([results](./results.md)) or
record it in Langfuse.

## Recording runs in Langfuse

`bench/langfuse_dataset.py` pushes a labelled retrieval set once, and
`retrieval_recall.py --langfuse-run <name>` records each run against it:

```bash
export LOOM_LANGFUSE_ADMIN_TOKEN='pk-lf-...:sk-lf-...'
# once per dataset: idempotent, item ids derive from the query text
.venv/bin/python -m bench.langfuse_dataset push /path/to/retrieval_qa.json
# per run
.venv/bin/python bench/retrieval_recall.py --dataset /path/to/retrieval_qa.json --top-k 10 \
  --namespace-scope this --langfuse-run "$(git rev-parse --short HEAD)"
```

Each query links to its trace (`X-Trace-Id`) and carries `bench_recall`,
`bench_reciprocal_rank` and `bench_top_similarity`; aggregate recall and MRR go on the run
metadata. The run is recorded before the pass/fail gate, so a failing run is still there
to open. `--langfuse-results out.json` writes the payload to a file.

## Re-baselining after an embedder change

A new embedder moves the whole cosine distribution, so every threshold and baseline
measured under the old one stops applying.

1. Re-embed: `loom reembed` (`--dry-run` first).
2. Recalibrate: with `LOOM_ADAPTIVE_THRESHOLDS` on (the default) the boot probe
   re-derives the search gates; `loom recalibrate` re-probes now. With it off you own the
   scalar gates.
3. Re-run and re-record the gates: `make bench-quality-gate` (set `MIN_RECALL` to the new
   value), `make bench-quick`, and `make bench-code-intel`.
4. Re-measure the `code.ask` cache threshold with
   `bench/code_ask_cache_threshold/measure.py --model <name>` and add it to
   `CACHE_HIT_THRESHOLDS` in `server/code/app/answers_entry.py`. An unlisted model takes the
   strictest measured value; `LOOM_CODE_ASK_CACHE_THRESHOLD` overrides all.

Editing `synthetic_memory_qa.json` also moves the quality baseline and needs the same
re-record.

## Related

- [Methodology](./methodology.md).
- [Configuration reference](../reference/configuration.md) for the provider settings.
- [Tokens and grants](../guides/tokens-and-grants.md) for `loom mint-token`.
- [Langfuse integration](../features/integrations/langfuse.md).
