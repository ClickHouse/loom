# Loom test suite

| Directory                                                                 | Layer                                                                                        | Needs                                                | CI job                |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------- | --------------------- |
| `tests/unit/`                                                             | unit and contract tests: pure logic, handlers against fakes, registry and snapshot contracts | nothing: no network, no ClickHouse, no credential    | `unit` (random order) |
| `tests/unit/snapshots/`, `tests/unit/sdk_typing/`, `tests/unit/fixtures/` | pinned surfaces, type-contract cases, recorded inputs                                        |                                                      | `unit`                |
| `tests/integration/`                                                      | real serialization, auth, ClickHouse semantics, transports, workers                          | a control plane (`LOOM_BASE_URL`) and its ClickHouse | `integration`         |
| `tests/analyzer/`                                                         | every statement builder's shapes through the ClickHouse analyzer                             | a migrated ClickHouse                                | `integration`         |
| `tests/code_lanes/`, `tests/fixtures/code_lanes/`                         | the multilingual extraction corpus and its expected rows                                     | nothing                                              | `unit`                |
| `tests/inventory/`                                                        | the inventory and feature ledger ([README](inventory/README.md))                             | git                                                  | `unit`                |

## Tiers and markers

Marks must be registered (`strict_markers`). An unknown mark fails collection.

| Marker        | Meaning                                                           | Where it runs                |
| ------------- | ----------------------------------------------------------------- | ---------------------------- |
| none          | ordinary test of its directory's layer                            | the directory's job          |
| `slow`        | takes more than a few seconds                                     | `integration`                |
| `destructive` | stops merges, or drops or rewrites shared objects                 | a disposable ClickHouse only |
| `provider`    | calls a real LLM or embedding provider                            | skips without its key        |
| `recovery`    | kills, restarts or resumes a worker, saga or migration mid-flight | `integration`                |

`pytest.mark.flaky` (rerunfailures) hides a flake instead of fixing it. The inventory lists every use as a smell.

## Rules every test keeps

- **Order-independent and parallel-safe.** The unit job runs in parts on separate runners (`LOOM_TEST_SHARD`, `tests/_shards.py`), each under pytest-randomly with a printed seed and one pytest-xdist worker per core. A test writes only under `tmp_path` and listens only on port 0, since another test runs beside it. To reproduce a failure in part k of n, prefix the commands that name `tests/unit` with `LOOM_TEST_SHARD=k/n`, so they shuffle the same files the part did (in a checkout with no untracked test files, which would join the split); run the ones that name test ids without it, since the split is worked out from the paths on the command line:
  - the seed's order: `pytest tests/unit -p randomly --randomly-seed=<seed>`, without `-n`. The seed fixes the order, but xdist hands tests to workers as they free up, so only a run without `-n` repeats it exactly;
  - a neighbour that breaks it: `pytest -p randomly --randomly-seed=<seed> --randomly-dont-reorganize <earlier ids> <failing id>`, which runs the ids in the order given with the same per-test random seeds as CI. The CI log does not say which tests shared the failing test's worker; the earlier ids are the ones before the failing test in `pytest tests/unit -p randomly --randomly-seed=<seed> --collect-only -q`. Start with those from its own file, and halve them until one is left;
  - two tests colliding while they run at once: the same with `-n 2` and the two ids.
