# Code generation

Several files in the repository are derived from the code: the SDKs' operation tables
and wire types, the reference pages, the Codex plugin's copies, the op reach manifest,
the schema dump. Each is committed, so a reader sees it without running anything, and
the generators marked below have a `--check` mode that exits 1 when the committed file
differs from what it would write. `make lint` runs each of those checks, and CI runs them
in the `lint` and `sdk-typescript` jobs and the unit suite, so a stale file fails with the
command to run named in the failure.

**Why commit generated files.** A generated file that is only built on demand is
invisible in review: a change to an operation's input model would change the SDK types
without a diff anyone reads. Committing them puts the effect of a change next to the
change, and the `--check` gate stops the two drifting apart. The cost is a regeneration
step in the change that moves the source.

## The generators

All live in [`scripts/codegen/`](../../scripts/codegen/) and share
`scripts/codegen/common.py` (the repository root, the facts an SDK table carries per op,
ruff formatting, an atomic write, and the write-or-check protocol).

| Script                      | Writes                                                                                                                                                                                | From                                                                                                                                                                    | `--check` in `make lint`   |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `sdk_py_gen.py`             | `sdk/python/loom/ops.py`: `OpName`, the `OPS` table (idempotency, role, maturity, latency budget) and one `TypedDict` per input and output model                                      | `server.operations.registry.ALL_OPERATIONS`                                                                                                                             | yes                        |
| `sdk_py_client_gen.py`      | `sdk/python/loom/client.py`: `OpsClient`, one method per op (`agent.client.tracker.source_add`), typed with the `ops.py` names                                                        | `server.operations.registry.ALL_OPERATIONS`                                                                                                                             | yes                        |
| `sdk_py_admin_gen.py`       | `sdk/python/loom/admin_routes.py`: `AdminRoutes`, one method per `/v1/admin/*` route (`agent.deployment.routes.settings_set`)                                                         | the `/v1/admin/*` routers: each route's method, path, gate and docstring                                                                                                | yes                        |
| `sdk_ts_gen.py`             | `sdk/typescript/src/generated/ops.ts`: one interface per model, the `OPS` table, the `OpMap` type                                                                                     | `server.operations.registry.ALL_OPERATIONS` (the models' JSON Schemas)                                                                                                  | yes                        |
| `agent_plugin_sync.py`      | the Codex plugin's copies of the hook scripts (`hooks/*.mjs`), the shared library (`lib/*.mjs`), the skills and the listing icon (`assets/`), byte-identical with the executable bit  | `integrations/claude-code-plugin/`                                                                                                                                      | yes                        |
| `settings_reference.py`     | the generated [configuration reference](../reference/configuration.md)                                                                                                                | `server/settings/catalog.py`: each field's env var, type, default, and the comment above it                                                                             | yes                        |
| `settings_schema.py`        | `schemas/loom-settings.schema.json`, the JSON Schema of the YAML settings file                                                                                                        | the settings fields' types, bounds, defaults and comments, secrets left out                                                                                             | yes                        |
| `settings_example.py`       | `loom.example.yaml`, the settings-file template: every setting but the secrets, commented out at its default under its description, by area                                           | the settings catalog's fields, defaults, comments and live marks                                                                                                        | yes                        |
| `docs_reference.py`         | the generated regions of `docs/reference/`: `operations`, `workers`, `cli`, `errors`, `schema`, `admin_routes` (the [operator-route table](../reference/http-api.md#operator-routes)) | the registry, `server/workers/registry.py` (`_WORKERS`), `tests/unit/snapshots/cli_tree.json`, `server/core/errors.py`, `schemas/schema.sql`, the `/v1/admin/*` routers | yes                        |
| `op_reach_census.py`        | `server/operations/reach_manifest.py` (`--write-manifest`); a census table otherwise                                                                                                  | the registry and the MCP toolsets                                                                                                                                       | yes                        |
| `wire_changelog.py`         | nothing; prints what changed for clients between two revisions                                                                                                                        | the capability catalogue built at each revision                                                                                                                         | no                         |
| `gen_py_library_methods.py` | `server/code/domain/extract/py_library_methods.py`                                                                                                                                    | the running interpreter's standard library                                                                                                                              | no (interpreter-dependent) |
| `push_langfuse_prompts.py`  | `server/observability/langfuse_prompts.lock.json` (`--write`)                                                                                                                         | the prompt constants in the server; pushes them to Langfuse                                                                                                             | no                         |

The configuration reference is rendered from comments, so the comment above a setting is
its documentation: edit the comment, not the page. The `docs/reference/` pages are
hand-written prose around `<!-- BEGIN GENERATED: <name> -->` … `<!-- END GENERATED: <name> -->`
regions; only the text between a pair of markers is rewritten, so a page can explain the
table it carries.

### sdk_py_gen and sdk_ts_gen

The Python SDK stays dict-in, dict-out; the generated file adds the names, so an editor
completes `agent.call("memory.search", …)` and a type checker sees the field names without
a hand-kept model layer. The dicts are `total=False` with `Required[...]` on the fields the
server requires. The TypeScript generator turns each model's JSON Schema into interfaces
(enums and consts to literal unions, `anyOf` to unions, shared `$defs` hoisted once), so
`client.call("memory.search", input)` is typed end to end. Run them after any change to an
operation's registration or its models:

```bash
python scripts/codegen/sdk_py_gen.py
python scripts/codegen/sdk_py_client_gen.py
python scripts/codegen/sdk_ts_gen.py
```

Both SDKs expose every op as a method at the same place: the group is the op name's first
segment, the method the rest with `.` joined, snake_case in Python
(`agent.client.tracker.source_add`) and camelCase in TypeScript (`loom.tracker.sourceAdd`).
`tests/unit/test_sdk_parity.py` fails when the two op tables, or the two method names of
any op, differ.

### agent_plugin_sync

The two coding-agent plugins differ only in their manifests and hook wiring
(`hooks/hooks.json`). The Codex plugin carries copies rather than `../` references so each
plugin is a self-contained directory a marketplace can check out on its own. After adding a
hook script or a skill to the Claude Code plugin, run the sync and commit the copies; wire
a new hook into the Codex `hooks.json` by hand if Codex should run it.

### op_reach_census

Every operation declares a maturity and must have a reach path other than a raw HTTP
`POST`, or be declared `experimental`. The reach kinds (`server/operations/reach.py`,
`REACH_KINDS`) are the MCP listings (`mcp_core`, `mcp_atomic`, `mcp_all`, derived live
from `server/operations/toolsets.py`), an SDK method (`sdk_method`), a shipped example or
operator script (`sdk_example`), a plugin hook or integration (`plugin_hook`) and a
dashboard call site (`dashboard`). The last four come from scanning the tree, which a
deployed server does not carry, so they are committed as `reach_manifest.STATIC_REACH`.
`--check` enforces the rule, that every op without a dashboard call site is in
`server/operations/dashboard.py` (with the reason, or pending), and that the
committed manifest matches the tree; `--write-manifest` refreshes it. `--call-counts --days 30` adds each op's call
count from `loom.agent_events` (the only mode that connects to ClickHouse), and
`--owners` names the commit that introduced each experimental op's declaration.

### Wire changelog

```bash
python scripts/codegen/wire_changelog.py <rev_a> <rev_b>           # markdown
python scripts/codegen/wire_changelog.py --since-hours 24 [rev_b]   # rev_a = rev_b's ancestor N hours back
python scripts/codegen/wire_changelog.py <rev_a> <rev_b> --json
python scripts/codegen/wire_changelog.py --snapshot <rev> > snap.json
python scripts/codegen/wire_changelog.py --diff a.json b.json
```

It materialises each revision with `git archive`, imports its registry in a child
interpreter, and prints the operations and the request and response fields added or
removed, plus role, budget and MCP-tier changes. Its output goes into the release notes
([release process](./release-process.md#release-notes)); the "what changed for clients"
section is generated, not written.

### gen_py_library_methods

The set of standard-library method names the Python extractor uses is frozen in the tree
so an interpreter upgrade cannot move call edges. Regenerating it changes an extractor
module, which moves the code index's version stamp and re-chunks every indexed namespace;
review the diff, and regenerate only when you mean to.

### push_langfuse_prompts

The server links an LLM generation to a Langfuse prompt version by looking up the
prompt's content hash in the committed lockfile, with no runtime call. Without `--write`
the script plans against the lockfile offline; `--write` pushes changed prompts (needs
`LOOM_LANGFUSE_URL` and `LOOM_LANGFUSE_ADMIN_TOKEN`) and rewrites the lockfile. A unit
test fails while the lockfile misses a registered prompt.

## The schema dump

`schemas/schema.sql` is generated by `scripts/ops/dump_schema.py` from a ClickHouse with
every migration applied:

```bash
make schema-dump    # python -m scripts.ops.dump_schema
make schema-check   # --check: exit 1 with the head of the diff; writes nothing
```

It is covered in [migrations](./migrations.md#schema-dump-and-check), since it follows
every migration change and must be taken against the ClickHouse version CI pins.

## Snapshots that are tests, not generators

The CLI surface (`tests/unit/snapshots/cli_tree.json`, `cli_help.json`) and the worker
registry (`tests/unit/snapshots/worker_registry.json`) are pinned by unit tests, and are
regenerated by an environment variable on the test run
([testing](./testing.md#ci-gates)). The CLI reference renders from `cli_tree.json`, so
after regenerating the CLI snapshot, run `docs_reference.py` too.

## Before you push

```bash
make lint            # every --check above, plus ruff, the gates and Biome's dashboard accessibility rules
```

A failure names the script to run; run it, review the diff, and commit the regenerated
file in the same change as its source.

## Related

- [Adding an operation](adding-an-operation.md): the op, the handler, the SDK
  tables
- [Documentation style](./documentation-style.md#generated-pages): which pages are
  generated
- [Testing](./testing.md)
