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
cd "../loom-${BRANCH//\//-}" && pwd   # the worktree's absolute path: <worktree> below
```

An agent's shell does not keep its working directory or variables between calls, so every block below starts with `cd <worktree> &&`, the absolute path printed here. Without it, `uv sync` rewrites the launch checkout's `.venv` and the server serves the launch checkout's code.

- Branch from `origin/main`, never from a local `main` or `FETCH_HEAD` (a concurrent fetch rewrites `FETCH_HEAD`).
- Never `git checkout main`, `git pull` or `git reset` on `main` in a linked worktree: `refs/heads/main` is shared with the primary checkout.
- Never `git stash` (shared across worktrees). Use a WIP commit; squash merge removes it.

## 2. Virtualenv

```bash
cd <worktree> && uv sync --locked --python 3.12 --extra dev --extra code   # the lockfile CI installs: same ruff, mypy, pytest
.venv/bin/python -P -c 'import server; print(server.__file__)'   # must be inside this worktree
```

The make targets use `.venv/bin/python`. Run repository scripts as modules (`.venv/bin/python -m scripts.ops.dump_schema`): a script run by path can import `server` from another checkout.

## 3. A private ClickHouse

Pick free ports and a name that cannot be mistaken for a deployment's containers (`loom-blue`, `loom-green`, `loom-proxy`, `loom-server`, `loom-workers`, `loom-<colour>-workers`, `loom-ch` are taken by convention):

```bash
cd <worktree> && LOOM_CH_CONTAINER="ch-$(basename "$PWD")" LOOM_CH_HOST_PORT=18123 LOOM_CH_NATIVE_HOST_PORT=19000 bash dev/ci_start_clickhouse.sh
docker ps --filter "name=^ch-$(basename "$PWD")$" --format '{{.Names}} {{.Ports}}'   # yours, on your ports
curl -s "http://loom:loomdev@localhost:18123/?query=SELECT+count()+FROM+system.tables+WHERE+database='loom'"   # 0 before the first migrate
```

The script refuses a port that is already taken (pick others and re-run), so a failed start cannot hand your migrations to another server. Remove it when done: `docker rm -f "ch-$(basename "$PWD")"` — check the name first; never `rm -f` a container you did not start.

## 4. One `.env` for this worktree

An agent's shell keeps no variables between calls, and a server started without them falls back to `localhost:8123` and migrates whatever answers there at boot. Put the worktree's values in its own `.env` (ignored; a fresh worktree has none), which the server, `make migrate` and the tests all read:

```bash
cd <worktree>
bash -c '(exec 3<>/dev/tcp/127.0.0.1/7790)' 2>/dev/null && {   # bash's /dev/tcp; zsh has none
  echo "port 7790 is taken: pick another for the label, URL and --port"; exit 1; }
test -e .env && { echo ".env exists: check its LOOM_CH_* before migrating"; exit 1; }
cat > .env <<ENV
LOOM_CH_HOST=localhost
LOOM_CH_PORT=18123
LOOM_BASE_URL=http://localhost:7790
LOOM_BACKGROUND_WORKERS=off
LOOM_JWT_SECRET=$(openssl rand -hex 16)
LOOM_AUTH_REQUIRED=false
LOOM_EMBEDDING_MODEL=fake-ngram-1024
LOOM_LLM_PROVIDER=fake
LOOM_LATENCY_BUDGET_MULTIPLIER=10
LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS=1
ENV
make migrate
```

The last five lines are the posture of CI's main integration pass (`.github/workflows/ci.yml`). A server with auth on fails dozens of tests written for that pass: that is the posture, not a bug. CI runs `test_cross_principal_leakage.py` a second time against an auth-on server. Workers stay off: a live supervisor drains the tests' rows.

## 5. Server from source

Run it in the background (`run_in_background`, or `&`), from the worktree root:

```bash
cd <worktree> && LOOM_INSTANCE_LABEL="iso:$(basename "$PWD"):7790" LOOM_BUILD_COMMIT=$(git rev-parse --short HEAD) \
  .venv/bin/uvicorn server.main:app \
  --app-dir "$PWD" --host 127.0.0.1 --port 7790 --loop asyncio --reload
```

Check it in a separate call, by its label rather than its commit (two worktrees fresh from `origin/main` report the same `build_commit`): `curl -s localhost:7790/v1/health | jq -e --arg l "iso:<worktree basename>:7790" '.instance_label == $l and .status == "ok"'`. A `false` means another server holds the port. `--app-dir "$PWD"` makes uvicorn serve this worktree's code; `build_commit` confirms it (a source run reports `unknown` without `LOOM_BUILD_COMMIT`). The label is read from the process environment, not `.env`: without it the server refuses to start.

## 6. Integration tests

```bash
cd <worktree> && make test-integration
cd <worktree> && .venv/bin/python -m pytest tests/integration -m "not slow and not destructive" -q   # quick pass
```

Tests read `LOOM_CH_HOST` and `LOOM_BASE_URL` from the environment or from the `.env` the session loads (`LOOM_ENV_FILE=` loads none) and skip without them ([integration tests](../../../docs/development/testing.md#integration-tests)); never point them at a shared or production server. Tests that need self-signup skip in CI, so new tests use `mint_test_token`.

## Reference

- [Local development](../../../docs/development/local-development.md), [testing](../../../docs/development/testing.md)
