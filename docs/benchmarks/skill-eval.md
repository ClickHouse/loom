# Skill eval

The skill eval grades the `loom-memory` skill by the namespace an agent leaves behind. The
skill says what goes where: a claim is one plain-text `memory.set` under a readable key, a
document goes to knowledge with a short pointer row, a table of things with identity is
records, a lease is working memory, and a revision rewrites the same key. The eval runs a
task, grades the namespace with `namespace.audit`, and treats any critical finding as a
failure.

## The pieces

| Piece             | Where                                                | What                                                                                                                                                                                                                                                                                                                                    |
| ----------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Task briefs       | `bench/skill_eval/tasks/*.md`                        | Three briefs that tempt the anti-patterns: an alert investigation with a long log, snippets, a table, a lease and a large model to keep (`sre_alert_investigation`); a batch of PRs with structured fields (`pr_review_batch`); a note revised twice (`research_note_revisions`). Each names its namespace and says it will be audited. |
| Runner            | `bench/skill_eval.py`                                | Runs the task (`--runner claude-code`, `codex`, `manual`, `replay`), then `namespace.audit` (one-hour window, 5 samples), then the shape counters; writes the result JSON.                                                                                                                                                              |
| Recorded agents   | `bench/skill_eval/replays/{bad,good}_agent.jsonl`    | `{op, body}` streams. The bad agent double-encodes values, uses hash keys and polls a lease stored as a memory; the good one follows the [investigation agent memory](../guides/investigation-agent-memory.md) guide.                                                                                                                   |
| Plugin eval suite | `integrations/claude-code-plugin/evals/loom-memory/` | The same briefs as `claude plugin eval` cases, graded from the tool trace against a mocked Loom server (`evals/mocks/loom/`).                                                                                                                                                                                                           |
| Unit test         | `tests/unit/test_skill_eval.py`                      | Runs both replays through the runner against a fake Loom whose audit uses the handler's own classifiers: bad fails with the expected codes, good passes. Also pins the task list and the eval prompts' bodies.                                                                                                                          |

Each brief's front matter carries `task`, `namespace`, `agent` and `traps` (the
anti-patterns it tempts). The runner substitutes `$ORG`, `$NAMESPACE` and `$AGENT` in a
replay stream so one recording runs in any org.

## The score

`pass` means the audit returned **0 critical** findings and **at most 1 warning**
(`MAX_WARNINGS_TO_PASS`). The report lists each finding with its count, share of the rows
checked, bytes and the writer-side remedy, then shape counters read from the rows
themselves, so the grade is not the audit alone:

