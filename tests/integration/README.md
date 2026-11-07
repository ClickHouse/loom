# Integration tests

These tests drive a running Loom server and the ClickHouse it writes to. They
check what unit tests cannot: real serialization, auth, ClickHouse semantics
(ReplacingMergeTree under `FINAL`, materialized views, mutations), transports,
migrations, worker passes and concurrency.

## Running locally

```bash
# ClickHouse with the dev config (the lemmatizer dictionary is required)
bash dev/ci_start_clickhouse.sh          # or any disposable ClickHouse
export LOOM_CH_HOST=localhost LOOM_CH_PORT=8123 LOOM_CH_USER=loom \
  LOOM_CH_PASSWORD=loomdev LOOM_CH_DATABASE=loom
python -m server.core.schema.migrate
LOOM_AUTH_REQUIRED=false LOOM_BACKGROUND_WORKERS=off \
LOOM_EMBEDDING_MODEL=fake-ngram-1024 LOOM_LLM_PROVIDER=fake \
LOOM_INSTANCE_LABEL=local:integration \
  uvicorn server.main:app --port 7777 --loop asyncio &
LOOM_BASE_URL=http://localhost:7777 pytest tests/integration -q
```

The CI job (`.github/workflows/ci.yml`, job `integration`) sets these
variables. Use it as the reference environment.

## Dependencies

| Dependency         | Provided by                                                                  | When missing or broken                                                                                   |
| ------------------ | ---------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Loom server        | `LOOM_BASE_URL`                                                              | skipped when unset; failed when set but `/v1/health` is not 200                                          |
| ClickHouse         | `LOOM_CH_HOST/PORT/USER/PASSWORD/DATABASE/SECURE`; the host must be explicit | every test that builds a client, calls handlers in-process or runs a ClickHouse-reading `dev/` CLI skips |
| JWT signing        | `LOOM_JWT_SECRET` in the test process, the same value as the server's        | minted tokens are rejected (401)                                                                         |
| Real embedder      | server `LOOM_EMBEDDING_MODEL` (read from `/v1/health.fake_embedding`)        | search-quality tests skip (`require_real_embedder`)                                                      |
| Real LLM           | server `LOOM_LLM_PROVIDER` (read from `/v1/health.fake_llm`)                 | extraction-dependent tests skip (`require_real_llm`)                                                     |
| Self-signup        | server `LOOM_SELF_SIGNUP=open`                                               | signup-based tests skip                                                                                  |
| Enforced auth      | server `LOOM_AUTH_REQUIRED=true`                                             | auth-enforcement probes skip                                                                             |
| Subprocess servers | `uvicorn` in this interpreter; kernel-assigned ports                         | CORS, OAuth, OIDC and RS256 tests start their own (`_spawn.loom_server`)                                 |
| Filesystem         | `tmp_path` and temporary log files                                           | —                                                                                                        |
| Network            | loopback only; no provider or third-party calls                              | —                                                                                                        |

There is no default ClickHouse host. On a developer machine,
`localhost:8123` can be a shared store, so the suite reaches only the host
you name. `_resources.ch_client_from_env` is the one client constructor. A
session guard makes any other driver construction skip, and
`tests/unit/test_integration_suite_targets.py` rejects a direct client, a
defaulted `LOOM_CH_HOST`/`LOOM_BASE_URL`, an `Agent` without `base_url`,
and a ClickHouse-reading `dev/` CLI run without `ch_target_required`.

`LOOM_BACKGROUND_WORKERS=off` is required. Tests that exercise a worker call
its `run_pass` in-process on their own namespace, and a live supervisor would
drain their rows.

## Isolation and cleanup

- **Identities.** Mint orgs with `tests.integration._resources.unique_org(label)`
  (`it-<label>-<8 hex>`) and use random namespace suffixes. A fixed org
  collides across runs on a shared store. Org-scoped blacklist patterns,
  audit counts and budget reports then read another run's rows.
- **Cleanup.** When the session ends, every row of every minted org is
  deleted from each MergeTree table that has an `org` column, and the rows
  are counted again. Any residue fails the run. Only ids of the minted shape
  are ever purged. Set `LOOM_IT_KEEP_ROWS=1` to keep them for debugging.
- **Waiting.** Never sleep for a fixed time and then assert. Poll with
  `_eventually.eventually` (async) or `_eventually.wait_until` (sync). Both
  use a bounded monotonic deadline and report the last value they saw.
- **Absence checks.** A "nothing was written" assertion first waits for a
  later positive event from the same emitter. Without that barrier, a slow
  flush reads as absence.

## Tiers

Markers are registered in `conftest.py`. `provider` and `destructive` are
also applied at collection to every test that uses `require_real_embedder`,
`require_real_llm` or `pause_loom_memory_merges`.

- unmarked: runs on every pull request
- `slow`: spends seconds of wall clock on purpose (lease lapses, retry exhaustion, refresh cycles)
- `destructive`: stops merges, runs `OPTIMIZE`, or deletes whole scopes
- `provider`: needs a real embedder or LLM; skips on the fakes
- `recovery`: drives a failure and the path back from it

CI runs the whole directory in parts (`LOOM_TEST_SHARD`,
[testing](../../docs/development/testing.md#integration-tests)). For a quick local pass,
use `-m "not slow and not destructive and not provider and not recovery"`.

## Diagnostics

When a test fails, its report has a `loom requests` section: the last 25
requests it made, with method, URL, status, `X-Request-Id` and latency. Use
the request id to find the server log line. Credentials in URLs and logs are
masked by `_redact.redact`.

## Security gates

- `test_cross_principal_leakage.py` drives every read-only operation in the
  registry as three foreign principals against canary nonces.
  `test_every_read_only_op_is_probed_or_exempt` keeps that set equal to the
  registry.
- `test_restricted_namespace_leakage.py` covers restricted namespaces and
  their audit rows.
- `test_read_ops_never_5xx.py` covers a purged namespace.
