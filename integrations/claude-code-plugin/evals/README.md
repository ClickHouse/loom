# Plugin evals — the memory guidance, graded by what it writes

`claude plugin eval integrations/claude-code-plugin` runs the three cases
under `loom-memory/`. Each case's `prompt.md` body is the task brief under
`bench/skill_eval/tasks/` — the same text the bench runner hands to
`claude -p`; `tests/unit/test_skill_eval.py` keeps the two identical.

## Manifest

| case                      | skill         | expected behaviour                                                                      | graders (`graders/*.md`)                                                                                                                             |
| ------------------------- | ------------- | --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pr_review_batch`         | `loom-memory` | one row per PR, review bodies kept as documents, the reviewer's claim in working memory | `one-row-per-pr` (≥ 8 writes), `no-blob-values`, `no-claim-in-memory`, `no-double-encoding`, `no-hash-keys`, `skill-fired`                           |
| `research_note_revisions` | `loom-memory` | each revision rewrites the same readable key; nothing base64-encoded or double-encoded  | `no-key-per-revision`, `no-base64`, `no-blob-values`, `no-double-encoding`, `no-hash-keys`, `skill-fired`                                            |
| `sre_alert_investigation` | `loom-memory` | the five answers as separate claims, the lease in working memory                        | `claims-written` (≥ 5), `lease-in-working-memory` (≥ 1), `no-lease-in-memory`, `no-blob-values`, `no-double-encoding`, `no-hash-keys`, `skill-fired` |

- **Rubric.** Every scored grader is a `tool_used` count over the trace: a
  `min` says the right write happened, a `max: 0` says a bad shape never
  reached the write tool (a hash key, a double-encoded value, a value over
  ~2 KB, a lease or claim key). These are the shapes `namespace.audit`
  grades critical or warning. `skill-fired` (`arm: with-only`) is the
  plugin-fired indicator and is not scored under `--ablation with-without`.
  No `llm` graders: a judge reading the prose is not a gate.
- **Graders are pure.** Each is a regex over the tool input; none reads
  prose or depends on wording.
- **Fixtures.** `mocks/loom/<tool>.md` are fixed replies for the loom MCP
  server, so no server is touched and nothing is written. Each reply is a
  valid instance of that op's output model (`server/<domain>/api/models_*.py`,
  named by the op in `server/operations/registry.py`; for
  `loom_search` and `loom_activate`, the synthetic tool's structured
  content in `server/transport/`). A tool with no mock is denied.
- **Determinism.** Mocks and graders are fixed; the model is not, so a run
  takes `--runs 3 --threshold 1.0`.
- **Owner.** The plugin's maintainers; a change to a skill's write guidance
  runs this suite.

## Running

```
claude plugin eval integrations/claude-code-plugin --trust-plugin --no-publish \
  --case 'loom-memory/*' --runs 3 --threshold 1.0 --json evals-result.json
```

`--case` filters by name glob (`--case sre_alert_investigation`);
`--ablation with-without` (the default when the plugin resolves) reports how
often the same prompt without the plugin trips the same graders.

The second layer grades the namespace itself (`bench/skill_eval.py`). With
real servers (`--mocks off --allow-real-servers --allow-tools
"mcp__plugin_loom_loom__*"`, a writer token in `LOOM_API_KEY`) the agent
writes to the case's namespace; then
`python bench/skill_eval.py --task <case> --runner manual --audit-only`
runs `namespace.audit` over it and applies the pass rule (0 criticals, ≤ 1
warning) plus the shape counters. The eval framework has no script grader,
so this step is a second command, not a grader file. The offline replay of
both layers is `tests/unit/test_skill_eval.py`.
