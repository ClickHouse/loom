# Adding a benchmark case, dataset or suite

The committed sets are small ([set sizes](./methodology.md#set-sizes)), so a new case is
the cheapest way to improve the suite. Read the file's header or `_comment` first: most
state how their gold was built and what a new row must satisfy.

## Add a case to an existing set

### Memory QA (`bench/datasets/synthetic_memory_qa.json`)

Append a question to a conversation, or a conversation:

```json
{
  "conversation_id": "06-billing",
  "conversation": [{ "role": "user", "content": "Our invoices go out on the 3rd of each month." }],
  "questions": [{ "question": "When do invoices go out?", "gold_keys": ["invoice", "3rd"] }]
}
```

A hit is any returned `memory_key` or excerpt containing a gold string (case-folded,
underscores as spaces). Pick gold words a correct memory must contain and a wrong one is
unlikely to. Keep it synthetic: no third-party text, no real person.

**It moves the baseline.** The 0.80 gate threshold was recorded on this file. Run the gate
on a real deployment before and after, and update `MIN_RECALL` in the `Makefile` if the
value changes. Tests: `tests/unit/test_bench_quality_gate.py`, `bench/test_quality.py`.

### Extraction and graph regressions

- `bench/fixtures/extraction_regressions.json` rows are `{id, messages, expected}`:
  phrases that must appear in some memory's key or value.
- `graph_regressions.json` rows are
  `{id, messages, expected_triples: [{subject, relation, object}], as_of}`.
- A `_note` field names the failure class; the harness ignores it.

`tests/unit/test_algorithm_gate_assets.py` checks every row has messages and a non-empty
expectation. Run `make bench-algorithm-gate` against a real extraction LLM to see the case
pass.

### Code-plane QA (`bench/fixtures/code_intel_qa*.jsonl`)

One JSON object per line; `//` lines are comments:

```json
{"id": "co037", "type": "conceptual", "query": "where are part merges scheduled", "gold": {"paths": ["src/Storages/MergeTree/MergeTreeBackgroundExecutor"]}}
{"id": "sd011", "type": "symbol_def", "query": "IColumn::insert", "gold": {"qualified_name": "DB::IColumn::insert", "path_prefix": "src/Columns/"}}
{"id": "ab011", "type": "abstain_probe", "query": "kubernetes ingress annotation for sticky sessions", "gold": {}}
```

Gold per type is in the `bench/code_intel_qa.py` docstring (`callers` takes
`expected_callers`, `hub` takes `path_prefix` and `symbols`). Paths are prefixes without
the extension. Optional `lane`, `facets` and `namespace` route and slice the row.

- **Verify the gold against a live index** before it gates: run once without floors, read
  the mismatches, fix the gold.
- **An abstain probe must be about code that is not in the repository.**
- **Keep the main file's type set.** `bench/test_code_intel_qa.py`
  (`test_fixture_loads_and_validates`) asserts `code_intel_qa.jsonl` has at least 30 rows
  and exactly the types `symbol_def`, `callers`, `conceptual`, `abstain_probe`. Hub,
  history and test-set rows go in their own lane files.
- `make bench-code-intel-dry` must still pass: every lane scores its ceiling under a
  perfect synthetic retrieval.
- Mined lanes are regenerated, not hand-edited: `code_intel_mine_prs.py` and
  `code_intel_ka_slice.py` write the file and its header counts.

### Coding-agent lessons (`bench/fixtures/coding_agent_qa.json`)

Append to `lessons` (a memory row keyed by `memory_key`) and `questions` (`question`,
`question_type` from the taxonomy, `gold_keys`, optional `fix_reuse`). Every gold key must
name a real lesson key: `bench/test_coding_agent_qa.py`
(`test_load_corpus_rejects_dangling_gold_key`) fails otherwise. `--dry` must still pass.

### Op-planning tasks (`bench/fixtures/op_planning_tasks.jsonl`)

A task carries `id`, `class` (`instruction_type`, `structure`), `prompt`, a `gold` plan
(tool, required args, literal args) and store `assertions`. `tests/unit/test_op_planning_bench.py`
(`test_every_gold_tool_and_required_arg_exists_on_the_surface`) fails when a gold tool or
argument no longer exists on the MCP surface. A task whose op was retired moves to
`op_planning_tasks_retired.jsonl` rather than being deleted.

### Skill-eval tasks

A brief under `bench/skill_eval/tasks/` has front matter (`task`, `namespace`, `agent`,
`traps`), names a `skill-eval-…` namespace in its body, and ends by saying the namespace
will be audited. The same body is a `claude plugin eval` case at
`integrations/claude-code-plugin/evals/loom-memory/<task>/prompt.md`, byte-identical after
the front matter. `tests/unit/test_skill_eval.py` checks all three rules and pins the task
list, so a new task also updates that test. See [skill eval](./skill-eval.md).

## Wire in a new dataset

1. **Do not commit it.** Put it under `bench/datasets/` or take a path flag. If it is
   fetched, add a `--fetch` path or print the download command when the file is missing,
   as `longmemeval.py` does.
2. **Record provenance in the harness docstring:** source, file names, item count and the
   licence the dataset states. Add a row to [datasets](./running.md#datasets).
3. **Keep the dataset's own categories** as a column, and name the comparable subset if
   published tables use one.
4. **Public benchmark names are fine in `bench/`**, never as the reason for a choice in
   prose under `server/` (`tests/unit/test_public_copy.py` `BENCHMARK_NAMES`).
5. Mint throwaway namespaces under a prefix in `LOOM_NAMESPACE_BENCH_PREFIXES`
   (`server/settings/catalog.py`; new harnesses use `bench-`), so `loom namespaces-gc` and
   the namespace GC find what an aborted run leaves behind.

## Write a new harness

Every harness follows this shape:

| Rule                              | How                                                                                                                                                                                                                                                                             |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Standalone script                 | argparse, `--base-url`, `--token` (default `LOOM_API_KEY`), `--out`; runnable as `PYTHONPATH=$PWD python bench/<name>.py` and as a module                                                                                                                                       |
| Shared HTTP policy                | Post through `bench/_http.py` (`post_json` / `apost_json`): Bearer token, 429 waits, bounded 5xx retries, errors carrying the response body                                                                                                                                     |
| Docstring is the manual           | What it measures, what it does not, dataset, cost, and a run command                                                                                                                                                                                                            |
| Pure scoring core                 | Scoring functions above the live path, importable without a server, with a unit test (`bench/test_<name>.py` or `tests/unit/test_bench_<name>.py`)                                                                                                                              |
| A `--dry` or self-test mode       | Exercises the scorer on synthetic input with no server and no spend                                                                                                                                                                                                             |
| Evaluation reads                  | Merge `measurement_fields("memory.search")` / `("memory.ask")` from `bench/_gate.py` into bodies first; offer `--credit`                                                                                                                                                        |
| Throwaway, registered namespaces  | `register_bench_namespace` + `remember_bench_namespace` before the first write, the work inside `erase_on_exit`, and a `--keep-namespaces` flag (`bench/bench_namespaces.py`)                                                                                                   |
| Pinned scope                      | Send `namespace_scope: "this"` on reads                                                                                                                                                                                                                                         |
| Judged? Use the shared gate       | `add_gate_arguments(parser)` for `--repeats`, `--gate*`, `--credit`, `--judge-calibration`; `gate_from_args` for the verdict; pin reader and judge models and warn on overrides. `tests/unit/test_bench_gate_help_renders.py` shows why a bare `%` in help text breaks `--help` |
| Record the instrument             | Embedder, LLM, provider and `build_commit` from `/v1/health` beside the numbers (see `quality.py` `run_metadata`)                                                                                                                                                               |
| Report failures apart from misses | Count and print request errors; do not score them as misses                                                                                                                                                                                                                     |
| Paired comparisons                | Per-item deltas through `paired_bootstrap` with a cluster key; ship on the lower bound                                                                                                                                                                                          |

Then:

- add a bullet to the right section of [`bench/README.md`](../../bench/README.md) and, if
  it answers a common question, a row in its "When to run what" table;
- add a Makefile target only for a gate someone runs routinely, with its floors as
  overridable variables;
- run `make format` (ruff formats `bench/`), `pytest bench -q`, and the copy lint:

```bash
.venv/bin/python -m pytest tests/unit/test_public_copy.py tests/unit/test_bench_namespaces.py -q
```

The copy lint scans `bench/`: no product names, people, internal hosts or namespaces,
paper citations or decision-record references in code, comments or fixtures.

## Related

- [Methodology](./methodology.md): what the gate and the bootstrap compute.
- [Running](./running.md): commands and outputs.
- [Testing](../development/testing.md): the unit and integration suites.
