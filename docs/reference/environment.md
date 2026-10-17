# Environment variables reference

Almost everything Loom is configured with is a settings field: the
environment variable `LOOM_<FIELD>` of a field on `server.settings.Settings`,
listed with its default and meaning in the generated
[configuration reference](./configuration.md). This page covers the rest: the
variables read directly with `os.environ` (or a shell `${VAR}`) by the server,
the CLI, the SDKs, the container image, the compose files and the operator
scripts. They are hand-listed here because no single registry holds them;
check a name against the file in the **Read by** column before relying on it.

## Server

| Variable                                             | Read by                                                    | Default              | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------------------------- | ---------------------------------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `OPENAI_API_KEY`                                     | `server/embedding/openai.py`, `server/llm/openai.py`       | unset                | Key for the OpenAI embedder and LLM. Missing while an OpenAI model is configured: the embedder falls back to the fake n-gram embedder with an ERROR log, the LLM to the fake client, and `loom doctor` warns.                                                                                                                                                                                                                                                                   |
| `ANTHROPIC_API_KEY`                                  | `server/llm/anthropic.py`                                  | unset                | Key for the Anthropic LLM provider (`LOOM_LLM_PROVIDER=anthropic`, or inferred from a `claude*` model).                                                                                                                                                                                                                                                                                                                                                                         |
| `GEMINI_API_KEY`, `GOOGLE_API_KEY`                   | `server/embedding/gemini.py`                               | unset                | Key for the Gemini embedder; the first one set wins.                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `COHERE_API_KEY`                                     | `server/memory/app/rerank.py`                              | unset                | Key for the hosted rerank provider. Without it the reranker logs a warning and returns candidates in their input order (fail-open).                                                                                                                                                                                                                                                                                                                                             |
| `LOOM_ENV_FILE`                                      | `server/core/env_file.py`, `server/cli/app.py`             | `.env`               | The env file `Settings` reads, and the one the `loom` CLI loads into the environment. Empty for none.                                                                                                                                                                                                                                                                                                                                                                           |
| `LOOM_INSTANCE_LABEL`                                | `server/observability/instance.py`                         | unset                | A label for this process on every ledger row (up to 64 characters). A process started without it and without `LOOM_DEPLOYMENT=1` (a settings field the shipped compose files set) refuses to boot, so every row is attributable.                                                                                                                                                                                                                                                |
| `LOOM_CH_CAPTURE_STATEMENTS`                         | `server/core/ch.py`                                        | unset                | A file path: append one JSON line (`digest`, `sql`) per executed ClickHouse statement. For collecting the statement shapes an acceptance run executes; never set it in production.                                                                                                                                                                                                                                                                                              |
| `XDG_RUNTIME_DIR`                                    | `server/core/runtime_files.py`                             | unset                | Where one server's web processes keep their shared files (the rate-limit table) when `LOOM_RUNTIME_DIR` is unset: the first of `$XDG_RUNTIME_DIR/loom`, `/run/loom` and `/tmp/loom-<uid>` that is, or can be made, a directory only that user can enter; with several web processes and none, `scripts/serve.sh` starts one when it chose the count itself, and otherwise stops and names them. `scripts/serve.sh` makes this choice once and exports it as `LOOM_RUNTIME_DIR`. |
| `HOSTNAME`                                           | `server/observability/instance.py`                         | the system host name | Fallback host name in the `hostname:pid` instance id.                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `POD_NAMESPACE`, `POD_NAME`                          | `server/core/provider_process.py`                          | unset                | This pod's namespace and name (the chart sets them from the downward API). The workers leader retires a lost pod's provider participant once the API in that namespace no longer has the pod, or has it failed.                                                                                                                                                                                                                                                                 |
| `KUBERNETES_SERVICE_HOST`, `KUBERNETES_SERVICE_PORT` | `server/core/provider_process.py`                          | set by Kubernetes    | The API server the lost-pod check asks, with the service-account token.                                                                                                                                                                                                                                                                                                                                                                                                         |
| `NODE_OPTIONS`                                       | `server/code/workers/scip_ts_delta.py`, `scip_py_delta.py` | unset                | Passed through to the compiler-tier indexer subprocess; every other variable of that subprocess is set by the worker.                                                                                                                                                                                                                                                                                                                                                           |
| `SCIP_ENV_JSON`                                      | `server/code/workers/scip_py_delta.py`                     | unset                | Passed through to the Python compiler-tier indexer.                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `PATH`                                               | `server/code/workers/scip_go_delta.py`                     | inherited            | Searched for the Go toolchain and indexer binaries; the subprocess gets its own `PATH`.                                                                                                                                                                                                                                                                                                                                                                                         |

