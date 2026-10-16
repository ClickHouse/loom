# Loom — instructions for coding agents

Loom is a memory, knowledge, and observability data plane for AI agents, built on ClickHouse. This is the one instruction file for every coding agent: Claude Code reads it through `CLAUDE.md`, Codex and Cursor read it directly. Per-area detail lives in `.claude/rules/<area>.md`, each naming the paths it governs: read the rule for the files you touch.

## Repo orientation

- `server/` — FastAPI control plane. One package per product plane (`memory/`, `code/`, `tracker/`, `knowledge/`, `erasure/`, `tasks/`, `identity/`, `insights/`), each with `api/` (wire models, `Operation` declarations), `app/` (handlers), `domain/` (pure logic, no I/O), `store/` (every ClickHouse statement and row builder for its tables) and `workers/`. `tests/unit/test_package_contracts.py` enforces which layer may import which.
- `server/operations/registry.py` flattens every domain's ops; `server/transport/` serves them over HTTP (`POST /v1/{op}`) and MCP from that registry; `server/core/ch.py` is the one ClickHouse client; `server/settings/` one mixin per area.
- `sdk/python/`, `sdk/typescript/` (generated op tables), `frontend/` (the `/me` dashboard), `schemas/migrations/` (numbered SQL; `schemas/schema.sql` is generated), `integrations/` (the Claude Code and Codex plugins), `bench/`, `dev/`, `docs/`.
- The full map: [repository guide](docs/development/repository-guide.md) and `docs/architecture/server-layout.md`.

## Conventions that aren't obvious

- **Wire op registration** lives in a domain's `server/<domain>/api/`: Pydantic wire models in `models_<group>.py` (they import no handler), the `Operation` declarations and the module-level `OPERATIONS` list in `ops_<group>.py`, which `server/operations/registry.py` imports. Adding an op: define `Input`/`Output` models, write the handler in `server/<domain>/app/` (it imports the models at module top), declare the `Operation`, append to the list. HTTP + MCP transports iterate the registry.
- **Ops addressed by an org-wide id** (`memory.get_by_id`, `update_by_id`, `redact`: `(org, memory_id)` only) get no namespace from the transport, so the handler calls `authz.row_in_scope(org, row.namespace, role)` after the fetch and answers `found=False` on refusal — never a 403 that confirms the id exists. `tests/integration/test_cross_principal_leakage.py` probes every read op as another tenant with canary nonces and is the CI gate for this.
- **Identity tuple on every row:** `(org, namespace, agent, user_id, session_id)` — first three required, rest default to `'-'`. Two more columns are stamped by the server, never read from the body: `actor` (`human:<subject>` / `process:<subject>`, kind from the token — `server.memory.app.provenance.writer_actor`) and `source_class` (`asserted | extracted | observed | derived | imported | external` — `server.memory.domain.provenance.source_class_for`; a request may only declare a downgrade to `derived`, `external` or `imported`). New write paths must set both.
- **Belief-revision is the design lens** (not a code enum). Five operations cover the memory lifecycle: Expand (`memory.set`), Revise (`memory.update_by_id`), Contract (`memory.delete`), Reinforce (`memory.pin`), Refuse (`blacklist.add`). Anything in the memory plane that doesn't map to one of these is suspect.
- **ReplacingMergeTree rows carry every column.** A new version row of `loom.memory` / `loom.shared` goes through `build_memory_version_row` / `build_embedding_row` (`server/memory/store/rows.py`); omitting a column zeros it for every reader under `FINAL`. A new `loom.memory` column also goes into `loom.memory_versions` and its view in the same migration.
- **Erasure covers every table that can hold memory content**: a saga step and a residue-census row in `server/erasure/store/census.py` (or a classified exemption with its reason); every write that creates or changes memory leaves a hash-only audit row. Loom deletes nothing its writer did not ask to delete: no TTL or retention sweep on memory content.
- **Settings live in `server/settings/`**, one mixin per area (`_memory_search.py`, `_memory_ask.py`, `_code.py`, `_tracker.py`, …) composed into one `Settings`; env names are `LOOM_<FIELD>` as always. A new field goes in its area's file with a comment saying what it controls and what its values mean. The exception is a setting that names a model: every one lives in `_models.py`, one per job, so the models a deployment runs are changed in one place. Read a field as `settings.<field>` — `getattr(settings, "<field>", default)` fails `test_settings_attribute_access.py`, because the copied default drifts and hides the reader from anyone deleting the field.
- **Workers are rows**, not code: `server/workers/registry.py::_WORKERS` holds one `_Worker(name, module, enabled, interval, …)` per background loop, imported lazily when enabled; `tests/unit/test_worker_registry_snapshot.py` pins the enabled set per settings profile.
- **Retiring an experiment flag** deletes the setting, its branch and any per-request field that carried it, in one change; the release notes name it.
- **Extractor modules' code is the code index's version stamp**: a code change to a file in `extractor_stamp.EXTRACTOR_MODULES` (or a grammar in `STAMPED_GRAMMARS`) re-runs its stage (`STAGES`: chunk, symbol, edge, guard) for the language families it touches in every indexed namespace (hours of ingest). Lane parsers outside the stamp (TypeScript, Rust, Go, Java, …) don't move it; re-ingest their files with `loom code backfill --paths-file`. The stamp hashes code tokens only (`extractor_stamp.logic_tokens`), so comments and docstrings can be edited freely. They are excluded from formatting, which could rewrite string quotes; change their code only when the extraction itself changes.
- **A broad except says why**: `except Exception as e:  # noqa: BLE001 — <what must not fail>`; BLE001 is enforced.
- **Every ClickHouse query is attributed**: the transports, the worker supervisor and the CLI dispatcher (`cli.<cmd>`) set the `log_comment` tag; a query issued outside them logs `untagged:<module>.<function>` — a new background loop should `set_query_tag(...)` for itself.
- **Foundational shapes are load-bearing.** Engine choice (ClickHouse), identity tuple, per-row content-hash audit (`server/observability/audit.py` — per-row hashing, NOT a chain), the five belief-revision operations. Changes here need a written design rationale in the PR and in the matching page under `docs/architecture/`, not just code.
- **Providers.** Dev defaults are fake providers (no network, no key). Changing a default model or embedder needs `make bench-quick` evidence first (`loom-bench`); switching the embedder changes the vector space and needs a re-embed (`.claude/rules/providers.md`).

