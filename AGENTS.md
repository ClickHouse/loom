# Loom — instructions for coding agents

Loom is a memory, knowledge, and observability data plane for AI agents, built on ClickHouse. This is the one instruction file for every coding agent: Claude Code reads it through `CLAUDE.md`; Codex and Cursor read it directly.

## Layout

- `server/` — FastAPI control plane. One package per plane (`memory/`, `code/`, `tracker/`, `knowledge/`, `erasure/`, `tasks/`, `identity/`, `insights/`), each split into `api/` (wire models, `Operation` declarations), `app/` (handlers), `domain/` (pure logic, no I/O), `store/` (every ClickHouse statement and row builder) and `workers/`; `tests/unit/test_package_contracts.py` enforces the import direction. `server/operations/registry.py` collects every op; `server/transport/` serves them over HTTP (`POST /v1/{op}`) and MCP; `server/core/ch.py` is the one ClickHouse client.
- `sdk/python/`, `sdk/typescript/`, `frontend/` (the dashboard, served at `/`), `extractor/` (the document extractor service, run apart from the server), `schemas/migrations/` (numbered SQL; `schemas/schema.sql` is generated), `integrations/` (the Claude Code and Codex plugins), `bench/`, `dev/`, `deploy/` (ClickHouse config, the proxy, the Helm chart), `docs/`.
- The full map: [repository guide](docs/development/repository-guide.md), `docs/architecture/server-layout.md`.

## Rules by area

Each `.claude/rules/<area>.md` holds the traps for the files its `paths:` frontmatter names. Claude Code loads a rule when it reads or edits a matching file. Other agents: before editing a file, read the rule whose paths match it (`awk '/^paths:/,/^---$/ {print FILENAME ": " $0}' .claude/rules/*.md` lists them all).

| Rule | Covers |
| --- | --- |
| `clickhouse-sql` | statements, the client, parameters, `FINAL`, mutations |
| `migrations` | numbering, additive changes, `schema.sql`, new tables |
| `memory-plane` | memory, knowledge, erasure and audit write paths |
| `wire-ops` | ops, transports, auth, SDKs |
| `settings` | settings fields and their reference page |
| `workers` | background loops |
| `code-index` | the extractor stamp and mirrors |
| `providers` | LLM and embedder providers |
| `plugins` | the coding-agent plugins and their hooks |
| `frontend` | the dashboard |
| `helm` | the Kubernetes chart |
| `tests` | unit and integration tests |
| `docs` | documentation pages |
| `bench` | benchmark harnesses and results |

## Invariants

These hold in every area; a change to one needs a design rationale in the PR body and in its `docs/architecture/` page.

- **Identity tuple on every row:** `(org, namespace, agent, user_id, session_id)`, the last two defaulting to `'-'`. The server sets `actor` from the token (`server.memory.app.provenance.writer_actor`) and `source_class` from the op and its `source_trace`, where a request may only declare a downgrade to `derived`, `external` or `imported` (`server.memory.domain.provenance.source_class_for`); a new write path sets both.
- **Tenant isolation.** A read scopes by `(org, namespace)` from the token. An op addressed by org-wide id (`memory.get_by_id`, `update_by_id`, `redact`) checks the caller's role on the namespace that holds the row (`authz.row_reach` through `id_namespace.namespace_for_id`, or `authz.row_in_scope` after the fetch) and answers `found=False`, never a 403 that confirms the id exists (`tests/integration/test_cross_principal_leakage.py`).
- **Belief revision** is the design lens for memory: Expand (`memory.set`), Revise (`memory.update_by_id`), Contract (`memory.delete`), Reinforce (`memory.pin`), Refuse (`blacklist.add`). Behaviour that maps to none of them is suspect.
- **Builds N-1 and N serve side by side** on N's schema during a rolling upgrade, and nothing replica-wide lives outside the deployment's stores: ClickHouse, under the Postgres memory authority Postgres for live memory, and the document bucket's bytes ([rolling upgrades](docs/architecture/rolling-upgrades.md), [storage authority](docs/architecture/storage-authority.md)).
- **Loom deletes only what its writer asked to delete**, and every table that can hold memory content is covered by erasure (`memory-plane` rule).
- **Security paths fail closed**: an unloaded revocation list, a missing grant or an unverifiable token refuses.
- **No compatibility code**: a replaced internal path is deleted in the same change, with no shim, fallback, rollout mode for data that does not exist yet, or doc describing the old way. What a running build N-1 needs follows the rolling-upgrade rules, and a removed wire field is served and named in `ignored_args`; the PR body names the break.
- A broad except says why: `except Exception as e:  # noqa: BLE001 — <what must not fail>`. A loop outside the transports, the worker supervisor and the CLI calls `set_query_tag(...)`.

## Commands

```bash
make server              # :7777, from source
make migrate             # idempotent; needs the ClickHouse in .env or LOOM_CH_*
make format lint typecheck test-unit    # what CI runs first (CI: make format-check)
make test                # integration suite; needs LOOM_BASE_URL
npm --prefix frontend run build
loom mint-token --subject <name> --org <org> --namespaces <ns> --roles writer
```

## How work lands

Each line is a rule; the skill named after it is the procedure (`.claude/skills/`, which Codex and Cursor also read through `.agents/skills/`).

- **Diagnose before you edit**: the failing cases from evidence, the pattern they share, the change and the result it predicts. Running confirms a diagnosis; it is never how you find one (`loom-debug`).
- **Decide within the goal.** Make the design call a senior Loom engineer would and report it. Ask first only for what is public or hard to undo: opening or merging a PR, filing or closing an issue, posting a comment, deleting data, deploying, spending benchmark money.
- **A guard that stops you is a decision**: a denied command, a refusing hook or a disabled push URL is reported, never routed around by another spelling, URL or tool.
- **Look before you build**: search for the existing helper, setting or feature, and for an open PR doing the same work (`gh pr list --search <area>`).
- **One task, one worktree from `origin/main`, its own ClickHouse.** The branch ref `main` and the git stash are shared by every worktree, so a linked worktree never checks out `main` and nobody uses `git stash` (a WIP commit instead). Tests and migrations run against a ClickHouse you started, never one you found listening (`loom-dev-setup`).
- **Shared machine**: stop every server, container and background run you started before you report, and remove only what you created, by exact name, never by a prefix filter.
- **Every gate the diff touches passes before the push**, fixed at the source, never by loosening the gate (`loom-pre-push`; what else a change must touch: `loom-change`).
- **A pull request** is one concern with a title that says what is now true, a public body from the template, signed-off commits, a fresh review at every push and a squash merge on green (`loom-ship-pr`, `loom-review`, `loom-address-review`). Issues: `loom-file-issue`.
- **Benchmarks run only on request**: they spend provider money and hours (`loom-bench`).
- **Committed and posted text** is plain technical English: the fact first, short comments that say why, no history, dates, people, hosts, internal namespaces or other products' names (`loom-writing`; `tests/unit/test_public_copy.py`).

`tests/unit/test_agent_instructions.py` checks these files: frontmatter, budgets, trigger cases, and that every path, make target and module they name exists (links: `tests/unit/test_doc_links.py`). Renaming any of those: the test names each file to update.