Provider selection itself (`LOOM_EMBEDDING_MODEL`, `LOOM_LLM_PROVIDER`,
`LOOM_LLM_MODEL`) is settings; see
[Deployment](../operations/deployment.md#providers) and
[LLM architecture](../architecture/llm-architecture.md).

## Container image and uvicorn

Read by `scripts/serve.sh`, the image's command, which starts uvicorn. It takes
them from its environment only, not from `.env` or `LOOM_ENV_FILE` (compose's
`env_file:` sets them in the environment), except `LOOM_WEB_WORKERS`, which it
also reads from the env file the server reads, as it does the settings field
`LOOM_RUNTIME_DIR`:

| Variable                   | Default              | Meaning                                                                                                                                                                                                                                                                                                                                                              |
| -------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PORT`                     | `7777`               | The port uvicorn binds inside the container.                                                                                                                                                                                                                                                                                                                         |
| `LOOM_LISTEN_HOST`         | `127.0.0.1`          | `uvicorn --host`: the address it listens on. The image sets `0.0.0.0`; `LOOM_BIND_HOST` stays the address the startup checks read.                                                                                                                                                                                                                                   |
| `LOOM_WEB_WORKERS`         | one per CPU, up to 8 | `uvicorn --workers`. Unset, one per CPU the container may use (its cpuset, lowered by a CPU quota), at most one per GiB of memory and at most 8. The compose files set it unless `.env` names a count: 2 in `deploy/standalone` and the dev stack, 1 in the cloud, colour and read-replica stacks. Also a settings field, which the rate limiter divides budgets by. |
| `LOOM_UVICORN_LOOP`        | `asyncio`            | `uvicorn --loop`: `asyncio`, `auto` or `uvloop`. uvloop forks the whole process for each git spawn, which stalls its loop.                                                                                                                                                                                                                                           |
| `LOOM_TRUSTED_PROXIES`     | `127.0.0.1`          | `uvicorn --forwarded-allow-ips`: the peers whose `X-Forwarded-For`/`-Proto` are applied.                                                                                                                                                                                                                                                                             |
| `LOOM_GRACEFUL_SHUTDOWN_S` | `10`                 | `uvicorn --timeout-graceful-shutdown`: how long a stopping process drains requests.                                                                                                                                                                                                                                                                                  |
| `LOOM_KEEP_ALIVE_S`        | `5`                  | `uvicorn --timeout-keep-alive`: how long an idle connection stays open. Behind a load balancer that reuses connections, longer than its idle timeout.                                                                                                                                                                                                                |

Build arguments: `LOOM_EMBEDDING_MODEL` (a value starting with `local` adds the
local CPU embedder's dependencies to the image; switching between a local and a
cloud embedder rebuilds that layer) and `LOOM_BUILD_COMMIT` (baked into the
image as `LOOM_BUILD_COMMIT`, reported as `build_commit`). The remaining
`ARG`s pin toolchain versions and checksums for the compiler-tier indexers
(`GO_VERSION`, `NODE_VERSION`, `SCIP_GO_VERSION`, `SCIP_TYPESCRIPT_VERSION`,
`SCIP_PYTHON_VERSION`, `PNPM_VERSION` and their `*_SHA256*` values).

## Compose files

| Variable                                        | Read by                                                    | Default                               | Meaning                                                                                                                                       |
| ----------------------------------------------- | ---------------------------------------------------------- | ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CH_HOST_PORT`, `LOOM_CH_NATIVE_HOST_PORT` | `dev/docker-compose.dev.yml`, `dev/ci_start_clickhouse.sh` | `8123`, `9000`                        | Host ports the local ClickHouse publishes (HTTP and native).                                                                                  |
| `LOOM_CH_CONTAINER`                             | `dev/ci_start_clickhouse.sh`                               | `loom-ch`                             | Container name, so a second ClickHouse (one per worktree) runs beside the first.                                                              |
| `LOOM_COLOUR`, `LOOM_COLOUR_PORT`               | `dev/docker-compose.colour.yml`                            | required                              | Which colour (`blue` or `green`) and host port (`7781` or `7782`) a blue/green project runs.                                                  |
| `LOOM_CLICKHOUSE_IMAGE`                         | `compose.yaml`, `dev/ci_start_clickhouse.sh`               | `clickhouse/clickhouse-server:latest` | The ClickHouse image the stack runs. A release bundle's compose file defaults it to the release the bundle was built and schema-checked with. |

Other `${LOOM_*}` references in the compose files are settings fields given a
compose-level default, the uvicorn variables above, and the standalone stack's
image and published port (`LOOM_IMAGE_REPOSITORY`, `LOOM_VERSION`, `LOOM_PORT`), which
the installer keeps in `.env` when given, as it does `LOOM_CLICKHOUSE_IMAGE`;
the [quickstart](../getting-started/quickstart.md) describes what the stock
`.env` sets.

## `loom` CLI

| Variable                           | Read by                                         | Default                     | Meaning                                                                       |
| ---------------------------------- | ----------------------------------------------- | --------------------------- | ----------------------------------------------------------------------------- |
| `LOOM_BASE_URL`                    | `server/cli/_client.py` and the client commands | `http://localhost:7777`     | The server the client commands (`install`, `demo`, `export-okf`, …) talk to.  |
| `LOOM_API_KEY`                     | `server/cli/_client.py` and the client commands | unset, then `~/.loom/token` | The token they present. A stale value here wins over `~/.loom/token`.         |
| `LOOM_BIND_HOST`, `LOOM_BIND_PORT` | `server/cli/serve.py`                           | `0.0.0.0`, `7777`           | Defaults for `loom serve --host` and `--port`.                                |
| `LOOM_CONFIG_FILE`                 | `server/cli/_client.py`                         | `~/.loom/config.json`       | The client config `loom doctor --client` reads (the file `loom init` writes). |

See [CLI reference](./cli.md).

## SDKs

| Variable            | Read by                    | Default                                             | Meaning                                                                     |
| ------------------- | -------------------------- | --------------------------------------------------- | --------------------------------------------------------------------------- |
| `LOOM_BASE_URL`     | Python and TypeScript SDKs | Python `http://localhost:7777`; TypeScript required | The server, when the constructor gives none.                                |
| `LOOM_API_KEY`      | Python and TypeScript SDKs | Python falls back to `~/.loom/token`                | The bearer token, when the constructor gives none. Blank counts as unset.   |
| `LOOM_LOG_LEVEL`    | Python and TypeScript SDKs | unset                                               | The SDK logger's level (`debug`, `info`, `warning`/`warn`, `error`, `off`). |
| `LOOM_MENTOR_BRIEF` | Python and TypeScript SDKs | off                                                 | `on` fetches `mentor.brief` in the background on the first call.            |
| `LOOM_AUTO_RUN`     | Python SDK                 | off                                                 | Bind an ambient run to each `Agent`'s lifetime.                             |

See [Python SDK](./python-sdk.md) and [TypeScript SDK](./typescript-sdk.md).

## Operator scripts

| Variable                                                                                                         | Read by                                     | Default                                           | Meaning                                                                                       |
| ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `LOOM_REPO`                                                                                                      | `redeploy.sh`                               | the script's directory                            | The deploy checkout.                                                                          |
| `LOOM_REDEPLOY_READY_TIMEOUT_S`                                                                                  | `redeploy.sh`                               | `180`                                             | How long to wait for the idle colour's `/v1/health` to report ready before aborting the swap. |
| `LOOM_PUBLIC_HEALTH_URL`                                                                                         | `redeploy.sh`                               | unset                                             | When set, the health URL clients use is probed last.                                          |
| `LOOM_PREFLIGHT_STRICT`                                                                                          | `redeploy.sh`                               | `1`                                               | `0` turns ledger-only migration rows and checksum drift into a report instead of a stop.      |
| `LOOM_PREFLIGHT_HEALTH_URL`                                                                                      | `dev/redeploy_preflight.sh`                 | `http://localhost:7777/v1/health`                 | The health URL the preflight reads.                                                           |
| `LOOM_PREFLIGHT_PORT`                                                                                            | `dev/redeploy_preflight.sh`                 | `7777`                                            | The port the preflight checks.                                                                |
| `LOOM_PREFLIGHT_MIGRATE`                                                                                         | `dev/redeploy_preflight.sh`                 | `$PYTHON -m server.core.schema.migrate --dry-run` | The migration dry-run command the preflight runs.                                             |
| `HDX_BASE`, `HDX_COOKIE` or `HDX_EMAIL` + `HDX_PASSWORD`, `HDX_DB`, `HDX_PREFIX`, `HDX_CONNECTION_ID`, `DRY_RUN` | `deploy/clickstack_sources.sh`              | `HDX_DB=default`, `HDX_PREFIX=loom`, `DRY_RUN=0`  | The HyperDX instance, credentials and naming for registering Loom's ClickStack sources.       |
| `LOOM_ADMIN_TOKEN`                                                                                               | `dev/onboard_tenant.py`                     | unset                                             | The admin token that onboards a tenant.                                                       |
| `LOOM_OUTPUT_DIR`                                                                                                | `dev/loom_init.py`, `dev/onboard_tenant.py` | `.`                                               | Where the onboarding scripts write their output files.                                        |
| `LANGFUSE_HOST`, `LANGFUSE_ORG_API_KEY`                                                                          | `dev/loom_init.py`                          | unset                                             | Langfuse project provisioning during `dev/loom_init.py`.                                      |

The redeploy procedure and its preflight are in
[Deployment](../operations/deployment.md) and [Upgrades](../operations/upgrades.md);
the compiler-tier workers in
[Code index operations](../operations/code-index-operations.md); ClickStack in
[ClickStack](../features/integrations/clickstack.md).

## Related

- [Configuration reference](./configuration.md): every `LOOM_*` settings field.
- [Deployment](../operations/deployment.md): required environment, providers and the production checklist.
