# Testing

Which suite a test belongs in, what each suite needs to run, how tests skip when their environment is missing, which gates CI enforces, and how to regenerate what they pin.

## The suites

| Suite                | Path                                                                 | Needs                                              | Run locally                                                    | In CI                                |
| -------------------- | -------------------------------------------------------------------- | -------------------------------------------------- | -------------------------------------------------------------- | ------------------------------------ |
| Unit                 | `tests/unit/`                                                        | nothing external; no ClickHouse                    | `make test-unit` (also runs `pytest bench -q`)                 | `unit`, random order, coverage ≥ 40% |
| Bench harnesses      | `bench/` (its `test_*` files)                                        | nothing external                                   | `pytest bench -q`                                              | `unit`                               |
| Integration          | `tests/integration/`                                                 | a migrated ClickHouse and a running server         | `make test-integration` with `LOOM_BASE_URL`                   | `integration`                        |
| Analyzer corpus      | `tests/analyzer/`                                                    | a migrated ClickHouse (no server)                  | `pytest tests/analyzer -q`                                     | `integration`                        |
| Code lanes corpus    | `tests/code_lanes/`, gated by `tests/unit/test_code_lanes_corpus.py` | nothing external                                   | `python -m tests.code_lanes.corpus`                            | `unit`                               |
| Dashboard            | `frontend/src/**/*.test.tsx`                                         | Node                                               | `npm --prefix frontend run test` (vitest, with axe)            | `frontend-tests`                     |
| Dashboard end to end | `frontend/e2e/*.spec.ts`                                             | Node, Chromium (`npx playwright install chromium`) | `npm --prefix frontend run e2e`                                | `frontend-tests`                     |
| Plugin hooks         | `integrations/*/tests/*.test.mjs`                                    | Node                                               | `node --test integrations/claude-code-plugin/tests/*.test.mjs` | `agent-plugins`                      |
| TypeScript SDK       | `sdk/typescript/`                                                    | Node                                               | `npm --prefix sdk/typescript run check`                        | `sdk-typescript`                     |
| Benchmarks and gates | `bench/*.py`, `make bench-*`                                         | a server with **real** providers                   | [running benchmarks](../benchmarks/running.md)                 | never (the CI embedder is fake)      |

**Why benchmarks stay out of CI.** CI runs with `LOOM_EMBEDDING_MODEL=fake-ngram-1024`
and `LOOM_LLM_PROVIDER=fake`, so a recall number there measures nothing about real
retrieval, and a green quality gate on fake embeddings is worse than no gate. Running
them in CI would need a paid provider secret in the repository. The gates
(`make bench-quick`, `bench-quality-gate`, `bench-code-intel`) are run by hand against a
deployment with real providers, before changing an extraction prompt, the default model
or the embedder.

## Unit tests

- **No ClickHouse.** A unit test stubs the client; anything that needs real SQL
  semantics belongs in the integration or analyzer suite.
- **Random order.** CI runs `pytest tests/unit -p randomly --randomly-seed=<seed>` and
  prints the seed first. A test must not depend on the one before it; reproduce a failure
  with the printed seed.
- **Async tests sleep with `await asyncio.sleep(...)`**, never `time.sleep`.
- **Wall-clock dates.** A fixture with a fixed date that a staleness check compares to
  `now()` passes until the date ages past the threshold. Pin the clock through the code's
  time seam instead of hard-coding a date the check reads against the wall clock.

## Integration tests

The integration suite runs against a live server, the way CI's `integration` job does:

```bash
bash dev/ci_start_clickhouse.sh                      # or the dev stack's ClickHouse
make migrate
LOOM_INSTANCE_LABEL=dev:$USER:tests LOOM_BACKGROUND_WORKERS=off make server &
LOOM_BASE_URL=http://localhost:7777 make test-integration
```

- `tests/conftest.py` loads the repository's `.env` into the test process (filling only
  missing variables), so the tests and the server pick the same providers: rows written
  under one embedder are invisible to reads under another.
- The session mints one wildcard-org writer token into `LOOM_API_KEY` unless you exported
  one, and the `mint_test_token` fixture mints a token for a test's own org. Both sign with
  the test process's `LOOM_JWT_SECRET`, which must match the server's.
- Run the server with `LOOM_BACKGROUND_WORKERS=off`: tests that exercise a worker call its
  `run_pass` in-process against their own namespace, and a live supervisor's workers have
  no namespace filter and would drain those tests' rows.
- `tests/integration/test_document_object_store_live.py` also needs an S3-compatible store
  in `LOOM_TEST_S3_ENDPOINT` (CI runs a local one, see `.github/workflows/ci.yml`) and
  skips without it.
- The publishing tests in `tests/integration/test_knowledge_ingest_live.py` run a worker
  pass that sends originals to a document extractor: they need one reachable through
  `LOOM_DOCUMENT_EXTRACTOR_URL` (or `LOOM_DOCUMENT_EXTRACTORS_JSON`) and
  `LOOM_DOCUMENT_EXTRACTOR_TOKEN`, and skip without one. CI runs the reference
  extractor (see `.github/workflows/ci.yml`; [running it](../../extractor/README.md)).
