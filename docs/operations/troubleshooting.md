# Troubleshooting

Each entry gives a symptom, the check that confirms its cause, and the fix. To follow
one request through the ledger, query log and traces, see
[debug a request](../guides/debugging.md).

## Start here

Three commands answer most questions before you read any log:

```bash
curl -s http://localhost:7777/v1/health | jq '{status, ch_ok, ch_error, schema, problems}'
docker exec loom-server loom doctor              # the same problems, plus pre-boot checks
curl -s -XPOST http://localhost:7777/v1/ops.health -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"org":"<org>"}' | jq   # ClickHouse parts, workers, stubs (ORG_ADMIN)
```

Every entry in `problems` carries a `fix`; read it first. On a client machine,
`loom doctor --client` checks the server URL and token the SDK and hooks resolve. What
each field means is in [monitoring](./monitoring.md#health-checks).

## Server and ClickHouse

### Every operation fails and /v1/health returns 503

- **Cause:** ClickHouse is unreachable from the server. The boot also stops at the first
  migration.
- **Check:** `curl -s localhost:7777/v1/health | jq .ch_error`, then
  `docker exec loom-server loom doctor` (the `clickhouse_reachable` line).
- **Fix:** correct `LOOM_CH_HOST` / `LOOM_CH_PORT` / `LOOM_CH_USER` / `LOOM_CH_PASSWORD`.
  Port 8443 implies TLS; set `LOOM_CH_SECURE=true` for TLS on any other port. On
  ClickHouse Cloud, check that the service is running (idle services pause) and that its
  IP allow-list admits the Loom host.

### loom up times out, or the container exits at boot

- **Cause:** usually a migration or ClickHouse error, a malformed `.env` value, or a
  process outside the compose files without `LOOM_INSTANCE_LABEL`.
- **Check:** `docker logs loom-server`; the failing migration file or setting is named
  there. Validate the env file with
  `docker compose --env-file .env -f dev/docker-compose.cloud.yml config >/dev/null`
  (use the compose file you boot).
- **Fix:** the error in the log. A deployment (`LOOM_DEPLOYMENT=1`) with auth on and the
  checked-in dev `LOOM_JWT_SECRET` refuses to start by design: set a real secret
  ([deployment](./deployment.md#required-environment)).

### Writes fail with TOO_MANY_PARTS, or merges are failing

- **Symptom:** writes fail with ClickHouse error 252 `TOO_MANY_PARTS`, or `ops.health`
  reports `clickhouse_parts: fail`.
- **Check:** whether merges are failing, which is almost always the cause, not the write
  rate. Read the merge errors in `system.part_log` first:

  ```sql
  SELECT table, error, count() AS failures, any(exception)
  FROM system.part_log
  WHERE database = 'loom' AND event_type = 'MergeParts' AND error != 0
    AND event_time > now() - INTERVAL 1 HOUR
  GROUP BY table, error;
  ```

- **Fix:** if the exception names a skip index the server cannot merge, exclude that
  index from merge-time materialization. The setting is metadata-only and reversible;
  inserted parts still build the index:

  ```sql
  ALTER TABLE loom.<table> MODIFY SETTING
    exclude_materialize_skip_indexes_on_merge = '<existing list>,<index name>';
  ```

  Part counts fall within minutes once merges succeed. Do not drop the index first
  (rebuilding it later rewrites the table), and do not only raise
  `parts_to_throw_insert`: with merges stopped the count keeps growing. If no merge is
  failing, look for a writer inserting one row per statement (`system.query_log` grouped
  by `log_comment`, [query attribution](./monitoring.md#logs-traces-and-query-attribution)).

### Writes take seconds after a ClickHouse Cloud replica restart

- **Symptom:** write-heavy operations (`memory.set`, `run.*`, `conversation.append`)
  take seconds while reads stay fast, with no errors, often right after a deploy that is
  not the cause.
- **Check:** replica uptime, and system-log re-attachment in progress:

  ```sql
  SELECT hostName(), uptime() FROM clusterAllReplicas(default, system.one);
  SELECT count() FROM system.query_log
  WHERE event_time > now() - INTERVAL 15 MINUTE AND query ILIKE 'ATTACH TABLE system.%';
  ```

- **Fix:** none needed on the Loom side. After a restart the service re-attaches its
  system tables for a while, and inserts recover as that finishes (typically within an
  hour). Do not roll back a deploy because of it; contact ClickHouse Cloud support if it
  persists.

### Requests fail with BACKEND_TIMEOUT or OVERLOADED

- **Cause:** a ClickHouse statement ran past its class deadline (504
  `BACKEND_TIMEOUT`), or every pooled ClickHouse connection of the process stayed busy
  (503 `OVERLOADED`). Under a sustained write rate, batched inserts (audit rows,
  transcript turns, the agent_events flush) hold their connection for up to
  `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS` and then the flush, and background ones (the
  usage-event and write-mark flushes) for up to
  `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` and the flush
  ([batched inserts](../architecture/clickhouse.md#batched-inserts)).
  `OVERLOADED` can also mean a statement found ClickHouse short of the server's or the
  ClickHouse user's memory (code 241, shared by every query; the server log has
  `ClickHouse is short of memory` with ClickHouse's line, once per 5 s).
  From `conversation.delete` or `conversation.compact`, `OVERLOADED` can also mean writes
  to the thread's embeddings kept landing for 8 reconcile rounds; resend once the
  thread's writes settle.
- **Check:** the slowest sources in `system.query_log` by `log_comment`
  ([monitoring](./monitoring.md#logs-traces-and-query-attribution)); `ops.health` for part
  pressure; for memory, the statements of `system.query_log` with `exception_code = 241`
  and the largest `memory_usage` beside them, against the server's
  `max_server_memory_usage` and the user's `max_memory_usage_for_user`.
- **Fix:** the query or worker that dominates; for memory, that statement, or more
  memory for the ClickHouse service. A Loom far from its ClickHouse (a WAN)
  needs `LOOM_LATENCY_BUDGET_MULTIPLIER` near 20 so its latency budgets do not fire on
  every call. For `OVERLOADED` under a sustained write rate, size the pool for the
  connections batched inserts hold
  ([batched inserts](../architecture/clickhouse.md#batched-inserts)): raise
  `LOOM_CH_POOL_MAXSIZE` (a restart), switch batching off for the switched group with
  `loom settings set ch_batched_inserts false` (`LOOM_CH_BATCHED_INSERTS=false` at
  startup; the waited async inserts still wait), or lower the insert waits live with
  `loom settings set ch_batched_insert_wait_max_ms <ms>` (and
  `ch_batched_insert_background_wait_max_ms`).
  [Runtime settings](./runtime-settings.md) covers live overrides.
  The [errors reference](../reference/errors.md) lists every code.

## Providers and search quality

### Searches fail with 503 EMBED_TIMEOUT

- **Cause:** the embedding provider did not answer the query embedding within
  `LOOM_MEMORY_SEARCH_EMBED_BOUND_S` (2.5 s) on both of its attempts by default (one
  retry, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`). A query whose vector is in the
  embedding cache still answers.
- **Check:** the server log's `query embed exceeded …s (attempt n/m)` warnings, and
  `loom.agent_events` rows with `error_code = 'EMBED_TIMEOUT'`. A provider that hangs
  sets no health flag; `/v1/health` shows `provider_failing:<name>` only when it also
  answers errors. Also check the provider's status page.
- **Fix:** a raw HTTP or MCP caller retries once; an SDK caller has already retried, so it
  backs off. If it repeats, the provider is slow or down: wait it out, or raise the bound
  for a provider that is slow but answering. Each failed search holds the server for up to
  one bound per attempt.

### Search returns arbitrary results, or extraction writes nothing

- **Symptom:** `set_from_messages` writes no facts, `memory.ask` returns empty answers,
  or search ranks by character overlap.
- **Cause:** the process runs on a fake provider: configured as fake
  (`LOOM_LLM_PROVIDER=fake`, which every stock `.env` sets, or `fake-ngram-*`
  embeddings in a checkout's `.env` or an upgraded install), or configured as real
  without its key.
- **Check:** `curl -s localhost:7777/v1/health | jq '.problems[].code'` shows
  `llm_key_missing`, `embedder_key_missing`, `fake_llm` or `fake_embedder`
  (`fake_llm: true`, `fake_embedding: true` in the body).
- **Fix:** set the key named in the problem's `fix` (`OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) and the provider block in `.env`, then recreate
  the containers (`docker compose … up -d --force-recreate`). Setting a key alone does not
  change a provider the `.env` pins to `fake`. On the local stack the vector width is
  fixed when the tables are created, so switching from the fake embedder needs
  `make down-all && make dev`. Rows embedded by the fake embedder need `loom reembed`.
  Set `LOOM_REQUIRE_REAL_PROVIDERS=true` so this reads `status: degraded` next time.

### Search quality dropped after an embedder change

- **Cause:** a new embedder moves every similarity score. Rows still embedded by the old
  model sit in a different vector space.
- **Check:** `loom reembed --dry-run` counts off-model rows; `loom.search.top_similarity`
  shows the shift.
- **Fix:** `loom reembed` ([maintenance](./maintenance.md#re-embed-and-re-extract)), then
  re-baseline your alert on `loom.search.top_similarity`. With
  `LOOM_ADAPTIVE_THRESHOLDS` on (the default) the similarity gates follow the new
  embedder after the boot probe; `loom recalibrate` reruns it immediately.

### A write is missing from search, or was not stored

- **Check:** `loom drops --namespace NS` for what the ingest pipeline discarded or
  truncated, and the write's own response (`status`, conflict verdicts,
  `pipeline_errors`). `loom.embed.pending` counts rows still waiting for a vector.
- **Fix:** follow the drop reason ([maintenance](./maintenance.md#memory-ingest-drops));
  for a conflicted write, resolve it in the dashboard's Review queue
  ([conflicts and refusal](../features/memory/conflicts-and-refusal.md)).

## Auth and tokens

### Requests fail with 401 AUTH_INVALID or 503 AUTH_UNAVAILABLE

- **Symptom:** 401 `AUTH_INVALID`, or 503 `AUTH_UNAVAILABLE` with `Retry-After: 5`.
- **Check:** the `message` in the error detail (expired, bad signature, revoked, missing
  bearer). On a client machine, `loom doctor --client` tests the token the hooks and the
  SDK would send.
- **Fix:**
  - A bad signature means the token was minted with a different `LOOM_JWT_SECRET` (secret
    rotated, or minted against another deployment): re-mint with
    `docker exec loom-server loom mint-token …`.
  - An expired or revoked token needs a new one; `loom revoke-token --list` shows the
    revocations.
  - `AUTH_UNAVAILABLE` right after boot means the revocation list has not loaded yet
    (`revocation_not_loaded`); the server refuses tokens until it has, so retry after a
    few seconds and check ClickHouse if it persists.
  - A stale `LOOM_API_KEY` in the environment overrides `~/.loom/token` for the SDK and
    the plugins.
  - After turning auth on for the first time, every call needs a token: mint one as
    above.

### A call answers found: false for an id you know exists

- **Cause:** ops addressed by an org-wide id (`memory.get_by_id`, `update_by_id`,
  `redact`) answer `found=False` when the caller cannot read the row's namespace, rather
  than a 403 that would confirm the id exists.
- **Fix:** use a token with a grant on the row's namespace
  ([tokens and grants](../guides/tokens-and-grants.md)).

## Schema and upgrades

### Migrations pending or checksum drift

- **Symptom:** `/v1/health` reports `pending_migrations`, or
  `loom migrate --dry-run --strict` exits 1 (it does so on ledger-only rows or checksum
  drift, not on pending files alone), or `redeploy.sh`'s preflight stops.
- **Check:** `loom migrate --dry-run --strict` prints pending files, then `ledger-only`
  rows (ledger filenames the repository no longer has) and `checksum drift` (applied
  files whose text no longer matches the recorded SHA-256).
- **Fix:**
  - Pending files: `loom migrate`, or restart with `LOOM_BOOT_MIGRATE=true`.
  - Checksum drift or a ledger-only row means a migration file was changed (or renamed
    and changed) after some server applied it, usually a server run from an unmerged branch. Compare
    the applied DDL with the repository file. If the effect is identical, record the
    current file (the newest row wins):

    ```sql
    INSERT INTO loom._migrations (filename, checksum, applied_by)
    VALUES ('<file>', '<sha256 of the file>', 'operator');
    ```

    A ledger-only row whose DDL exists under the new name can be removed with
    `DELETE FROM loom._migrations WHERE filename = '<old name>'`.

  - If the effect differs, write a new migration that reconciles the schema; never edit
    an applied file ([migrations](../development/migrations.md)).
  - A boot that fails on a table "with rows in the old layout" needs
    `scripts/ops/table_rebuild.py` for that table first
    ([upgrades](./upgrades.md#how-migrations-run)).

### Requests fail with 503 SCHEMA_PENDING

- **Cause:** the statement names a table that a migration this build carries would
  create, and the service has not applied it (a process booted with
  `LOOM_BOOT_MIGRATE=false`, or a migration failed).
- **Fix:** as for pending migrations above.

## Workers

### A worker fails every pass

- **Symptom:** `loom.worker.consecutive_failures` stays at 3 or more; the workers log
  repeats `<name>: pass failed` or `pass exceeded its deadline`, then `DEGRADED`.
- **Check:** `GET /v1/admin/workers.status` for the worker's last error, and
  `docker logs loom-workers 2>&1 | grep -E '<name>: (pass|DEGRADED)'` for the traceback.
- **Fix:** follow the exception. Repeated deadline cancellations mean the pass is too
  large for its interval (a big backlog, a slow provider). A provider error usually means
  a missing key or quota. For an LLM worker on the fake provider, disable the worker or
  configure a real one. To stop one worker while you investigate, set its flag to false
  and recreate the workers container; the others keep running.

### No worker runs at all

- **Check:** `/v1/health?include=workers` → `workers_leader` on each process; one must
  hold the lease. `workers_lease_absent` at warn means no process holds a live one. A
  `workers_lease_unreachable` problem names why a process could not ask ClickHouse;
  `workers_lease_given_up` that its workers child kept failing to start.
- **Fix:** run every replica with `LOOM_BACKGROUND_WORKERS=auto` and `LOOM_DEPLOYMENT=1`; a
  process with `off` never runs them, and one outside the deployment runs none under
  `auto`. Set `on` only on a ClickHouse no deployment uses: it runs its own copy and takes
  no lease. During an upgrade an older build leaves the lease to a live newer one.

## Host

### The host or ClickHouse runs out of disk

- **Symptom:** the Loom host's disk fills (containers fail to write, image builds fail),
  or ClickHouse reports low free space.
- **Check:** on the host, `docker system df` and the size of `var/code_repos`; in
  ClickHouse, the per-table sizes and `system.disks` queries in
  [maintenance](./maintenance.md#disk-pressure-hygiene). When the disk is full, write
  diagnostic output to `/dev/shm`.
- **Fix:** cap Docker's JSON logs, remove build cache and dangling images
  (`docker builder prune`, `docker image prune`). Do not prune stopped containers if a
  stopped container is your rollback. In ClickHouse, let merges collapse versions after
  large erasures, compact the code vector tables with `loom code compact --apply`, or opt
  into per-table retention.

## Local stack

- **The dashboard returns 503.** The server runs from source (`make server`)
  without a built frontend. Run `make frontend-build`; `/v1/admin/*` answers either way.
- **The stack ignores your `.env`.** A `docker compose` command without
  `--env-file .env` boots the compose file's fallbacks (auth on, an OpenAI provider with
  no key). Use `make dev` or pass the flag.
- **A server started from source serves other code.** See
  [local development](../development/local-development.md#running-the-server-from-source).

## Clients, plugins and MCP

- **The coding-agent plugins** have their own symptom tables (a hook that finds no
  Node, no primer, captures not arriving, tools listed twice):
  [Claude Code](../features/integrations/claude-code.md) and
  [Codex](../features/integrations/codex.md). The plugins' `loom-status` skill runs the
  client check without a Loom checkout.
- **The ledger has no row for the request.** The request never arrived.
  `loom doctor --client` names the server URL and token the SDK and hooks resolve, and
  where each came from; a stale `LOOM_API_KEY` or `LOOM_BASE_URL` in the environment
  wins over `~/.loom/`. `GET /v1/whoami` with the token shows the subject, org,
  namespaces and roles the server verified. A 403 on a namespace the token does not name,
  or `IDENTITY_INCOMPLETE` from a scoped token with no namespace in the body, is
  authorization, not a bug ([tokens and grants](../guides/tokens-and-grants.md#namespace-access)).
  Stranded `loom-flush-*.json` files in the temp directory are plugin captures that never
  reached the server ([headless bots](../guides/headless-bots.md#5-when-the-bot-is-quiet)).
- **An MCP tool is missing from the listing.** Listings vary by the MCP URL's
  `?toolset=`, never by caller. Under `all`, an experimental op that neither the core nor
  the atomic roster lists is left out of the listing; it is still callable by name or
  through `loom_op`. The `code.*` tools appear on the default toolset only with
  `LOOM_MCP_CODE_TOOLS=true` ([MCP toolsets](../reference/mcp.md#toolsets)).
- **An MCP call returns `isError: true`.** A refusal other than 401 (`LOCKED`,
  `PINNED_CONFLICT`, `BLACKLISTED`, `RATE_LIMITED`, `UNKNOWN_ARGUMENT`, …) is a tool
  result with `isError` and the HTTP error detail in `structuredContent`; a 401 or a 5xx
  is a JSON-RPC error. Match on `code`.
- **A field you sent was ignored.** The response names it in `ignored_args`, with
  `did_you_mean` when a known field is close; a write operation refuses a close
  misspelling with 422 `UNKNOWN_ARGUMENT`
  ([unknown and removed fields](../reference/http-api.md#unknown-and-removed-fields)).
- **429 `RATE_LIMITED`.** See the buckets in
  [monitoring](./monitoring.md#rate-limits-and-traffic-classes); `Retry-After` says when.

## Filing a bug

`loom report` prints the bug form's Environment field with the `loom doctor` diagnostic
(presence booleans only, never secret values); `--open` opens the bug form with the
versions filled in ([filing an issue](../../CONTRIBUTING.md#filing-an-issue)).
