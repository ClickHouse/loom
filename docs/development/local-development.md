# Local development

This page sets up a checkout for working on Loom's code. To evaluate Loom without changing it, the [quickstart](../getting-started/quickstart.md) (`loom up`) is shorter.

## Prerequisites

- **Python 3.12** for the server (the SDK supports 3.11 and later).
- **A C compiler and Python headers.** The `dev` extra pulls in `lemmagen3` (the lexical
  leg's lemmatizer), which has no wheel and compiles against `Python.h`:
  `sudo apt-get install -y build-essential python3-dev` on Debian/Ubuntu,
  `xcode-select --install` on macOS. A plain `pip install -e .` needs neither.
- **Docker** with Compose 2.34 or later (the dev compose file pulls ClickHouse daily),
  for ClickHouse and the stacks.
- **Node 22** (the version CI uses) for the dashboard and the TypeScript SDK.
- **Bash 5 or later** on `PATH` for the shell scripts and their unit tests. On macOS,
  `brew install bash` and `export PATH="$(brew --prefix)/bin:$PATH"`; check
  `bash --version` before running the tests. The system Bash 3.2 lacks associative
  arrays and `mapfile`.

## Python environment

The make targets run `.venv/bin/python` by default (`PYTHON ?= .venv/bin/python` in the
[`Makefile`](../../Makefile)); CI overrides it with `PYTHON=python`.

```bash
git clone https://github.com/ClickHouse/loom.git && cd loom

# with uv: the locked resolution CI installs
uv sync --locked --python 3.12 --extra dev --extra code

# or with pip
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,code]"
```

`make install` runs `pip install -e ".[dev]"` into the active interpreter.

| Extra            | Adds                                                                                        |
| ---------------- | ------------------------------------------------------------------------------------------- |
| `dev`            | test, lint and type tools, the lemmatizer                                                   |
| `code`           | tree-sitter grammars and numpy for the code index and its tests (CI installs `.[dev,code]`) |
| `local-embedder` | the CPU embedder                                                                            |
| `ingest`         | the S3 client `dev/loom_ingest.py` fetches an `s3://` source with                           |

## A local ClickHouse

Any ClickHouse 26.4 or later works ([ClickHouse requirements](../operations/deployment.md#clickhouse-requirements)); `loom ch-setup` prepares one Loom did not start. For a schema dump that matches CI, use the bundled config (the `loom_reader` user, the NLP flags, the Lemmagen3 dictionary and an embedded Keeper) through one of:

```bash
# 1. the CI container: same image and config CI uses, on 8123 / 9000
bash dev/ci_start_clickhouse.sh

# 2. only the ClickHouse service of the dev stack
make bootstrap    # creates .env and named_collections.xml from their templates
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d clickhouse
```

Both listen on `localhost:8123` with user `loom`, password `loomdev` and database `loom`, matching `LOOM_CH_*` in `.env.example`. Both pin the same image, so a schema dump against either matches CI ([migrations](./migrations.md#schema-dump-and-check)).

Then apply the schema:

```bash
make migrate          # python -m server.core.schema.migrate
make migrate-status   # applied and pending
make migrate-dry      # what would apply
```

## Env files

Settings are read once at startup from the environment and from the dotenv file named by `LOOM_ENV_FILE` (default `.env` in the working directory; empty reads none). `make bootstrap` creates `.env` from `.env.example`: `single-user` mode with fake providers, so no auth, no network, no key. The [configuration reference](../reference/configuration.md) lists every setting; [deployment](../operations/deployment.md) covers the essentials.

Compose reads the `.env` in the first `-f` file's directory. With `compose.yaml` first that is the root one; the cloud stack, whose first file is under `dev/`, needs `--env-file .env`. The make targets pass the flag everywhere.

## Running the server from source

```bash
make server     # uvicorn server.main:app on :7777 with --reload
```

`make server` sets `LOOM_INSTANCE_LABEL=dev:make-server:7777` unless you set one. A process outside the compose deployment refuses to boot unlabelled; the label keeps its ledger rows and spans apart from a deployment's on a shared ClickHouse. The server applies pending migrations at boot (`LOOM_BOOT_MIGRATE=true`) before it answers.

`loom serve [--host] [--port] [--reload]` does the same through the CLI.

**Running from a worktree or a second checkout.** uvicorn imports `server.main` from its `--app-dir`, which defaults to the current directory and wins over `PYTHONPATH`. A server started in one checkout with another's path on `PYTHONPATH` serves the first checkout's code. Start it from the checkout you mean, or pass `--app-dir`, with its own port and label:

```bash
LOOM_INSTANCE_LABEL=dev:$USER:7790 .venv/bin/uvicorn server.main:app \
  --app-dir "$PWD" --host 127.0.0.1 --port 7790 --loop asyncio --reload --reload-dir "$PWD/server"
```

Check `build_commit` and the boot log before trusting a measurement against it.

## The dashboard

The server serves `frontend/dist/` at `/` and answers 503 on dashboard pages until it is built. `/v1/admin/*` works either way.

```bash
make frontend-build   # npm install && npm run build (tsc -b + vite build) into frontend/dist/
make frontend-dev     # Vite on :5173, proxying /v1 and /mcp to the server on :7777
npm --prefix frontend run test    # vitest (unit, component, axe)
npm --prefix frontend run e2e     # Playwright specs in frontend/e2e/
```

Open `http://localhost:7777/` and paste a token (`loom mint-token … --save` writes `~/.loom/token`), or use it without one in `single-user` mode.

## Fake providers

The stock `.env` pins `LOOM_EMBEDDING_MODEL=fake-ngram-3072` and `LOOM_LLM_PROVIDER=fake`. The fake embedder is deterministic character n-grams at Loom's standard 3072-vector width; the fake LLM extracts nothing. The stack runs offline and tests are reproducible, but retrieval quality on it measures nothing. Benchmarks and quality gates need real providers ([running benchmarks](../benchmarks/running.md)). With fake providers, the adaptive similarity thresholds fall back to their scalar settings.

For real providers, set the provider block in `.env` ([deployment](../operations/deployment.md#providers)). The embedder's width is fixed when the vector tables are created, so recreate the local database when you change it (`make down-all`, or a fresh `dev/ci_start_clickhouse.sh` container).

## Make targets

Every target below exists in the [`Makefile`](../../Makefile).

| Target                                           | Does                                                                                                                                    |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `make bootstrap`                                 | creates `.env` and `deploy/clickhouse-config/named_collections.xml` if missing; gives an existing `.env` without an extractor token one |
| `make dev` / `make dev-cloud`                    | the local stack with bundled ClickHouse / Loom against the ClickHouse in `.env` (both `--build`)                                        |
| `make down` / `make down-all`                    | stop both stacks / also delete their volumes                                                                                            |
| `make server`                                    | the server from source with `--reload`                                                                                                  |
| `make migrate`, `migrate-status`, `migrate-dry`  | apply / list / preview migrations                                                                                                       |
| `make schema-dump`, `schema-check`               | regenerate / verify `schemas/schema.sql`                                                                                                |
| `make frontend-build`, `frontend-dev`            | build the dashboard / run Vite                                                                                                          |
| `make format`, `format-check`                    | ruff, Biome, Prettier, Taplo, shfmt (pinned versions)                                                                                   |
| `make lint`                                      | ruff, the function-length and secret gates, every generator's `--check`, Biome's dashboard accessibility rules                          |
| `make typecheck`                                 | the mypy ratchet (`mypy-gate`) and `mypy --strict` on the Python SDK                                                                    |
| `make test-unit`                                 | `pytest tests/unit` and `pytest bench`                                                                                                  |
| `make test` / `test-integration`                 | `pytest tests/integration` (needs a server and ClickHouse)                                                                              |
| `make test-unit-cov`                             | the unit suite with the coverage floor CI enforces                                                                                      |
| `make ingest FILE=… [TAGS=…]`                    | load a file into the knowledge plane with `dev/loom_ingest.py`                                                                          |
| `make init` / `make onboard NAMESPACE=… AGENT=…` | provision a service account and `.env` for an agent / plus raised limits and a smoke test                                               |
| `make bench OP=… C=… D=…`                        | concurrent load against one op (`scripts/ops/benchmark.py`)                                                                             |
| `make bench-quick`, `bench-*`                    | quality and regression gates; need real providers ([benchmarks](../benchmarks/running.md))                                              |
| `make clean`                                     | `down-all` and remove Python caches                                                                                                     |

Before a pull request, run `make format`, `make lint`, `make typecheck` and `make test-unit` ([testing](./testing.md)).

## Related

- [Repository guide](./repository-guide.md), [testing](./testing.md),
  [code generation](./code-generation.md)
- [Troubleshooting](../operations/troubleshooting.md#local-stack)
