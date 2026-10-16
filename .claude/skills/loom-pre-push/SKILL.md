---
name: loom-pre-push
description: Run the Loom CI gates locally for the paths a diff touches — format, lint, the generators' --check, the mypy and function-length ratchets, unit tests, schema check, frontend and plugin tests, integration tests — and fix each failure at its source. Use before committing or pushing a Loom change, when asked "is this ready" or "run the checks", or after a rebase onto main. NOT for opening or merging the PR (loom-ship-pr) or diagnosing a red CI job (loom-investigate-ci).
---

# Loom pre-push gates

CI runs every gate below on each pull request into `main`. Running the ones the diff touches locally turns a 20-minute CI round trip into a 2-minute one.

## 1. What changed

```bash
git fetch origin main
git diff --name-only origin/main...HEAD
git status --short          # untracked files are part of the change too
```

## 2. Always

```bash
make format                 # rewrites; commit the result
make lint                   # ruff, function-length + secret gates, every generator --check
make typecheck              # mypy --strict on the SDK + the server ratchet
make test-unit              # tests/unit (random order in CI) + bench
```

Run them in the worktree's `.venv`. CI also runs `pytest -p randomly`; an order-dependent failure reproduces with `pytest -p randomly --randomly-seed=<seed>`, the seed CI prints.

## 3. By path

| Paths touched | Also run |
| --- | --- |
| `schemas/migrations/` | `make schema-dump` against a private, freshly migrated ClickHouse (`loom-dev-setup`), then `make schema-check`; `git diff schemas/schema.sql` shows only your change |
| `server/**/api/`, op registry | `.venv/bin/python -m scripts.codegen.<gen>` for `sdk_py_gen`, `sdk_py_client_gen`, `sdk_ts_gen`, `docs_reference`, `op_reach_census` (without `--check` each regenerates) |
| `server/settings/` | `.venv/bin/python -m scripts.codegen.settings_reference` |
| `server/cli/` | `LOOM_UPDATE_CLI_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_cli_surface_snapshot.py` — then read the snapshot diff |
| `server/workers/` | `LOOM_UPDATE_WORKER_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_worker_registry_snapshot.py` for an intended change |
| server behaviour | the integration suite against a private server (`loom-dev-setup` §5) |
| auth, write paths, erasure, migrations, the worker supervisor | the per-module branch-coverage floors CI enforces (about 10 minutes): `.venv/bin/python -m pytest tests/unit -q --cov=server --cov-branch --cov-report=json:coverage.json && .venv/bin/python -m tests.inventory.coverage_floors coverage.json` |
| `integrations/` | `.venv/bin/python -m scripts.codegen.agent_plugin_sync`; `node --test` in the plugin; both manifests bumped to a version no open PR uses |
| `frontend/` | `npm --prefix frontend run test && npm --prefix frontend run build && npm --prefix frontend run budget` |
| `sdk/typescript/` | `npm --prefix sdk/typescript run check` |
| `docs/`, any `.md` | `.venv/bin/python -m pytest tests/unit/test_doc_links.py tests/unit/test_public_copy.py` |

## 4. Read the regenerated diff

A generator's output is part of the review. `git diff --stat` after regenerating: a file you did not expect to change means the source change reached further than intended.

## 5. Fix at the source

- A ratchet (`function_length_gate.py`, `mypy_gate.py`) failing on your file: split the function, fix the types. `--update` only ever lowers a baseline.
- A public-copy failure: rewrite the text as the technique, without the name, date or history. The digest list is not edited to let a name through.
- A doc-link failure: fix the link or the anchor; anchors are the heading lowercased with spaces as `-`.
- Never skip, `xfail` or loosen a test to get green. A test that is wrong is fixed in the same change, with the reason in the PR body.

## Done when

Every command above that applies exits 0 and `git status` shows only the files you mean to commit.