- CI sets `LOOM_LATENCY_BUDGET_MULTIPLIER=10`: shared runners are noisy enough that the
  default per-op budgets fire violations.
- Tests that depend on self-signup skip in CI, which has no `LOOM_SELF_SIGNUP=open`; prefer
  minted tokens.
- **Tiers.** `tests/integration/conftest.py` registers four markers a quick local pass can
  deselect (`-m "not slow and not destructive"`); CI runs the whole directory. `slow`
  spends wall clock on purpose (lease lapses, retry exhaustion), `destructive` stops
  merges, runs `OPTIMIZE` or deletes whole scopes, `provider` needs a real embedder or LLM
  and skips on the fakes, `recovery` drives a failure and the path back from it.
- **Failure diagnostics.** A failing test's report carries its last 25 Loom requests
  (method, URL, status, `X-Request-Id`, latency) with JWTs, bearer tokens and secrets
  redacted, so the matching server log line can be found by request id.
- **Cleanup.** `unique_org(label)` (`tests/integration/_resources.py`) mints
  `it-<label>-<8 hex>`. At session end every row of every minted org is deleted from each
  table with an `org` column and the residue is counted; residue fails the run.
  `LOOM_IT_KEEP_ROWS=1` keeps the rows for debugging.

## How tests skip

A test whose environment is missing skips with a reason; it never fails for a missing
variable. Use `os.environ.get(...) or pytest.skip(...)`, never `os.environ[...]`. The
shared fixtures follow the rule: `control_plane_url` skips without `LOOM_BASE_URL`, and
skips with the URL and the error when a set `LOOM_BASE_URL` does not answer 200 on
`/v1/health`.
`require_real_embedder` skips a search-quality test on a server running the fake embedder.
The analyzer suite (`migrated_ch`) skips, with the address and the fix in the reason, when
no ClickHouse answers at `LOOM_CH_*` or the target is behind on migrations, and fails
under `CI`, where the job starts one and runs migrations first.

## The analyzer statement corpus

Loom's ClickHouse statements are Python strings built by many builders under many flags.
Unit stubs accept any string, so an alias that shadows a filtered column, a CTE read
without its `WITH`, an outer alias used inside a subquery, or a parameter type the driver
cannot bind reaches a server as a leg that fails open: missing rows, not an error.
`tests/analyzer/` drives every builder across its axes, deduplicates the statements by
shape, and runs each through `EXPLAIN QUERY TREE` against the migrated, empty tables,
which rejects all of those in milliseconds. `shapes.txt` is the committed review surface:
a pull request that changes a statement's shape shows the new head in its diff.

```bash
pytest tests/analyzer -q                              # what CI runs
python -m tests.analyzer.corpus --write               # regenerate shapes.txt (review the diff)
python -m tests.analyzer.corpus --case erasure --errors
```

The [analyzer README](../../tests/analyzer/README.md) covers the case format, the
migrated-target requirement and importing production shapes captured with
`LOOM_CH_CAPTURE_STATEMENTS`.

## CI gates

Most gates are unit tests, so `make test-unit` runs them; the rest are `make lint` steps.
Each names what to run when it fails.

