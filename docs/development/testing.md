# Testing

Which suite a test belongs in, what each suite needs to run, how tests skip when their environment is missing, which gates CI enforces, and how to regenerate what they pin.

## The suites

| Suite                | Path                                                                 | Needs                                              | Run locally                                                    | In CI                                                          |
| -------------------- | -------------------------------------------------------------------- | -------------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------- |
| Unit                 | `tests/unit/`                                                        | nothing external; no ClickHouse                    | `make test-unit` (also runs `pytest bench -q`)                 | `unit` in parts, random order; `unit-coverage`, coverage ≥ 40% |
| Bench harnesses      | `bench/` (its `test_*` files)                                        | nothing external                                   | `pytest bench -q`                                              | `unit-coverage`                                                |
| Integration          | `tests/integration/`                                                 | a migrated ClickHouse and a running server         | `make test-integration` with `LOOM_BASE_URL`                   | `integration`                                                  |
| Analyzer corpus      | `tests/analyzer/`                                                    | a migrated ClickHouse (no server)                  | `pytest tests/analyzer -q`                                     | `integration`                                                  |
| Code lanes corpus    | `tests/code_lanes/`, gated by `tests/unit/test_code_lanes_corpus.py` | nothing external                                   | `python -m tests.code_lanes.corpus`                            | `unit`                                                         |
| Dashboard            | `frontend/src/**/*.test.tsx`                                         | Node                                               | `npm --prefix frontend run test` (vitest, with axe)            | `frontend-vitest`                                              |
| Dashboard end to end | `frontend/e2e/*.spec.ts`                                             | Node, Chromium (`npx playwright install chromium`) | `npm --prefix frontend run e2e`                                | `frontend-e2e`                                                 |
| Plugin hooks         | `integrations/*/tests/*.test.mjs`                                    | Node                                               | `node --test integrations/claude-code-plugin/tests/*.test.mjs` | `agent-plugins`                                                |
| TypeScript SDK       | `sdk/typescript/`                                                    | Node                                               | `npm --prefix sdk/typescript run check`                        | `sdk-typescript`                                               |
| Benchmarks and gates | `bench/*.py`, `make bench-*`                                         | a server with **real** providers                   | [running benchmarks](../benchmarks/running.md)                 | never (the CI embedder is fake)                                |

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
- **Random order, in parallel.** CI runs the suite with `-n auto -p randomly` on one
  worker process per core and prints the seed first, so a test must pass after any other
  and beside any other. [`tests/README.md`](../../tests/README.md#rules-every-test-keeps)
  has the rules and how to reproduce a failure from the seed and the log.
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
- Each test gets a wildcard-org writer token in `LOOM_API_KEY` unless you exported one
  (the `agent` fixture uses it), and the `mint_test_token` fixture mints a token for a
  test's own org. Both sign with the test process's `LOOM_JWT_SECRET`, which must match
  the server's.
- Run the server with `LOOM_BACKGROUND_WORKERS=off`: tests that exercise a worker call its
  `run_pass` in-process against their own namespace, and a live supervisor's workers have
  no namespace filter and would drain those tests' rows. A worker whose pass takes one item
  of a queue every tenant shares, such as `knowledge_ingest`, is exercised by running the
  test's own item as a pass runs one (`ingest_run.take_and_run`).
- `tests/integration/test_document_object_store_live.py` also needs an S3-compatible store
  in `LOOM_TEST_S3_ENDPOINT` (CI runs a local one, see `.github/workflows/ci.yml`) and
  skips without it.
- The publishing tests in `tests/integration/test_knowledge_ingest_live.py` run their own
  job, which sends originals to a document extractor: they need one routed for SQL and TOML
  (`LOOM_DOCUMENT_EXTRACTOR_URL` or `LOOM_DOCUMENT_EXTRACTORS_JSON`, with its token; see
  the [configuration reference](../reference/configuration.md)), and skip without one.
  `tests/integration/test_knowledge_inspect_ingest.py` also asserts, through
  `knowledge.inspect`, that the server has one configured, so give the server the same
  settings. CI starts the reference extractor before each part's server
  (`.github/workflows/ci.yml`; [running it](../../extractor/README.md)).
- `tests/integration/test_pg_authority.py` needs a server of the Postgres authority:
  start Postgres with `dev/ci_start_postgres.sh`, run `make migrate`, and set
  `LOOM_STORAGE_AUTHORITY=postgres` and
  `LOOM_PG_DSN=postgres://loom:loomdev@localhost:5432/loom` (the script's database;
  `LOOM_PG_HOST_PORT` moves the port) for both the server and pytest, since its tests
  also run the authority's code in the test process. A Postgres started with
  `LOOM_PG_STANDBY=0` (no synchronous standby) needs `LOOM_PG_STANDBY=0` in pytest's
  environment too, so the standby health test skips. Against a ClickHouse-authority server
  all but the tests that use the API alone skip.
- CI sets `LOOM_LATENCY_BUDGET_MULTIPLIER=10`: shared runners are noisy enough that the
  default per-op budgets fire violations.
- Tests that depend on self-signup skip in CI, which has no `LOOM_SELF_SIGNUP=open`; prefer
  minted tokens.
- **Tiers.** `tests/integration/conftest.py` registers four markers a quick local pass can
  deselect (`-m "not slow and not destructive"`); CI runs the whole directory, in parts
  (below). `slow` spends wall clock on purpose (lease lapses, retry exhaustion),
  `destructive` stops merges, runs `OPTIMIZE` or deletes whole scopes, `provider` needs a
  real embedder or LLM and skips on the fakes, `recovery` drives a failure and the path
  back from it.
- **Parts.** `LOOM_TEST_SHARD=k/n` collects only the test files of the k-th of n parts. A
  part holds whole files, so a module fixture is built in one part only. Files are dealt
  by the seconds `tests/shard_weights.json` records for each, and a file it does not list
  by its count of test functions times the recorded seconds per test function, or one
  second each when nothing is recorded, as for every integration file
  (`tests/_shards.py`). CI splits the suite into four parts, each on its own stack, beside
  the isolation gates part; a failure in part k reproduces with
  `LOOM_TEST_SHARD=k/4 make test-integration`.
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
shared fixtures follow the rule: `control_plane_url` skips without `LOOM_BASE_URL`. A
set `LOOM_BASE_URL` that does not answer 200 on `/v1/health` is a broken server, not a
missing one, and fails the test with the URL and the error.
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

| Gate                            | Where                                                                                 | Fails when                                                                                                                                                                                     |
| ------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Public copy                     | `tests/unit/test_public_copy.py`                                                      | the tree names an adjacent product, a person, an internal host or namespace, cites a paper or a design record; public pages name a competitor                                                  |
| Doc links                       | `tests/unit/test_doc_links.py`                                                        | a relative link or `#anchor` in the root Markdown, `docs/` or the integration READMEs does not resolve                                                                                         |
| Settings reference              | `tests/unit/test_settings_reference.py`, `settings_reference.py --check`              | the generated configuration reference differs from the settings sources, or a field has no row                                                                                                 |
| Docs reference                  | `tests/unit/test_docs_reference.py`, `docs_reference.py --check` (`make lint`)        | a generated region under `docs/reference/` is stale, or an op or renderer has no row                                                                                                           |
| Stock env matches docs          | `tests/unit/test_stock_env_matches_docs.py`                                           | the quickstart's stated stock `.env` values drift from `.env.example`, or a documented compose command lacks `--env-file .env`                                                                 |
| Function length                 | `scripts/gates/function_length_gate.py` (lint)                                        | a file gains an oversized function, or a listed function grows past its baseline                                                                                                               |
| mypy ratchet                    | `scripts/gates/mypy_gate.py` (`make typecheck`)                                       | any file has more strict-mypy errors than `scripts/gates/mypy_baseline.json`                                                                                                                   |
| Secrets                         | `scripts/gates/check_no_secrets.py` (lint), `tests/unit/test_no_secrets_guard.py`     | a tracked file carries a credential-shaped value                                                                                                                                               |
| Dashboard accessibility         | `biome lint frontend/src` (`make lint`, CI job `format`)                              | a dashboard `.tsx` adds an accessibility violation                                                                                                                                             |
| Cross-principal leakage         | `tests/integration/test_cross_principal_leakage.py`                                   | any read op, driven as another tenant with canary nonces, returns a nonce or a 500; payloads are synthesised from each op's input model, so a new read op is probed the day it lands           |
| Worker registry snapshot        | `tests/unit/test_worker_registry_snapshot.py`, `snapshots/worker_registry.json`       | the enabled worker set, order, cadence or deadline changes under the `defaults`, `all_on` or `production` profile                                                                              |
| Worker registry parity          | `tests/unit/test_worker_registry_parity.py`                                           | the workers `/v1/admin/workers.status` lists and the ones the supervisor runs differ                                                                                                           |
| Migrations additive             | `tests/unit/test_migrations_additive.py`                                              | a migration drops or renames a table or column, or exchanges tables outside a declared rebuild                                                                                                 |
| Migration numbers               | `tests/unit/test_migration_numeric_order.py`                                          | two migration files share a number                                                                                                                                                             |
| Schema check                    | `schema-check` job: strict dry run, then `make schema-check`                          | the ledger holds a file the repository lacks, or `schemas/schema.sql` differs from a replay of every migration                                                                                 |
| CLI surface snapshot            | `tests/unit/test_cli_surface_snapshot.py`, `snapshots/cli_tree.json`, `cli_help.json` | any `loom` parser, argument, default or `--help` text changes                                                                                                                                  |
| Op reach census                 | `tests/unit/test_op_reach_census.py`, `op_reach_census.py --check`                    | an op has no reach path other than raw HTTP and is not experimental, an op has neither a dashboard call site nor an entry in `server/operations/dashboard.py`, or `reach_manifest.py` is stale |
| Op surfaces                     | `tests/unit/test_op_surfaces.py`                                                      | an op's MCP routing, its hidden-op refusal or its entry in `server/operations/dashboard.py` disagrees with its reach                                                                           |
| MCP listing budget              | `tests/unit/test_mcp_listing_budget.py`                                               | the default `tools/list` grows past its token ceiling or points a model at a tool it cannot call                                                                                               |
| Settings access                 | `tests/unit/test_settings_attribute_access.py`                                        | code reads a setting with `getattr(settings, "<field>", default)`                                                                                                                              |
| Generated SDK and plugin copies | `sdk_py_gen.py`, `sdk_ts_gen.py`, `agent_plugin_sync.py` `--check` (lint)             | a generated file differs from its source ([code generation](./code-generation.md))                                                                                                             |
| Formatting                      | `format` job, plus `ruff format --check` in `lint` (`make format-check` locally)      | ruff, Biome, Prettier, Taplo or shfmt would change a file                                                                                                                                      |

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
`main`, with Python 3.12 (`sdk-python-package` adds other versions) and Node 22. A newer
push to a pull request cancels that pull request's running workflow; every push to `main`
runs to completion in its own concurrency group, so a failure on one merge commit is never
hidden by the next. Every action is pinned to a full commit SHA (its version in a
comment), and the workflow grants its jobs read-only repository access
(`permissions: contents: read`).

| Job                    | Runs                                                                                                                                                                                                                                                                                                                          |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `clickhouse`           | resolves the ClickHouse release `dev/clickhouse_release.sh` names, so `unit`, `schema-check` and `integration` test the same one                                                                                                                                                                                              |
| `lint`                 | `ruff check`, `ruff format --check`, the `--check` of `sdk_py_gen.py`, `sdk_py_client_gen.py`, `sdk_py_admin_gen.py`, `agent_plugin_sync.py`, `settings_reference.py`, `settings_schema.py`, `docs_reference.py` and `op_reach_census.py`, `mypy --strict` on the SDK, the mypy ratchet, the function-length and secret gates |
| `unit`                 | `pytest tests/unit` in six parts (`LOOM_TEST_SHARD`), each on its own runner, in random order on every core with coverage                                                                                                                                                                                                     |
| `unit-coverage`        | the parts' coverage combined (floor 40%) and the per-module floors, then `pytest bench` and the generic integration sample (`integrations/generic/test.sh`); its `shard-weights` artifact is the run's seconds per unit file, to commit as `tests/shard_weights.json` when the parts drift apart                              |
| `sandbox`              | the hostile repository-job sandbox suite under real bubblewrap, where a skip fails (`LOOM_TEST_REQUIRE_SANDBOX=1`)                                                                                                                                                                                                            |
| `schema-check`         | a fresh ClickHouse (`dev/ci_start_clickhouse.sh`), every migration, the strict dry run, `make schema-check`                                                                                                                                                                                                                   |
| `ts-typecheck`         | the dashboard build (`tsc -b` + Vite) and its bundle budget (`npm run budget`)                                                                                                                                                                                                                                                |
| `format`               | Biome, Prettier, Taplo and shfmt checks at pinned versions, and Biome's accessibility lint on `frontend/src`                                                                                                                                                                                                                  |
| `helm`                 | `helm lint --strict` and kubeconform on every overlay, kubeconform on `deploy/okf-sync/cronjob.yaml`, the values the chart must refuse, and `scripts/gates/helm_extractor_isolation.py`, `helm_metadata_egress.py` and `helm_assistant_isolation.py`                                                                          |
| `agent-plugins`        | `node --test` for the plugin hooks and the grep shim, the Cursor hook sample (`integrations/cursor/test.sh`), a parse check of every hook, library and `bin/` helper script of both plugins and the shim, and a check that the grep shim's entry points are executable                                                        |
| `frontend-vitest`      | vitest (unit, component, axe), in parts on separate runners                                                                                                                                                                                                                                                                   |
| `frontend-e2e`         | Playwright, in parts on separate runners                                                                                                                                                                                                                                                                                      |
| `integration`          | parts, each with its own ClickHouse, migrations, reference extractor and booted server (fake providers, workers off): the numbered parts split `pytest tests/integration` (`LOOM_TEST_SHARD`), the isolation gates part the provider staging tests, the auth-on gates and `pytest tests/analyzer`                             |
| `integration-postgres` | the memory, knowledge, isolation, idempotency, erasure and restored-id suites and the auth-on gates against a server with `LOOM_STORAGE_AUTHORITY=postgres`, on Postgres 16 and 18 with a synchronous standby ([Postgres](../operations/postgres.md))                                                                         |
| `mixed-version`        | `scripts/gates/mixed_version.py`: builds N-1 and N side by side on one ClickHouse, N-1 being `origin/main` on a pull request and the previous commit (`HEAD~1`) on `main`                                                                                                                                                     |
| `sdk-typescript`       | `sdk_ts_gen.py --check`, `npm run check` (lint, types, version, README examples, tests), `npm run check:dist`                                                                                                                                                                                                                 |
| `sdk-python-package`   | builds the SDK wheel with uv, installs it into a clean venv and runs `scripts/gates/sdk_wheel_smoke.py`, on Python 3.11, 3.12 and 3.14, plus 3.11 and 3.14 with every declared dependency at its lower bound                                                                                                                  |

`.github/workflows/plugin-platforms.yml` runs when a change touches `integrations/`, a
plugin marketplace, `.gitattributes` or the workflow itself: the plugin and grep-shim
suites on Linux (x86_64, arm64, Alpine), macOS (Apple silicon, Intel), Windows (x86_64,
arm64) and WSL 2 (Ubuntu, Node 18), then real Claude Code and Codex sessions with the
plugin installed from the checkout (`tests/e2e/agent-session.mjs` under the Claude Code
plugin: a local fake answers both the model API and Loom), also with `node` off the
agent's `PATH` on the Ubuntu and macOS runners and without Git Bash on Windows.

A pull request stacked on another branch gets no checks until it is retargeted to `main`,
and a pull request with a merge conflict gets none either: "no checks reported" after a
push usually means one of the two.

## Related

- [Local development](./local-development.md), [code generation](./code-generation.md),
  [migrations](./migrations.md)
- [Benchmarks methodology](../benchmarks/methodology.md)