## Common tasks

- Server — `make server` (`:7777`); migrations — `make migrate`; unit tests — `make test-unit`; integration — `make test` (needs `LOOM_BASE_URL`)
- `make format` (CI: `make format-check`), `make lint`, `make typecheck`, `make test-unit` — CI also runs branch-coverage floors and the integration suite (`loom-pre-push` says when)
- Frontend — `npm --prefix frontend run build`
- Mint a JWT — `loom mint-token --subject <name> --org <org> --namespaces <ns> --roles writer`

## What Loom is not

- Not file-format aware in-server — `knowledge.add` takes `content: str`; PDF/Markdown/S3 ingestion is the operator CLI's job (`dev/loom_ingest.py`).
- Not a GDPR-compliance guarantee — the erasure cascade and per-row audit hashing are engineering primitives (`.claude/rules/memory-plane.md`).

## Key reading order for a new contributor

1. README — product surface + onboarding.
2. `docs/architecture/system-overview.md` and `docs/architecture/clickhouse.md` — control-plane shape, ClickHouse layout, the design and its constraints.
3. `server/operations/registry.py` — the wire-op registry; `docs/architecture/server-layout.md` — where every package and layer lives.
4. `server/main.py` lifespan — how workers and transports are wired.
5. The domain package for whatever you're touching: its `api/` models and ops, then the handler in `app/`.

## How work lands

These hold for every change, by a person or an agent. The skills under `.claude/skills/` (Codex reads the same files through `.agents/skills/`) carry the step-by-step versions.

- **One task, one worktree, one branch from `origin/main`.** `git worktree add -b <branch> ../loom-<branch> origin/main`. Never check out, pull or reset `main` inside a linked worktree: the branch ref is shared with the primary checkout, so a deployment running from it sees changes it never pulled. Never `git stash`: the stash stack is shared by every worktree of the clone; set work aside with a WIP commit.
- **Your own ClickHouse.** Integration tests and migrations run against a ClickHouse you started for this worktree and confirmed empty, never one you found listening on a port. Tests that need a server read `LOOM_CH_HOST` / `LOOM_BASE_URL` explicitly and skip without them.
- **Run code from the worktree.** `python -m scripts.<pkg>.<module>`, not `python scripts/<pkg>/<module>.py`: a script run by path imports `server` from whatever checkout the virtualenv installed in editable mode. `python -c 'import server; print(server.__file__)'` says which.
- **Every gate before the push**: `make format`, `make lint`, `make typecheck`, `make test-unit`; the integration suite when server behaviour changed. A failing gate is fixed at the source, never by loosening it.
- **Pull requests**: the title says what is now true; the body fills `.github/PULL_REQUEST_TEMPLATE.md` and is public text, so it carries no host names, internal namespaces, people, wall-clock times or production counts. One concern per pull request, grouped by concern rather than split per file. Commits are signed off (`git commit -s`).
- **Review starts at the push**, in parallel with CI, not after it. CI runs on pull requests into `main` and pushes to it; "no checks" means a stacked base or a merge conflict.
- **Merge** with `gh pr merge --squash`, only when every required check is green. After another pull request merges, a pull request that adds files the whole-tree gates scan (public copy, doc links, function length) re-runs CI on the new `main` before it merges.
- **Benchmarks cost money and time**: never start a benchmark run nobody asked for. A result counts only when the treatment demonstrably ran and the paired delta clears the noise band ([methodology](docs/benchmarks/methodology.md#the-verdict)).
- **No compatibility shims.** A renamed or removed op, field or flag is changed in one step; a client that still sends a removed field is served with it named in `ignored_args`, and the changelog entry and the PR's compatibility section name the break.
- **Code reads like expert code**: short comments that say why, no history ("previously", "new in"), no dates, people or ticket numbers.

## Skills, rules and agents

| Path | What | Loaded |
| --- | --- | --- |
| `.claude/skills/loom-dev-setup/` | worktree, virtualenv, a private ClickHouse, a server from source | on request |
| `.claude/skills/loom-change/` | the recipe and gates for each kind of change (op, table, migration, setting, worker, plugin hook, frontend, docs) | when changing code |
| `.claude/skills/loom-pre-push/` | the gates to run for the paths a diff touches | before a push |
| `.claude/skills/loom-ship-pr/` | pull request body, push, review, CI watch, squash merge | when opening or landing a PR |
| `.claude/skills/loom-review/` | Loom's review gates: the invariants a diff can break | reviewing a PR or diff |
| `.claude/skills/loom-investigate-ci/` | a red CI job to its cause and fix | a failing check |
| `.claude/skills/loom-bench/` | when and how to run a benchmark and report it | on request |
| `.claude/skills/loom-deploy/` | blue/green deploy, rollback and checks with `redeploy.sh` | operators, on request only |
| `.claude/rules/*.md` | per-area rules, each with the `paths:` it governs | Claude Code loads one when a matching file is read or edited; other agents read the one for the files they touch |
| `.claude/agents/loom-reviewer.md` | a read-only reviewer that runs `loom-review` | after a push |

`tests/unit/test_agent_instructions.py` checks their frontmatter, links and path globs.
