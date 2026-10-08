# Test inventory and feature ledger

Two generated views of the suite, and a gate that keeps both honest.

## The inventory: one row per tracked file under `tests/`

```sh
python -m tests.inventory.build --summary          # counts by workflow, layer, status, tier
python -m tests.inventory.build --json --path 'tests/unit/test_memory_*'
```

Derived from the path and a static scan:

| Field                                       | Source                                                                                                                            |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `workflow`                                  | longest matching prefix in `_WORKFLOW_PREFIXES`. A new directory (a new fixture language, say) has none until one is added there. |
| `layer`                                     | unit, contract, analyzer, fixture, integration, typing, snapshot, support                                                         |
| `tier`                                      | unit or integration by directory; `slow` / `destructive` / `provider` markers override                                            |
| `targets`                                   | `server.*`, `loom.*`, `bench.*`, `scripts.*` imports                                                                              |
| `markers`, `services`, `isolation`, `clock` | literal hints: fixtures such as `control_plane_url` or `ch_client`, `monkeypatch`, `tmp_path`, seeded RNG, wall-clock reads       |
| `tags`                                      | file-name words (auth, lease, retry, recover, limit…) plus `pytest.raises` for negative cases                                     |
| `smells`                                    | `time.sleep` in a file with async tests, `pytest.mark.flaky` reruns                                                               |

The fields that need judgement come from `annotations/<area>.toml`. There is one file per area: suite, analyzer, integration, code, memory, platform.

```toml
[[files]]
match = "tests/unit/test_memory_search_*.py"   # fnmatch; `*` crosses `/`
owner = "memory search ranking"
status = "retain"          # unreviewed retain refactor consolidate generate archive deprecate remove investigate
tags = ["boundary"]        # positive negative boundary fault security concurrency recovery
covers = ["op:memory.search*"]   # feature ids this file exercises without naming them
tier = "unit"              # unit integration slow destructive provider manual
layer = "contract"
duplicates = ["tests/unit/test_other.py"]
runtime = "0.4 s"
flake = "order-dependent under seed N until the cache reset landed"
evidence = "why removal loses nothing"   # required when status = "remove"
notes = ""
```

List fields are merged across matching entries. Two entries that set a scalar field to different values for one file are an error.

## The ledger: product features and the tests that reference them

```sh
python -m tests.inventory.ledger                 # per kind: features, covered, by layer
python -m tests.inventory.ledger --uncovered     # what the gate would refuse
python -m tests.inventory.ledger --json --kind op
```

Features are read from their registries:

| Kind               | Registry                                                             |
| ------------------ | -------------------------------------------------------------------- |
| `op`               | `server.operations.registry.ALL_OPERATIONS`                          |
| `route`            | `@app` / `@router` HTTP decorators under `server/`                   |
| `setting`          | `Settings.model_fields`                                              |
| `worker`           | `server.workers.registry._WORKERS`                                   |
| `sdk-py`, `sdk-ts` | the Python SDK's `__all__` and `sdk/typescript/src/index.ts` exports |
| `ch`               | the objects in `schemas/schema.sql`                                  |
| `migration`        | `schemas/migrations/*.sql`                                           |
| `role`             | `Role`                                                               |
| `cli`              | the `loom` argparse tree                                             |
| `gate`             | `scripts/gates` and `scripts/codegen`                                |

A test file references a feature when it names it: the op name or MCP tool name, the setting field or its `LOOM_` env var, `loom.<table>`, and so on. A file's `covers` patterns also count. This is a static signal. It proves a test mentions the feature, not that the test checks the feature well. The layer cells (unit, integration, analyzer, bench, ts) and risk cells (security, fault, concurrency, recovery) show where the coverage is thin.

## `gaps.toml`: the ratchet

Every feature that no test references has a line with its owning area and a reason:

```toml
"op:memory.brief" = { owner = "memory", reason = "No test calls this operation by name." }
```

`tests/unit/test_test_inventory.py` fails when:

- a tracked file under `tests/` has no workflow, or has an unknown layer, status or tier;
- an annotation matches no tracked file;
- a feature has no test and no gap line. The failure prints the lines to add; `--uncovered` prints them too.

A gap line whose feature has gained a test, or no longer exists, is reported as a warning rather than a failure, because a new test is good news. `python -m tests.inventory.ledger --prune-gaps` deletes those lines. So a new operation, setting, worker or table ships with a test or an owned gap, and the list only shrinks. Keep the file sorted.

## Branch-coverage floors for critical modules

There is no repository-wide line-coverage target, because one rewards trivial tests. Instead, `coverage_floors.toml` sets a unit-suite branch-coverage floor for each module where an untested branch is a security, integrity or lifecycle risk, and says why that module is critical. Each floor sits about one point under the measured value.

```sh
pytest tests/unit --cov=server --cov-branch --cov-report=json:coverage.json
python -m tests.inventory.coverage_floors coverage.json   # exit 1 below a floor
```

`raise` in the output means a module now sits well above its floor. Raise the floor in the same PR. Lowering a floor needs its reason in the PR.
