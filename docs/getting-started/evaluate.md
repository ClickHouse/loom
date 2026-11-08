# Evaluate Loom

Evaluate the decision you need to make, not every harness in the repository. This guide
starts with a no-cost product smoke test, then moves to a workload-specific quality test.
The public benchmark suite is the final step when you need to audit or reproduce a
published claim.

## Choose an evaluation

| Decision                                        | Data                                                           | Providers                                              | Typical output                                                                        |
| ----------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| Does Loom install and connect correctly?        | Four test facts and one document                               | Stock fake providers are enough                        | A fact survives a new session; source, history and audit rows appear in the dashboard |
| Is memory useful for our agent?                 | 25–50 real questions with expected memory keys                 | Real embedder; real LLM for extraction or `memory.ask` | recall@k, rank@1, abstention, answer accuracy, latency and cost                       |
| Is code intelligence useful for our repository? | Representative symbols, caller sets and issue-shaped questions | Real embedder; indexed repository                      | exact definitions, caller F1, file recall, abstention, tokens and latency             |
| Can we reproduce a published result?            | The named public dataset and committed result artifact         | The pinned models or documented substitutes            | A comparable run with the same slice, reader, judge and search mode                   |

Do not use the fake embedder (a checkout's `.env`) or the fake LLM to judge quality: they
exist so the stack can boot without credentials and integration tests can be
deterministic. The released stack's local embedder retrieves for real, so a first look at
search is meaningful on it; judge answers and extraction with a real LLM.

## Before you run

Write down these five things first:

1. **Workload.** The users, repositories and question types the decision covers.
2. **Baseline.** What the same user or agent does without Loom: full context, grep, BM25,
   another memory system, or no retrieval.
3. **Precision gates.** The minimum recall, answer accuracy or task success you will
   accept. Quality comes before cost.
4. **Economy gates.** Maximum p95 latency, tokens, model cost, index time and storage.
5. **Instrument.** Loom commit, ClickHouse deployment, provider models, model parameters,
   hardware, dataset version and random seed.

Pre-registering the gates prevents a convenient metric or slice from becoming the result
after the run. Keep failures and abstentions visible; report request errors separately
instead of silently counting or discarding them as retrieval misses.

## 1. Product smoke test

This takes about 15 minutes and proves wiring, persistence and visibility—not semantic
quality.

1. [Boot the local stack](quickstart.md#1-boot-the-stack) and confirm `/v1/health` reports
   `status: ok`.
2. [Write and search a fact](quickstart.md#4-first-memory-over-http).
3. [Connect a coding agent](connect-a-coding-agent.md), tell it a durable preference, open
   a fresh session and ask for that preference.
4. Correct the fact, then confirm the dashboard shows its source and version history.
5. Run `loom demo` with a document and confirm the returned chunks cite that document
   ([document smoke test](quickstart.md#optional-ask-a-question-about-a-document)).

Pass only when the new session recalls the fact, the correction replaces the current
answer, an unrelated question abstains, and the dashboard shows the writer and history.
The result says nothing about production recall while health reports `fake_embedding` or
`fake_llm`.

## 2. Evaluate your workload

Configure [real providers](../operations/deployment.md#providers), reset or re-embed data
if the vector width changed, and record the resolved models from `/v1/health`. Use an
isolated namespace and keep the same indexed data for every paired arm.

### Memory and RAG

Build a set of at least 25–50 questions from real tasks. Include:

- direct facts, paraphrases, multi-hop questions and time-sensitive updates;
- questions whose answer should have been superseded;
- plausible but unanswerable questions, to measure false answers and abstention;
- near-duplicate distractors that differ in the value that matters.

For each answerable memory question, label the expected `memory_key` before running. For
document RAG, label the expected document and chunk, query `knowledge.search`, and score
those identifiers with the same retrieval metrics. Measure retrieval and synthesis
separately:

- **Retrieval:** recall@k, rank@1, MRR, false-positive rate and abstain precision/recall.
- **Answer:** correctness under one pinned reader and judge, plus citation correctness.
- **Economy:** p50/p95 latency, input/output tokens, model cost, construction cost and
  stored rows.

For an existing memory corpus, save a JSON dataset with this shape (the supplied harness
targets `memory.search`; use an equivalent scorer for `knowledge.search`):

```json
{
  "org": "acme",
  "namespace": "support-eval",
  "questions": [{ "query": "When can we deploy?", "gold_keys": ["deploy_window"] }]
}
```

Then run retrieval without changing the corpus:

```bash
export PYTHONPATH=$PWD LOOM_API_KEY=<token>
.venv/bin/python bench/retrieval_recall.py \
  --dataset /path/to/retrieval_qa.json --base-url http://localhost:7777 \
  --top-k 10 --out /tmp/loom-retrieval.csv
```

Set `--min-recall` and `--min-mrr` only from your pre-registered gates. Use
`memory.ask` or your production reader on the same questions for answer accuracy; recall
and answer accuracy are different measurements.

### Code intelligence

Index a repository that resembles your production repositories. Do not start with only
the repository Loom was tuned on. Label:

- definitions with the expected file and qualified symbol;
- callers with the complete expected caller set;
- conceptual and issue-shaped questions with acceptable files in the top 5 and top 10;
- out-of-repository questions that should abstain;
- the tests or dependants expected from an impact query.

Run the code-agent acceptance report against an indexed repository:

```bash
export PYTHONPATH=$PWD
.venv/bin/python bench/acceptance/code_agent.py \
  --base-url http://localhost:7777 --token-file /path/to/token \
  --namespace <code-namespace>
```

The built-in fixture targets ClickHouse and is useful for checking a deployment. For a
product decision, replace or extend it with your labelled questions and compare the same
agent tasks with ordinary repository tools only. Report task success first, then turns,
tokens, cost and wall time. Tool-schema overhead depends on the client, so record whether
it defers MCP tool definitions.

### Keep the comparison fair

- Use the same model, prompt, data snapshot and question order in paired arms.
- Keep extraction/index construction cost separate from per-question cost, then state the
  number of questions needed to amortize construction.
- Disable learning from evaluation reads (`auto_touch: false`, `reinforce: false`) so an
  early query cannot improve a later one.
- Report sample size, per-category results and uncertainty. Do not promote a small smoke
  slice to a general claim.
- Save per-question outputs, including errors and misses—not only aggregate scores.

## 3. Audit or reproduce published results

The README is a scorecard, not the evidence ledger. For every published number:

1. Open the matching section in [Benchmark results](../benchmarks/results.md).
2. Inspect its committed artifact under [`bench/results/`](../../bench/results/).
3. Confirm the dataset slice, Loom commit, models, reader, judge, search mode and seed.
4. Run the exact reproduction command from the result section.
5. Interpret the delta using the [methodology](../benchmarks/methodology.md), including
   judge calibration, bootstrap intervals and known bias.

Third-party datasets are not all redistributed by this repository. Their source and
license are listed under [datasets](../benchmarks/running.md#datasets). Full public runs
can take hours and incur provider charges; start with the documented smoke slice, then run
the complete protocol before publishing a comparison.

## What to publish

Publish a result in the README only when it is representative, understandable in one row,
and backed by a committed artifact and reproduction command. Put the full category table,
models, prompts, uncertainty, costs, failure analysis and limitations in the
[results ledger](../benchmarks/results.md). Put stable evaluation rules in
[methodology](../benchmarks/methodology.md), and commands and dataset acquisition in
[running](../benchmarks/running.md).

Every public claim should state:

- the task and sample size;
- the metric and whether it is retrieval, answer quality or end-to-end success;
- the baseline and paired delta, when one exists;
- models and judge, or that no LLM judge was used;
- uncertainty or the granularity of a one-question change;
- construction and query cost separately;
- the committed artifact and exact reproduction command;
- material negative findings and where the result does not generalize.

Do not publish every available number in the README. A long scoreboard hides the product
decision, encourages invalid cross-benchmark comparisons and makes stale claims hard to
spot. The README should lead readers to evidence; the docs should carry the evidence.

## Next

- [Benchmark coverage](../benchmarks/coverage.md): what has a public measurement, what
  has only a harness, and what has no public evidence yet.
- [Benchmark overview](../benchmarks/overview.md): choose a harness by the signal it measures.
- [Running benchmarks](../benchmarks/running.md): providers, tokens, commands and datasets.
- [Benchmark methodology](../benchmarks/methodology.md): comparison rules and limitations.
- [Benchmark results](../benchmarks/results.md): committed measurements and artifacts.
