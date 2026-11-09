# Server layout

`server/` is organised by domain. Each product plane owns one package holding its wire
declarations, handlers, pure logic, ClickHouse access and background loops, and a test
fixes which way imports may point. This page is the map for placing a new module and for
reading an import error from the contract test.

## Packages

| Package                       | Owns                                                                                                                                                                                                        | May import                                                                                                  |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/main.py`              | App wiring: the lifespan, routers, workers, transports. With `server/boot.py`, `server/health.py`, `server/wiring.py`, `server/worker_main.py` and `server/__init__.py`, the only root modules              | anything                                                                                                    |
| `server/boot.py`              | The lifespan's boot and shutdown steps: the serialised boot migration, the leader's tracker namespace rows, the revocation snapshot and keyring, the supervisor lease loop, the write drain                 | anything                                                                                                    |
| `server/health.py`            | `GET /v1/health`: the multi-tier health answer (ClickHouse, providers, the boot snapshot, calibration, the rate limiter, on request the workers and observability)                                          | anything                                                                                                    |
| `server/wiring.py`            | `wire()`: hands each platform seam its domain piece (the auth gate's namespace directory, the state gauges' facts); run by `main.py`, the CLI and the test suites                                           | anything                                                                                                    |
| `server/worker_main.py`       | The workers child's composition root (`python -m server.worker_main`): the supervisor and what its workers use, without the serving lifespan                                                                | anything                                                                                                    |
| `server/core/`                | Cross-cutting infrastructure: the ClickHouse client (`ch`), `errors`, budgets, datetimes, hashing, naming, cursors, idempotency, rate limits, vector storage policy, `schema/` (migrations and DDL tooling) | `core`, `settings`                                                                                          |
| `server/settings/`            | Every setting declared in `catalog.py`, one section per area, composed into `Settings`                                                                                                                      | `core`, `settings`                                                                                          |
| `server/operations/`          | The registry core: `Operation` and `Role` (`base.py`), the flattened registry (`registry.py`: `ALL_OPERATIONS`, `OPERATIONS_BY_NAME`), `toolsets.py`, read projection, the reach census                     | `core`, platform packages, every domain's `api`                                                             |
| `server/transport/`           | HTTP and MCP: admission, request binding, listings, the primer                                                                                                                                              | `core`, platform packages; a domain's `api`, `app` and `domain` (never its `store` or `workers`); not `cli` |
| `server/cli/`                 | The `loom` command, an outer adapter                                                                                                                                                                        | anything                                                                                                    |
| `server/workers/`             | The worker registry (`registry.py`), the supervisor, pass context, status and its store, first-pass scheduling, cost events. Worker loops live in their domain                                              | `core`, platform packages; a domain's `workers` (the registry's module strings)                             |
| `server/observability/`       | The ledger, events, audit rows, metrics, tracing export                                                                                                                                                     | `core`, platform packages                                                                                   |
| `server/auth/`                | Token mint and verify, the role and namespace gate, the per-request identity                                                                                                                                | `core`, platform packages                                                                                   |
| `server/embedding/`, `llm/`   | Pluggable embedder and LLM providers                                                                                                                                                                        | `core`, platform packages                                                                                   |
| `server/clients/`, `storage/` | Outbound HTTP (the shared pool, GitHub); retention clauses                                                                                                                                                  | `core`, platform packages                                                                                   |
| `server/memory/`              | The memory plane: `memory.*`, search, ask, extraction, facts, procedures, profile, working memory, context packs, conflicts, blacklist, `loom_search`                                                       | see [layers](#layers)                                                                                       |
| `server/code/`                | The code plane: `code.*`, the index (`app/index/`, `domain/index/`, `store/index/`), its parsers (`domain/extract/`), the ingest and compiler-tier workers                                                  | see [layers](#layers)                                                                                       |
| `server/tracker/`             | Tracker namespaces: `tracker.*`, duplicate checks, the live source readers and their client, loom's own tracker tables                                                                                      | see [layers](#layers)                                                                                       |
| `server/knowledge/`           | Knowledge documents and chunks, conversations, repository documents                                                                                                                                         | see [layers](#layers)                                                                                       |
| `server/erasure/`             | The erasure saga, its census, the federated backends, the recovery worker                                                                                                                                   | see [layers](#layers)                                                                                       |
| `server/tasks/`               | The task inbox, the run ledger, their wake-up and webhook loops                                                                                                                                             | see [layers](#layers)                                                                                       |
| `server/identity/`            | Namespaces, grants, history policy, read scope, restricted access, the dashboard page and the caller API (`/v1/me/*`)                                                                                       | see [layers](#layers)                                                                                       |
| `server/insights/`            | Operator surfaces: `ops.*`, `cost.*`, `agents.*`, `audit.list`, `ui.vitals`, `mentor.brief`, the `/v1/admin/*` routes                                                                                       | see [layers](#layers)                                                                                       |
| `server/assistant/`           | The assistant: its LibreChat service user and agent definition, set up at boot                                                                                                                              | see [layers](#layers)                                                                                       |

The platform packages are `core` and the other non-domain packages above: `settings`,
`operations`, `transport`, `cli`, `workers`, `observability`, `auth`, `embedding`, `llm`,
`clients` and `storage`. Besides `core` and `settings`, each may import any other platform
package, and none imports a domain package except as the table says.

## Layers

A domain package has up to five sub-packages. A module goes where its primary
responsibility is; a small domain has fewer.

| Layer      | Holds                                                                                                                            | May import                                                                                                                                                              |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api/`     | The wire: Pydantic input and output models (`models*.py`) and the `Operation` declarations of the domain's op groups (`ops*.py`) | `core`, `operations`, `settings`; any domain's `domain` and `api`; its own `app` for the handler an `Operation` binds                                                   |
| `app/`     | Handlers and orchestration: what an op or a worker pass does, in order, with I/O                                                 | its own `api`, `app`, `domain`, `store`; other domains' `app`, `domain` and wire models (`api/models*.py`); `core` and every platform package but `transport` and `cli` |
| `domain/`  | Pure logic, typed values and policies: no ClickHouse, no network                                                                 | `core` (not its I/O modules: `ch`, `schema`, `idempotency`, `ratelimit_shared`, `namespace_write_marks`), `settings`, any domain's `domain`                             |
| `store/`   | ClickHouse repositories and row builders: every statement the domain runs against its tables                                     | `core`, `settings`, `observability`, `storage`, its own `store`, any domain's `domain`                                                                                  |
| `workers/` | Background loops, one module per registry row                                                                                    | as `app`, with `workers` in place of `api`; other domains' `app`, `domain` and wire models                                                                              |

One `domain/` module starts a process: `server/code/domain/extract/bounded_parse.py`, which
every tree-sitter parser in `domain/` calls, parses each source first in a local child
process under resource limits.

Two edges are deliberately absent. `store/` never calls `app/`: a repository takes values
and returns rows, so an app can be tested against a fake store. And no domain reads
another domain's `store/`: it calls that domain's `app/` or reads its `domain/` types, so
a table's owner is the only writer of its statements. To call another
domain's handler, `app/` and `workers/` may import that domain's wire models
(`api/models*.py`), never its `api/ops*.py`. `app/` imports the wire
models, never the `ops` modules, so the `Operation` declarations can bind handlers without
an import cycle.

## The import contract

`tests/unit/test_package_contracts.py` parses every module under `server/` with `ast`
and classifies each import — top-level, nested under `if`/`try`, inside a function, or
under `TYPE_CHECKING` — by the package and layer of both ends. An import the rules above
do not allow fails the test with the importer and the imported module.

The imports that break the contract today are listed in
`tests/unit/package_contract_allowlist.txt`, keyed `importer -> imported` by module path.
The list only shrinks: an allowed pair that no longer occurs also fails the test, so
fixing an import means deleting its line.

A second check runs beside it: only `server/transport/`, `server/cli/` and
`server/main.py` import `server.transport` (`tests/unit/test_transport_layering.py`).
No domain module imports `server.cli`.

## Adding a module

1. Pick the domain whose table, op group or plane the module serves. A module that
   serves every plane (a ClickHouse helper, a hashing rule) goes in `core/`; one that
   serves two domains goes in the one whose rows it reads, and the other calls it.
2. Pick the layer by what the module does, not by who calls it: I/O or an `async def`
   that awaits something is `app/` or `store/`; a function of its arguments is `domain/`.
3. Name it for what it holds inside its package, without repeating the path:
   `server/erasure/app/saga.py`, not `server/erasure/app/erasure_saga.py`.
4. A package's `__init__.py` holds a one-line docstring saying what the package owns. It
   re-exports names only when it is the package's public surface
   (`server/memory/store/`, `server/code/store/index/`).
5. A background loop is a module in `<domain>/workers/` plus one row in
   `server/workers/registry.py` naming its full module path.
6. A mock or monkeypatch target, a registry row and a coverage floor name modules by
   path; write them with the module's real path, never through a re-export.
7. Find files the server ships (migrations, `schema.sql`, the frontend build) through
   `server.core.paths`, never by counting parents of `__file__`
   (`tests/unit/test_repo_paths.py`).

## Extractor modules

The files listed in `extractor_stamp.STAGE_MODULES` are the code index's version stamp.
The stamp hashes each file's name and code tokens, with an import counted as the names
it binds rather than the package they come from. A changed stamp re-derives the index of
every code namespace. They live in `server/code/domain/extract/` with `extractor_stamp.py`,
which reads them from its own directory, and keep their file names. The language lane
modules live beside them. Moving one between packages leaves the stamp where it is;
renaming one, reformatting it (`ruff format` excludes them) or changing its code moves it.

## Related

- [System overview](system-overview.md) — the control plane's components and request flow.
- [Code index architecture](code-index-architecture.md) — the extractor stamp and re-indexing.
- [Request path](system-overview.md#request-path) — the path a request takes through the transport, the registry and a handler.
