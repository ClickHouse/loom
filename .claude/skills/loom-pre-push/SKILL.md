---
name: loom-pre-push
description: Runs the Loom CI gates a diff touches, locally, and fixes each failure at its source — format, lint, generator checks, ratchets, unit tests, schema, frontend, plugins, integration. Use before a commit or push, after a rebase, or when asked "is this ready". Not for opening the PR (loom-ship-pr).
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

Run them in the worktree's `.venv`, on CI's Python (3.12), with `LOOM_ENV_FILE=` so a local `.env` does not change what a snapshot or generator renders. CI also runs `pytest -p randomly`; an order-dependent failure reproduces with `pytest -p randomly --randomly-seed=<seed>`, the seed CI prints.

## 3. By path

| Paths touched | Also run |
| --- | --- |
| `schemas/migrations/` | `make schema-dump` against a private, freshly migrated ClickHouse (`loom-dev-setup`), then `make schema-check`; `git diff schemas/schema.sql` shows only your change |
| a migration, a row builder, an enum value, a wire format, worker leasing | `make test-mixed-version` — builds N-1 and N on one throwaway ClickHouse (needs docker; skips without it, CI fails) |
| `server/**/api/`, op registry | `.venv/bin/python -m scripts.codegen.<gen>` for `sdk_py_gen`, `sdk_py_client_gen`, `sdk_ts_gen`, `docs_reference`, `op_reach_census` (without `--check` each regenerates) |
| `server/settings/` | `.venv/bin/python -m scripts.codegen.settings_reference` |
| `server/cli/` | `LOOM_UPDATE_CLI_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_cli_surface_snapshot.py` in a 3.12 virtualenv (a newer Python renders the help differently) — then read the snapshot diff |
| a ClickHouse statement builder | `pytest tests/analyzer -q` against a private, migrated ClickHouse; regenerate with `LOOM_ENV_FILE= .venv/bin/python -m tests.analyzer.corpus --write` on fake providers ([analyzer corpus](../../../docs/development/testing.md#the-analyzer-statement-corpus)) |
| `server/workers/` | `LOOM_UPDATE_WORKER_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_worker_registry_snapshot.py` for an intended change |
| server behaviour | the integration suite against a private server (`loom-dev-setup` §7) |
| auth, write paths, erasure, migrations, the worker supervisor | the per-module branch-coverage floors CI enforces (about 10 minutes): `.venv/bin/python -m pytest tests/unit -q --cov=server --cov-branch --cov-report=json:coverage.json && .venv/bin/python -m tests.inventory.coverage_floors coverage.json` |
| `integrations/` | `.venv/bin/python -m scripts.codegen.agent_plugin_sync`; `node --test` in the plugin; for a behaviour change, both manifests bumped to a version no open PR uses |
| `frontend/` | `npm --prefix frontend run test && npm --prefix frontend run build && npm --prefix frontend run budget`; the Playwright run CI adds (`npx --prefix frontend playwright install --with-deps chromium` once, then `E2E_PORT=<free port> npm --prefix frontend run e2e`); `make lint` covers the accessibility rules |
| `deploy/helm/` | the commands of CI's "lint and validate every overlay" step (`.github/workflows/ci.yml`, job `helm`): a `secrets.jwtSecret` of at least 32 characters (CI uses `openssl rand -hex 32`; `helm lint` passes a shorter one, `helm template` refuses it), `helm lint --strict` and `helm template … \| kubeconform -strict -kubernetes-version 1.30.0` for each overlay, once more with every optional object on |
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
