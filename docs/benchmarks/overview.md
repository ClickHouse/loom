# Benchmarks overview

Ranking levers, similarity gates, cache thresholds and extraction prompts change
behaviour that a unit test cannot judge. Each one is decided by a harness under `bench/`
that runs over HTTP against a live server with real providers. This section covers the
[methodology](./methodology.md), [how to run](./running.md) the suite and where its data
comes from, the [coverage matrix](./coverage.md), the [results](./results.md) the repository reproduces, how to
[add a case](./adding-a-case.md), and the [skill eval](./skill-eval.md).

[`bench/README.md`](../../bench/README.md) lists every harness. Each script's module
docstring is its manual: protocol, flags and a run command.

## What the suite measures

Harnesses are standalone scripts: argparse plus an HTTP client, no shared fixtures, no
test runner.

| Area                     | Question                                                                                       | Main harnesses                                                                                                                   |
| ------------------------ | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Retrieval recall         | Did the right memory land in the top K?                                                        | `quality.py`, `retrieval_recall.py`, `k_curve.py`, `plane_ablation.py`, `hard_distractors.py`                                    |
| Answer accuracy          | Is the answer built from retrieved memory correct, under a pinned reader and judge?            | `longmemeval.py`, `locomo_qa.py`, `memoryagentbench.py`, `membench.py`, `beam.py`, `musique.py`, `msc_dmr.py`, …                 |
| Write-side quality       | Did extraction write what the turns say, and only that?                                        | `extraction_eval.py`, `graph_eval.py`, `memory_integrity.py`, `supersession_audit.py`                                            |
| Time and revision        | Is a superseded fact hidden after its interval closes and visible inside it?                   | `fama_forgetting.py`, `mutation_sequence.py`, `fact_consolidation.py`, `pi_llm.py`                                               |
| Agents and operations    | Did an agent pick the right op and leave the store in the right shape?                         | `op_planning.py`, `skill_eval.py`, `op_trajectories.py`, `procedure_replay.py`                                                   |
| Cost                     | What does an answer or a session cost?                                                         | `ask_cost_rollup.py`, `session_ab.py`, `route_replay.py`, `ch_cost_by_op.py`                                                     |
| Safety and privacy       | Does erasure reach every tier; can a prompt-only attacker extract or poison memory?            | `erasure_compliance.py`, `memory_extraction_redteam.py`, `memory_poison.py`, `sensitivity_leakage.py`                            |
| Code index               | Does the code index find definitions, callers and conceptual answers, and abstain on the rest? | `code_intel_qa.py`, `code_plane_ablation.py`, `code_shunt_tokens.py`, `code_ask_cache_threshold/`                                |
| Document parsing         | Does the extractor read a document's text, headings, tables and pages, and lose nothing?       | `docparse/` (offline)                                                                                                            |
| Tracker                  | Does `tracker.similar` find the older duplicate an issue was closed against?                   | `tracker_similar_bench.py`                                                                                                       |
| Latency and index health | Where does search time go; would a vector index return what brute force does?                  | `search_latency.py`, `vector_tenant_recall.py`, `ann_final_staleness.py`, `vector_index_vs_exact.py`, `scripts/ops/benchmark.py` |
| Write load               | What latency and how many ClickHouse parts does a sustained write rate cost?                   | `write_rate.py`                                                                                                                  |
| Consumer acceptance      | Does one kind of deployment get precise answers at an acceptable cost?                         | `acceptance/{code_agent,memory_app,plugin,harness}.py`                                                                           |

[`bench/memoryarena/`](../../bench/memoryarena/README.md) (an agent gym that scores later
sessions of dependent tasks) and [`bench/memorydata/`](../../bench/memorydata/README.md)
(a testbed that runs Loom beside other methods) are external harnesses with Loom plugged
in. Their READMEs are their run guides.

If you are evaluating Loom rather than changing its ranking code, start with
[Evaluate Loom](../getting-started/evaluate.md). It turns the harness inventory into a
short decision path for product wiring, your own memory workload, code intelligence and
published-result reproduction.

## Entry points

