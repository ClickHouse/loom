---
name: loom-change
description: What else a Loom change must touch — generated files, snapshots, erasure classification, docs, design notes — per change kind (wire op, table or column, migration, setting, worker, provider, code-index language, CLI, plugin hook, dashboard, docs). Use when adding, renaming or removing an op, field, table, column, setting, flag, worker or CLI command, or when asked "what else does this need". NOT for running the gates (loom-pre-push) or a fix inside one function.
---

# Making a Loom change

Find the row for the change, read its guide before the first edit, and finish its "also" column in the same pull request. The rules under `.claude/rules/` add the area-specific traps.

| Change | Guide | Also in the same PR |
| --- | --- | --- |
| Wire op (new, renamed, removed field) | [adding-an-operation](../../../docs/development/adding-an-operation.md) | `python -m scripts.codegen.sdk_py_gen`, `.sdk_py_client_gen`, `.sdk_ts_gen`, `.docs_reference`, `.op_reach_census`; unit test + integration test for a read or write path; MCP listing budget if it is listed by default |
| Table or column | [adding-a-table](../../../docs/development/adding-a-table.md) | migration + `make schema-dump`; RMT row builders; `loom.memory_versions` + its view for a `loom.memory` column; erasure step + census row, or a `NO_MEMORY_CONTENT` / `STEP_ONLY_TABLES` entry with its reason |
| Migration | [migrations](../../../docs/development/migrations.md) | next free number on current `origin/main`; additive only; `make schema-dump` |
| Setting | [extending-loom](../../../docs/development/extending-loom.md#add-a-setting) | field in `server/settings/_<area>.py` (models in `_models.py`) with a comment on what it controls and its values; `python -m scripts.codegen.settings_reference`; read as `settings.<field>` |
| Retire a flag | — | delete the setting, its branch and any request field carrying it; the changelog entry names it |
| Worker | [adding-a-worker](../../../docs/development/adding-a-worker.md) | registry row; `set_query_tag`; `LOOM_UPDATE_WORKER_SNAPSHOT=1` for an intended snapshot change |
| Provider | [extending-loom](../../../docs/development/extending-loom.md#add-a-provider) | `make bench-quick` evidence before changing a default (ask first — see `loom-bench`) |
| Code-index language | [adding-language-support](../../../docs/development/adding-language-support.md) | stay outside the extractor stamp; backfill plan in the PR |
| CLI command or flag | [code-generation](../../../docs/development/code-generation.md) | `LOOM_UPDATE_CLI_SNAPSHOT=1` snapshot; `python -m scripts.codegen.docs_reference` |
| Plugin hook or skill | [integrations](../../../integrations/claude-code-plugin/README.md) | edit `integrations/claude-code-plugin/`, then `python -m scripts.codegen.agent_plugin_sync`; bump both plugin manifests to a version no other open PR uses |
| Dashboard | [local-development](../../../docs/development/local-development.md#the-dashboard) | `npm --prefix frontend run test`, `npm --prefix frontend run build`, bundle budget |
| Docs | [documentation-style](../../../docs/development/documentation-style.md) | regenerate generated pages; never hand-edit them |

## Design before code

- A new table, op group, auth-model change, external dependency or identity-tuple change is a material change: the PR body carries the design (what, why, alternatives and their cost, migration story), and the matching `docs/architecture/` page is updated to describe the system as it now is.
- Map memory-plane behaviour onto the five belief-revision operations; something that maps to none is suspect.
- Prefer the existing seam (`server/core/ch.py`, the row builders, `server.core.paths`, the clock seam in tests) over a new one. Search for a helper before writing one.
- Loom deletes nothing its writer did not ask to delete: no TTL or retention sweep on tables that hold memory content.
- Security paths fail closed: an unloaded revocation list, a missing grant or an unverifiable token refuses.

## Code

- Short comments that say why; no history, dates, people or ticket numbers (`tests/unit/test_public_copy.py`).
- `except Exception as e:  # noqa: BLE001 — <what must not fail>`; anything narrower is better.
- No compatibility shims: a removed field is served and named in `ignored_args`; the changelog entry and the PR's compatibility section name the break.
- A function near the length baseline is split before it grows: `scripts/gates/function_length_gate.py` fails on one added line past its baseline.
- Moving a module breaks `Path(__file__).parents[N]`; resolve shipped files through `server.core.paths`.

## Tests

- Unit tests run in random order and without ClickHouse (`tests/unit/ch_fake.py`). Time goes through a seam (`tests/unit/clock_fake.py`); a fixture with a literal date and a wall-clock staleness check is a test that fails on a future day.
- A write path is proven on a real throwaway row: write it, read it back, diff every field. A probe that sends a missing key proves only the error path.
- Integration tests use `unique_org(...)` and `mint_test_token`, and skip without `LOOM_BASE_URL`.

Then run `loom-pre-push`.