| Gate                            | Where                                                                                 | Fails when                                                                                                                                                                           |
| ------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Public copy                     | `tests/unit/test_public_copy.py`                                                      | the tree names an adjacent product, a person, an internal host or namespace, cites a paper or a design record; public pages name a competitor                                        |
| Doc links                       | `tests/unit/test_doc_links.py`                                                        | a relative link or `#anchor` in the root Markdown, `docs/` or the integration READMEs does not resolve                                                                               |
| Settings reference              | `tests/unit/test_settings_reference.py`, `settings_reference.py --check`              | the generated configuration reference differs from the settings sources, or a field has no row                                                                                       |
| Docs reference                  | `tests/unit/test_docs_reference.py`, `docs_reference.py --check` (`make lint`)        | a generated region under `docs/reference/` is stale, or an op or renderer has no row                                                                                                 |
| Stock env matches docs          | `tests/unit/test_stock_env_matches_docs.py`                                           | the quickstart's stated stock `.env` values drift from `.env.example`, or a documented compose command lacks `--env-file .env`                                                       |
| Function length                 | `scripts/gates/function_length_gate.py` (lint)                                        | a file gains an oversized function, or a listed function grows past its baseline                                                                                                     |
| mypy ratchet                    | `scripts/gates/mypy_gate.py` (`make typecheck`)                                       | any file has more strict-mypy errors than `scripts/gates/mypy_baseline.json`                                                                                                         |
| Secrets                         | `scripts/gates/check_no_secrets.py` (lint), `tests/unit/test_no_secrets_guard.py`     | a tracked file carries a credential-shaped value                                                                                                                                     |
| Dashboard accessibility         | `biome lint frontend/src` (`make lint`, CI job `format`)                              | a dashboard `.tsx` adds an accessibility violation                                                                                                                                   |
| Cross-principal leakage         | `tests/integration/test_cross_principal_leakage.py`                                   | any read op, driven as another tenant with canary nonces, returns a nonce or a 500; payloads are synthesised from each op's input model, so a new read op is probed the day it lands |
| Worker registry snapshot        | `tests/unit/test_worker_registry_snapshot.py`, `snapshots/worker_registry.json`       | the enabled worker set, order, cadence or deadline changes under the `defaults`, `all_on` or `production` profile                                                                    |
| Worker registry parity          | `tests/unit/test_worker_registry_parity.py`                                           | the workers `/v1/admin/workers.status` lists and the ones the supervisor runs differ                                                                                                 |
| Migrations additive             | `tests/unit/test_migrations_additive.py`                                              | a migration drops or renames a table or column, or exchanges tables outside a declared rebuild                                                                                       |
| Migration numbers               | `tests/unit/test_migration_numeric_order.py`                                          | two migration files share a number                                                                                                                                                   |
| Schema check                    | `schema-check` job: strict dry run, then `make schema-check`                          | the ledger holds a file the repository lacks, or `schemas/schema.sql` differs from a replay of every migration                                                                       |
| CLI surface snapshot            | `tests/unit/test_cli_surface_snapshot.py`, `snapshots/cli_tree.json`, `cli_help.json` | any `loom` parser, argument, default or `--help` text changes                                                                                                                        |
| Op reach census                 | `tests/unit/test_op_reach_census.py`, `op_reach_census.py --check`                    | an op has no reach path other than raw HTTP and is not experimental, or `reach_manifest.py` is stale                                                                                 |
| MCP listing budget              | `tests/unit/test_mcp_listing_budget.py`                                               | the default `tools/list` grows past its token ceiling or points a model at a tool it cannot call                                                                                     |
| Settings access                 | `tests/unit/test_settings_attribute_access.py`                                        | code reads a setting with `getattr(settings, "<field>", default)`                                                                                                                    |
| Generated SDK and plugin copies | `sdk_py_gen.py`, `sdk_ts_gen.py`, `agent_plugin_sync.py` `--check` (lint)             | a generated file differs from its source ([code generation](./code-generation.md))                                                                                                   |
| Formatting                      | `format` job, plus `ruff format --check` in `lint` (`make format-check` locally)      | ruff, Biome, Prettier, Taplo or shfmt would change a file                                                                                                                            |

To regenerate a snapshot for an intended change:

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
LOOM_UPDATE_WORKER_SNAPSHOT=1 python -m pytest tests/unit/test_worker_registry_snapshot.py
python scripts/gates/function_length_gate.py --update   # only ever lowers the baselines
python scripts/gates/mypy_gate.py --update
```

A ratchet baseline never rises: `--update` writes only lower counts, so debt cannot move
between files.

## The CI workflow

`.github/workflows/ci.yml` runs on every pull request into `main` and on pushes to
`main`, with Python 3.12 and Node 22. A newer push to the same ref cancels the running
workflow. Every action is pinned to a full commit SHA (its version in a comment), and the
workflow grants its jobs read-only repository access (`permissions: contents: read`).

| Job                  | Runs                                                                                                                                                                                                                                                                             |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lint`               | `ruff check`, `ruff format --check`, the `--check` of `sdk_py_gen.py`, `sdk_py_client_gen.py`, `agent_plugin_sync.py`, `settings_reference.py`, `docs_reference.py` and `op_reach_census.py`, `mypy --strict` on the SDK, the mypy ratchet, the function-length and secret gates |
| `unit`               | `pytest tests/unit` in random order with coverage (floor 40%), then `pytest bench`                                                                                                                                                                                               |
| `schema-check`       | a fresh ClickHouse (`dev/ci_start_clickhouse.sh`), every migration, the strict dry run, `make schema-check`                                                                                                                                                                      |
| `ts-typecheck`       | the dashboard build (`tsc -b` + Vite) and its bundle budget (`npm run budget`)                                                                                                                                                                                                   |
| `format`             | Biome, Prettier, Taplo and shfmt checks at pinned versions                                                                                                                                                                                                                       |
| `agent-plugins`      | `node --test` for the plugin hooks and the grep shim, a parse check of every hook, library and `bin/` helper script of both plugins and the shim, and a check that the grep shim's entry points are executable                                                                   |
| `frontend-tests`     | vitest (unit, component, axe) and Playwright                                                                                                                                                                                                                                     |
| `integration`        | ClickHouse, migrations, a booted server (fake providers, workers off), `pytest tests/integration`, then `pytest tests/analyzer`                                                                                                                                                  |
| `sdk-typescript`     | `sdk_ts_gen.py --check`, `npm run check` (lint, types, version, README examples, tests), `npm run check:dist`                                                                                                                                                                    |
| `sdk-python-package` | builds the SDK wheel with uv, installs it into a clean venv and runs `scripts/gates/sdk_wheel_smoke.py`                                                                                                                                                                          |

CI runs only on pull requests into `main`. A pull request stacked on another branch gets
no checks until it is retargeted to `main`, and a pull request with a merge conflict gets
none either: "no checks reported" after a push usually means one of the two.

## Related

- [Local development](./local-development.md), [code generation](./code-generation.md),
  [migrations](./migrations.md)
- [Benchmarks methodology](../benchmarks/methodology.md)