| Entry point                  | What it runs                                                               | Needs                                                             |
| ---------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `make bench-quality-gate`    | `quality.py` on the committed synthetic set; exit 1 below recall 0.80      | a server with a real embedder and extraction LLM                  |
| `make bench-algorithm-gate`  | `extraction_eval.py` and `graph_eval.py` on the regression fixtures        | a server with a real extraction LLM                               |
| `make bench-quick`           | `longmemeval.py` on 30 shuffled items with the pinned reader and judge     | the downloaded LongMemEval file, `LOOM_API_KEY`, `OPENAI_API_KEY` |
| `make bench-matrix`          | `matrix.py`: search mode × rerank × pool width on LongMemEval              | as `bench-quick`                                                  |
| `make bench-code-intel-dry`  | `code_intel_qa.py --dry`: the scorer on a perfect synthetic retrieval      | nothing (offline)                                                 |
| `make bench-code-intel`      | `code_intel_qa.py` with the rollout floors                                 | a server with an indexed ClickHouse repository, real embedder     |
| `make bench-code-legs`       | `code_intel_qa.py --compare-extra` for three leg ablations, each paired    | as `bench-code-intel`                                             |
| `make bench-clickgap-replay` | `clickgap_code_replay.py`: a PR-review bot's call sequence replayed        | a replay JSONL of that consumer's PRs                             |
| `make bench-clickgap-golden` | `clickgap_golden_replay.py` on that consumer's golden fixtures             | `GOLDEN_DIR`, private fixtures not in this repository             |
| `make bench`                 | `scripts/ops/benchmark.py`: concurrent load on one op, p50/p95/p99 and RPS | a server                                                          |
| `make test-unit`             | also runs `pytest bench -q`: the scoring cores and dataset readers         | nothing (offline)                                                 |

Every other harness runs as `python bench/<name>.py` ([running](./running.md)).

## How a bench differs from a production caller

A bench calls the same operations over HTTP, with three differences:

1. **Evaluation reads.** A crediting read reinforces the rows the next pass ranks
   against, so a bench would move its own results. Harnesses send `auto_touch: false` on
   `memory.search` and `reinforce: false` on `memory.ask` unless the run passes
   `--credit`.
2. **Throwaway bench namespaces.** Each namespace is registered with `traffic_class` and
   `kind` `bench`: the bench rate bucket (half the base cap by default), bench ClickHouse
   priority, a separate cost-ledger column, and erasure when the run ends
   ([bench namespaces](./methodology.md#bench-namespaces)).
3. **Pinned instrument.** The reader and judge models are fixed per harness and do not
   follow `LOOM_LLM_MODEL`. The server's extraction model, embedder and settings are the
   variable. `--answerer loom-ask` (LongMemEval) or `--reader ask` (LoCoMo) grades
   `memory.ask` itself.

The server's defaults are the ones these benches chose. Changing the `LOOM_LLM_MODEL` or
`LOOM_EMBEDDING_MODEL` default starts with `make bench-quick`, and a new embedder needs a
[re-baseline](./running.md#re-baselining-after-an-embedder-change).

## Coverage and what it leaves out

- **No quality signal in CI.** CI runs fake providers, so it tests scorers, not retrieval
  ([why](./methodology.md#fake-providers-make-scores-meaningless)). A ranking regression
  can merge unseen unless someone runs the gates on a real deployment.
- **Few measurements are committed.** [Results](./results.md) lists each committed
  measurement with its artifact under `bench/results/`, the instrument it ran under and a
  reproduction command. A number for your data and providers has to be produced on your
  own deployment.
- **Most harnesses need data the repository does not hold.** Third-party corpora are
  downloads ([datasets](./running.md#datasets)); replays, relied-on sets, tracker gold and
  latency-from-trace read one deployment's private data.
- **The code fixtures are repository-specific.** The ClickHouse lanes need an index of
  ClickHouse/ClickHouse, the Langfuse lane an index of that repository. Their numbers say
  little about another codebase.
- **No named-system scoreboard.** Harnesses on public datasets report Loom's numbers and
  the columns needed to place them beside published tables. The repository compares Loom
  with its own builds and arms only.
- **No hardware record or capacity model.** No harness records the host.
  `scripts/ops/benchmark.py` measures one op at one concurrency; `search_latency.py`
  breaks a `memory.search` into stages by namespace size. Neither models a real agent mix
  or multi-tenant contention.
- **The skill eval grades shapes, not recall** ([skill eval](./skill-eval.md#what-this-does-not-measure)).

### What a pass does not prove

- **A gate pass is a floor.** The build clears a recorded threshold on a small set under
  one embedder. That does not mean recall is good on your data.
- **Recall is not answer quality.** A memory can rank first and still be misread or
  stale. Check the judged axis and the failure stage.
- **Answer accuracy is not agent success.** Most harnesses score one question at a time.
  Only `code_plane_ablation.py` and the agent gyms measure what an agent did with the
  memory.
- **Safety benches measure named attacks.** `memory_poison.py` and
  `memory_extraction_redteam.py` report on their own attack shapes. `erasure_compliance.py`
  scans every String-shaped column of every `loom.*` table for its canary; external
  backends are only as complete as their own deletion.

## Related

- [Retrieval and ranking](../concepts/retrieval-and-ranking.md): what the retrieval
  benches exercise.
- [Code index](../features/code-index/overview.md): what the code benches exercise.
