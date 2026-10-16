---
name: loom-dev-setup
description: Set up an isolated Loom development environment for one task — a git worktree branched from origin/main, its own virtualenv, its own ClickHouse on free ports, migrations, and a server from source on its own port. Use when starting work on a Loom change, when asked to "set up", "create a worktree", "run the server", "run integration tests locally", or before any command that writes to ClickHouse.
argument-hint: "<branch-name>"
---

# Loom dev setup

Everything a task touches is private to it: the branch, the checkout, the virtualenv, the ClickHouse and the server port. Shared state (the primary checkout's `main`, the git stash, a ClickHouse someone else started, a deployment's containers) is read, never written.

## 1. Worktree

```bash
BRANCH="$ARGUMENTS"          # e.g. fix/search-floor
git fetch origin main
git worktree add -b "$BRANCH" "../loom-${BRANCH//\//-}" origin/main
cd "../loom-${BRANCH//\//-}"
```

- Branch from `origin/main`, never from a local `main` or `FETCH_HEAD` (a concurrent fetch rewrites `FETCH_HEAD`).
- Never `git checkout main`, `git pull` or `git reset` on `main` in a linked worktree: `refs/heads/main` is shared with the primary checkout.
- Never `git stash` (shared across worktrees). Use a WIP commit; squash merge removes it.

## 2. Virtualenv

```bash
uv sync --locked --python 3.12 --extra dev --extra code   # the lockfile CI installs: same ruff, mypy, pytest
.venv/bin/python -P -c 'import server; print(server.__file__)'   # must be inside this worktree
```

The make targets use `.venv/bin/python`. Run repository scripts as modules (`.venv/bin/python -m scripts.ops.dump_schema`): a script run by path can import `server` from another checkout.

## 3. A private ClickHouse

Pick free ports and a name that cannot be mistaken for a deployment's containers (`loom-blue`, `loom-green`, `loom-proxy`, `loom-server`, `loom-ch` are taken by convention):

```bash
export LOOM_CH_CONTAINER="ch-${BRANCH//\//-}" LOOM_CH_HOST_PORT=18123 LOOM_CH_NATIVE_HOST_PORT=19000
bash dev/ci_start_clickhouse.sh || exit 1
docker ps --filter "name=^${LOOM_CH_CONTAINER}$" --format '{{.Names}} {{.Ports}}'   # yours, on your ports
curl -s "http://loom:loomdev@localhost:$LOOM_CH_HOST_PORT/?query=SELECT+count()+FROM+system.tables+WHERE+database='loom'"   # 0 before the first migrate
```

The script refuses a port that is already taken (pick others and re-run), so a failed start cannot hand your migrations to another server. Then point Loom at it and migrate:

```bash
export LOOM_CH_HOST=localhost LOOM_CH_PORT=$LOOM_CH_HOST_PORT
make migrate
```

Remove it when done: `docker rm -f "$LOOM_CH_CONTAINER"` — check the name first; never `rm -f` a container you did not start.

## 4. Server from source

```bash
PORT=7790
LOOM_INSTANCE_LABEL="iso:$(basename "$PWD"):$PORT" LOOM_BUILD_COMMIT=$(git rev-parse --short HEAD) LOOM_BACKGROUND_WORKERS=off \
  .venv/bin/uvicorn server.main:app --app-dir "$PWD" --host 127.0.0.1 --port $PORT --loop asyncio --reload
curl -s localhost:$PORT/v1/health | jq '{status, build_commit, schema}'
```

`--app-dir "$PWD"` makes uvicorn serve this worktree's code; `build_commit` confirms it (a source run reports `unknown` without `LOOM_BUILD_COMMIT`). Workers stay off for the integration suite (a live supervisor drains the tests' rows).

The server and the tests read the same `.env` ([integration tests](../../../docs/development/testing.md#integration-tests)). To match CI's main integration pass, start the server with its posture: `LOOM_AUTH_REQUIRED=false LOOM_EMBEDDING_MODEL=fake-ngram-1024 LOOM_LLM_PROVIDER=fake LOOM_LATENCY_BUDGET_MULTIPLIER=10` (`.github/workflows/ci.yml`). A server with auth on fails dozens of tests written for that pass: that is the posture, not a bug. CI runs `test_cross_principal_leakage.py` a second time against an auth-on server.

## 5. Integration tests

```bash
export LOOM_BASE_URL=http://localhost:$PORT
make test-integration
.venv/bin/python -m pytest tests/integration -m "not slow and not destructive" -q   # quick pass
```

Tests read `LOOM_CH_HOST` and `LOOM_BASE_URL` from the environment or from the `.env` the session loads (`LOOM_ENV_FILE=` loads none), and skip without them; never export them to a shared or production server. Tests that need self-signup skip in CI, so new tests use `mint_test_token`.

## Reference

- [Local development](../../../docs/development/local-development.md), [testing](../../../docs/development/testing.md)