- **Collection is deterministic.** Every xdist worker collects the suite and must get the same test ids, so a parametrize id never comes from `uuid4()`, the clock or a random draw.
- **State goes back as it was found.** `tests/unit/conftest.py` holds autouse guards. Each one resets its state and fails the test that leaked it:
  - settings fields: use the `settings_override` fixture or `monkeypatch.setattr(settings, ...)`;
  - the auth principal; the query tag and the cost accumulator (reset, not failed);
  - environment variables, the working directory, `logging.disable` and non-daemon threads left running: use `monkeypatch`, and join what you start. The root logger (handlers, level and the CLI's logging latch) is reset after every test without failing it. `sys.path` is not checked, because bench modules insert their directory on first import;
  - pending tasks on the session loop;
  - the LLM and embedder singletons. A test double left in either cache fails the test, and the embedding cache is emptied after every test;
  - a test double bound at import: a repository module outside `tests/` and dot directories (`server/`, `scripts/`, `bench/`, ...) first imported while a test patches a name it imports (`from pkg import Name` under `patch("pkg.Name")`) would keep the double for the rest of the process. A double is a mock (an autospec'd function included), an instance of a class defined under `tests/`, or a function or class defined under `tests/`; a `SimpleNamespace`, a `partial` or a plain value is not one. After such a test every repository module first imported in it, `tests/` helpers included, is dropped (reset, not failed), so the next import binds the real names. A module first imported in a module-, class- or session-scoped fixture's setup is outside that window.

  They also empty the module caches (search key axis, corpus stats, error codes, deferred ledgers) before and after every test.

- **Async tests.** The event loop is session-scoped (`asyncio_mode = "auto"`), so `@pytest.mark.asyncio` is optional. Use `await asyncio.sleep(...)`, never `time.sleep`. To wait for a condition, poll with a bounded `time.monotonic()` deadline rather than a fixed sleep.
- **Deterministic time and randomness.** Inject a clock (a `_utcnow`-style seam) instead of comparing with the wall clock, and pass seeds explicitly. A dated fixture compared against today's date will eventually fail.
- **Unit tests never read the developer's environment.** The unit conftest removes `LOOM_*` and provider keys before `server.settings` loads, and it shadows the integration suite's `.env` loader and token minting.

## Shared unit helpers

Each helper is narrow, typed and has its own test. A helper builds the input or plays a boundary; the scenario and its assertions stay in the test.

| Module                             | What it gives a test                                                                 |
| ---------------------------------- | ------------------------------------------------------------------------------------ |
| `tests/unit/ch_fake.py`            | `RecordingCH`: routed reads, recorded writes, and a failure on an unrouted read      |
| `tests/unit/llm_fake.py`           | `ScriptedLLM`: scripted replies and faults, recorded prompts                         |
| `tests/unit/clock_fake.py`         | `ManualClock`: time that moves only when told, and a record of every sleep           |
| `tests/unit/principals.py`         | `principal(...)` builds an `AuthInfo`; `acting_as(...)` sets it and always resets it |
| `tests/unit/memory_rows.py`        | a full `loom.memory` FINAL projection row                                            |
| `tests/unit/schema_ddl.py`         | one object's DDL and columns from `schemas/schema.sql`                               |
| `tests/unit/_tracker_config.py`    | the tracker source and credential JSON, the way a deploy sets them                   |
| `tests/unit/_memory_search_pkg.py` | patching a name every module of `server/memory/app/search/` binds                    |

A new shared helper earns its place when several independent files need the same stable boundary. Don't create a catch-all utils module.

## Skip policy

A test whose prerequisite is absent skips at setup with a reason that says how to supply it. It never errors during collection.

- A missing env var: `os.environ.get(...) or pytest.skip("set X to …")`.
- A service that is configured but unhealthy: the `control_plane_url` fixture probes `/v1/health` and skips with the URL and the error.
- A quality assertion that only holds on a real embedder: `require_real_embedder`.

A skip is not a pass. When CI provides the prerequisite, the test must run there.

## Integration isolation

Every scenario mints its own identities, so tests never share rows:

- namespaces (`test-<random>`) and agents;
- tokens, through `mint_test_token` with the test's own org.

Cleanup runs on failure too, and it is checked by reading the data back, not by trusting a delete response. `ch_client` forces synchronous inserts, so a row is readable as soon as the insert returns.

## Adding a test

Put it in the layer that matches the boundary it checks, and name it after the behaviour it pins, not after an issue number. Then run:

```sh
pytest tests/unit/test_x.py -p randomly --randomly-seed=1   # and two more seeds
make format lint typecheck test-unit
```

The inventory gate adds the new file's row automatically. If the test adds the first reference to a feature listed in `tests/inventory/gaps.toml`, run `python -m tests.inventory.ledger --prune-gaps`.