| Counter                  | Meaning                                                                                                     |
| ------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `rows`                   | Live memory rows                                                                                            |
| `readable_key_share`     | Keys whose last segment is not a long hex run and with no sha-length segment (the audit's `hash_keys` rule) |
| `plain_text_value_share` | Values that are a string and not serialized JSON                                                            |
| `documents`              | Knowledge documents (`knowledge.list`)                                                                      |
| `records`                | Typed records (`memory.record.query` per registered schema)                                                 |
| `working_memory_blocks`  | Working-memory blocks (`working_memory.render`)                                                             |

The shape reads run **after** the audit, so they never count as the agent's reads. A
counter the token cannot read is listed under `shape.skipped`, not reported as zero.

## Run it

### Replay (no model is called)

Any Loom works; a local stack is enough.

```bash
export LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY=<writer token> LOOM_ORG=<org>
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/bad_agent.jsonl            # exits 1: FAIL
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/good_agent.jsonl --baseline   # exits 0: PASS
```

Use a fresh namespace per run (`--namespace skill-eval-sre-$(date +%s)`). The audit window
is one hour, so a reused namespace carries the previous run's reads into the grade.

Offline, the same check runs as a unit test with no server:

```bash
.venv/bin/python -m pytest tests/unit/test_skill_eval.py -q
```

### A live agent with the skill loaded

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code --baseline
```

This costs model tokens. It runs, from the repository root:

```bash
claude -p --plugin-dir integrations/claude-code-plugin \
  --mcp-config <generated file: loom at $LOOM_BASE_URL/mcp?toolset=all, Bearer $LOOM_API_KEY> \
  --strict-mcp-config --allowedTools "mcp__loom__*" "Skill" "Read" \
  --permission-mode bypassPermissions --output-format json --max-turns 60 \
  "<the task body>"
```

- `--plugin-dir` loads the skills from the checkout, so the run measures the skill text in
  the working tree.
- `toolset=all` lists `memory_schema.register` and `memory.record.write`, which the
  default `core` listing leaves out. `knowledge.upsert` is not listed even under `all`
  (neither the core nor the atomic roster lists it); the agent calls it through `loom_op`.
- The generated MCP config holds the token; it is created `0600` and deleted after the
  run.
- `--max-turns` (default 60) caps the run.

`--runner codex` runs `codex exec` with the same server wiring; the Codex plugin, when
installed, supplies the skills.

### Any other agent

`--runner manual` prints the brief. Run any agent you like against the namespace it
names, then grade what it left:

```bash
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual --audit-only
```

The same `--audit-only` grades a namespace a `claude plugin eval` run with real servers
wrote.

### Compare with a baseline

Edit the skill, run again and compare:

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code \
  --compare bench/out/skill_eval_sre_alert_investigation_baseline.json
```

The delta table has one row per finding code (severity and count, before and after) and
one per shape counter. A live agent is not deterministic, so compare a skill edit over
several runs rather than one.

### Output

Every run writes `bench/out/skill_eval_<task>_<runner>_<ts>.json` (`--out-dir` moves it).
`--baseline` also writes `bench/out/skill_eval_<task>_baseline.json`, the reference
`--compare` reads. `bench/out/` is gitignored.

## The plugin eval suite

```bash
claude plugin eval integrations/claude-code-plugin --trust-plugin --no-publish \
  --case 'loom-memory/*' --runs 3 --threshold 1.0 --json evals-result.json
```

This grades each run from the tool trace, offline: the Loom MCP server is a mock with
fixed replies (`evals/mocks/loom/`), so nothing is written. Graders use only documented
grader types:

- `tool_used` with `max: 0` on memory writes whose input has a hash key, a
  JSON-string-of-JSON value, a value over about 2 KB, a lease or lock key, or a versioned
  revision key: the shapes `namespace.audit` grades critical or warning;
- `tool_used: Skill` matching `loom-memory`, the plugin-fired indicator;
- positive minimums for working memory and per-item claims.

There are no `llm` graders: a rubric read of the prose is not a gate. Cases filter by
name glob (`--case sre_alert_investigation`). `--ablation with-without`, the default when
the plugin resolves, reports how often the same prompt without the plugin trips the same
graders.

The framework has no script grader, so the namespace-level grade is a second command.
With `--mocks off --allow-real-servers --allow-tools "mcp__plugin_loom_loom__*"` and a
writer token in `LOOM_API_KEY`, the agent writes to the case's namespace, and
`bench/skill_eval.py --task <case> --runner manual --audit-only` grades it.

## Reading a failure

Each finding's remedy is the writer-side fix. The `loom-optimize` skill maps the same
codes to reworking rows that already exist ([optimize a namespace](../guides/optimize-namespace.md)).

| Finding                                    | The rule that did not land                                    |
| ------------------------------------------ | ------------------------------------------------------------- |
| `double_encoded_values`                    | Send the value once; never `json.dumps` a string twice.       |
| `hash_keys`                                | Keys are readable `<scope>/<kind>/<id>`, not hashes.          |
| `coordination_as_memory`                   | Leases and locks belong in working memory.                    |
| `exact_key_store`                          | Keep the pointer in working memory instead of polling by key. |
| `oversized_values`, `json_prefix_excerpts` | A document goes to knowledge, with a short pointer row.       |

A run that passes the audit with `documents: 0` on the SRE task kept the large model
where search cannot reach it. Read the shape line, not only the verdict.

## Adding a task

A new brief needs front matter, a `skill-eval-…` namespace named in its body, and the
closing sentence that the namespace will be audited; a matching plugin eval case with a
byte-identical body; and an update to the task list `tests/unit/test_skill_eval.py` pins.
See [adding a case](./adding-a-case.md#skill-eval-tasks).

## What this does not measure

Recall quality. Whether the claims an agent wrote answer next week's question is a recall
benchmark's job ([overview](./overview.md)); this grades the shapes that make recall
impossible.

## Related

- [Investigation agent memory](../guides/investigation-agent-memory.md): the recipe the
  good replay follows.
- [Optimize a namespace](../guides/optimize-namespace.md): reworking rows the audit flags.
- [Claude Code plugin](../features/integrations/claude-code.md) and
  [Codex](../features/integrations/codex.md).
- [`integrations/claude-code-plugin/evals/README.md`](../../integrations/claude-code-plugin/evals/README.md).
