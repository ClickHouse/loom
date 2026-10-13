# Failure and recovery

The primary write and every access decision fail closed. Derived data and telemetry fail
open and are repaired. A request that returns success has durably stored what it
promised; anything projected beside it is caught up by a worker. The last section,
[Capacity](#capacity), covers what scales how and where the limits are. Symptom runbooks
are in [troubleshooting](../operations/troubleshooting.md).

## Errors, retries and idempotency

| Boundary             | Behaviour                                                                                                                                                                                                                     |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse client    | A transient network failure of an idempotent statement is retried once on a fresh connection. A replica-bound server error (codes 10, 242, 999) is retried on up to two new connections. Anything else raises.                |
| Statement deadlines  | Past its class deadline (`LOOM_CH_TIMEOUT_READ_S`, `_WRITE_S`, `_DDL_S`) a request fails with 504 `BACKEND_TIMEOUT`, not retried.                                                                                             |
| Pool admission       | No free slot within `LOOM_CH_POOL_WAIT_MS`: 503 `OVERLOADED` with `Retry-After: 1`.                                                                                                                                           |
| Inserts              | A retried INSERT carries the same `insert_deduplication_token`, so a committed batch is not written twice. Without the token, an insert is not retried.                                                                       |
| Unsupported settings | A replica that rejects the quantized-codes read setting makes the client drop it for the process and retry.                                                                                                                   |
| Point reads          | Reads after the caller's own write use sequential consistency; a miss right after a write re-reads once (`LOOM_CH_READ_YOUR_WRITES`); a search on a namespace written within 20 s reads sequentially.                         |
| Schema skew          | A statement naming a table a pending migration would create is refused with 503 `SCHEMA_PENDING` (below).                                                                                                                     |
| Op idempotency       | Ops declared `idempotent` accept `Idempotency-Key` (HTTP) or `_idempotency_key` (MCP). The answer is kept in `loom.idempotency` for 24 hours, keyed on org, subject, op and key. Two replicas racing on one key can both run. |
| Primary write        | The fact row and its vector row are awaited committed inserts. A failure fails the request.                                                                                                                                   |
| Projections          | Graph, chunks, evidence and supersession side tables fail open and are repaired by workers.                                                                                                                                   |
| Telemetry            | `agent_events` and the events stream are batched and never fail a request. A crash can lose the unflushed queue.                                                                                                              |
| Workers              | A failing pass is retried with backoff. Work is written as absolute values or new versions, so a repeated pass converges.                                                                                                     |
| Erasure              | `erasure.request` is a checkpointed saga. `erasure_recovery` resumes it, and a residue census over every table decides success.                                                                                               |

**Retryable answers.** `OVERLOADED`, `AUTH_UNAVAILABLE` and `RATE_LIMITED` carry
`Retry-After` (and `retry_after_s`); retry after the hint. `SCHEMA_PENDING` clears when an
operator applies the named migration. `EMBED_TIMEOUT`: a raw HTTP or MCP
caller retries once; an SDK caller has already retried (idempotent ops), so it backs off.
`BACKEND_TIMEOUT` is retryable only for an `idempotent` op, with an idempotency key for a
write. The full list is in the [errors reference](../reference/errors.md).

**Why the idempotency cache is best effort:** a cluster-consistent cache needs a lock per
request. The ops that use it write the same content on a duplicate run into a
`ReplacingMergeTree`, so a race costs a second handler run, not a second effect. Ops whose
repeat would double an effect (`memory.set_from_messages`) are non-idempotent and bypass
it.

## Consistency boundaries

Cross-table writes are not transactional. The fact and its vector land together; derived
data catches up:

| Derived data                  | Catches up                                                                                                                                                                                  |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| BM25 statistics               | Refreshable views every minute; search reads a live tail, so ranking stays exact (`LOOM_MEMORY_SEARCH_BM25_LIVE_TAIL`).                                                                     |
| Usage counters, reinforcement | One `usage_fold` pass (every 300 s).                                                                                                                                                        |
| Entity-graph projection       | Shortly after the response. A `facts.traverse` of the same namespace waits for it, across every process that shares the write marks (`LOOM_GRAPH_WRITE_MARKS_DIR`), else within the writer. |

**Why not transactions:** ClickHouse has none across tables, and emulating them adds a
round trip and a failure mode to every write. A durable primary pair plus idempotent repair
keeps the write to one round trip.

## Backpressure

No request waits unboundedly on a shared resource:

- **ClickHouse pool:** a bounded wait, then `OVERLOADED` ([ClickHouse client](#clickhouse-client)).
- **Rate limits:** 429 `RATE_LIMITED` with `Retry-After`. Over MCP the transport waits once
  for a short hint (`LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S`) and re-checks.
- **Query embedding:** `LOOM_MEMORY_SEARCH_EMBED_BOUND_S` (2.5 s) per attempt, retried
  `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES` times (1), then 503 `EMBED_TIMEOUT`.
- **Telemetry queues:** the `agent_events` emitter (10,000 rows) and the events stream
  (20,000) drop and count rows when full (`dropped_queue_full`, `dropped_flush_failed`),
  logging the first drop and every 1000th. Delivery is at most once.
- **Workers:** `usage_fold` skips a pass while ClickHouse has too many pending mutations;
  every pass is bounded by batch size and deadline.
- **Budgets:** an op over its `budget_p99_ms` still answers and leaves a `budget.violated`
  row (at most one per op per `LOOM_BUDGET_VIOLATION_AUDIT_INTERVAL_S`, 60 s).

## `SCHEMA_PENDING`

A server built from a newer commit than its schema (a branch server with
`LOOM_BOOT_MIGRATE=false`, or a colour that booted before its migrations) can name a table
that does not exist yet. `server/core/ch_schema_guard.py` (`guard_pending_schema`) refuses such a statement
with 503 `SCHEMA_PENDING`, naming the table and migration. A retrieval leg that treats a
failed read as optional continues without it, and the ledger row records the table. DDL is
never refused.

The pending set comes from the migration ledger (`server/core/schema/status.py`),
cached 60 s per process. When the ledger reports
nothing applied or more than 25 pending files, the guard goes idle with one warning.
`/v1/health` reports `schema.pending`, and a blue/green switch waits for it to be empty.

## Worker supervision

`server/workers/supervisor.py` runs each enabled worker as its own asyncio task:

- **Stagger.** The first pass waits `crc32(name) % interval`, except that a worker whose
  output table is empty runs at once, and a worker whose last pass is older than its
  interval starts within 300 s of boot.
- **Deadline.** A pass past `min(4 × interval, 900 s)`, floored at 60 s (unless the worker
  declares its own), is cancelled and counted as a failure.
- **Backoff.** A failed pass waits `min(60 s, 2^(n-1) s)`. A pass cut at its deadline, or a
  failed pass of a worker whose interval is longer than its deadline, waits the interval.
- **Degraded.** Three consecutive failures log `DEGRADED`; the streak is
  `loom.worker.consecutive_failures{worker}`, and a pass with a failed leg counts in
  `loom.worker.partial_failures`.
- **Status.** Each pass writes a `loom.worker_status` row (`GET /v1/admin/workers.status`);
  a resume cursor lets a new colour continue a long scan.
- **Shutdown.** `stop_all` cancels the tasks, waits up to 10 s, then flushes status rows.

A worker's exceptions stay in its task. Workers write absolute values or new versions, so
a pass that runs twice converges.

## Repair workers

| Worker                    | Repairs                                                                                                                | Guarantee                                                                          |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `projection_repair`       | Replays failed projections (evidence links, chunks, graph side tables) logged in `loom.memory_projection_failures`.    | Unsupported entries stay for inspection.                                           |
| `entity_extractor`        | Extracts entities onto rows that have none; re-emits `memory_embeddings` rows whose `entities` diverge from `memory`.  | Every searchable row eventually carries its entities.                              |
| `orphan_vector_reconcile` | Tombstones embedding and entity-vector rows whose memory is tombstoned or gone, after a grace period.                  | No vector outlives its fact.                                                       |
| `embedding_worker`        | Embeds `embedding_dim = 0` stubs newest first and memories with no embedding row; re-embeds stubs from an older model. | A row that fails 5 times is marked `embed_failed_at` and skipped.                  |
| `erasure_recovery`        | Resumes sagas stuck `in_progress` or `failed` from `steps_completed`.                                                  | After `max_attempts` (5): terminal `failed` plus an `erasure.abandoned` audit row. |
| `task_lease_sweep`        | Returns expired leases to `todo`, or `blocked` after their attempts; resumes deferred tasks.                           | A crashed agent's task is not lost.                                                |
| `audit_verifier`          | Recomputes audit hashes and verifies the checkpoint chain.                                                             | Tampering is reported, not repaired.                                               |

## Fail-closed choices

These refuse instead of degrading, because degrading would widen access or lie about a
deletion:

- **Revocation.** Until a process has loaded the revoked-token set once, every token gets
  503 `AUTH_UNAVAILABLE`. After that, a ClickHouse outage keeps the last snapshot.
- **Authorization.** A failed grant read refuses; a restricted namespace whose
  classification cannot be read is denied.
- **Boot migrations.** A failed migration stops the process before any worker starts.
- **Erasure.** A federated backend that cannot confirm deletion fails its step; residue
  anywhere in scope fails the saga.
- **Dev signing secret.** A deployment that requires auth refuses to start with the
  checked-in secret.

These fail open, counted or logged, because refusing would take the service down for an
availability control: traffic classification, the rate limiter, the idempotency cache read,
the audit insert, boot calibration, the embedding caches and every telemetry write. See
[security](security.md).

## When a dependency is down

| Down                  | What happens                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse            | `/v1/health` answers 503 (the only case). Requests fail with `OVERLOADED`, `BACKEND_TIMEOUT` or a 500 carrying the request id. Tokens verify on the last revocation snapshot. Workers fail their passes and back off.                                                                                                                                                                                                                                 |
| Embedding provider    | `memory.set` and `memory.bulk_set` fail: no fact is stored without its vector. Paths that can wait (`memory.promote`, conversation turns, chunk embeds) store a stub for `embedding_worker`. `memory.search` fails with 503 `EMBED_TIMEOUT` unless the query's vector is in the embedding cache. A hung provider sets no health flag (the log shows `query embed exceeded`); one that answers errors shows `provider_failing:<name>` on `/v1/health`. |
| Embedding key missing | The process serves the fake embedder, with an error log and a `problems` entry; searches match only rows written under the fake model.                                                                                                                                                                                                                                                                                                                |
| LLM provider          | Each call retries (four attempts). Fail-open stages (HyDE, rerank, the extraction verifier, titles) are skipped; extraction and `memory.ask` synthesis have no substitute. `LOOM_LLM_FALLBACK_MODEL` hands an outage to a second model.                                                                                                                                                                                                               |
| Langfuse, ClickStack  | Tracing and export drop; requests are unaffected. A configured erasure backend that is down fails its saga step, and recovery retries it.                                                                                                                                                                                                                                                                                                             |

**Why `memory.set` fails instead of storing a stub:** a stub is invisible to every search
leg until embedded, so a caller told "stored" would not find its own write. Failing lets it
retry with its idempotency key.

## Blue/green rollback

`./redeploy.sh` starts the idle colour and switches only after its `/v1/health` reports
`status: ok`, ClickHouse reachable, `schema.pending` empty, no degraded provider and no
embedding width mismatch, within `LOOM_REDEPLOY_READY_TIMEOUT_S` (a script variable, 180 s).
The old colour is stopped, not removed, and its image tagged `loom:prev`.
`./redeploy.sh --rollback` starts it and switches back. Every migration is additive
(`tests/unit/test_migrations_additive.py`), so the previous build runs against the newer
schema. See [upgrades](../operations/upgrades.md).

## Data-loss boundaries

- **Acknowledged writes.** A successful `memory.set` has committed both its rows.
- **Telemetry and audit rows.** A crash loses the unflushed `agent_events` queue (flushed
  every 100 ms) and events-stream queue (every `LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S`, 1 s).
  The events stream carries budget violations only. Audit, exposure and search-trace rows
  are inserted directly; a failed audit insert is logged and never fails the write it
  records. Retrieval reads none of them.
- **After-response projections** lost in a crash are rebuilt by the repair workers.
- **ClickHouse OSS single node.** No replication and no point-in-time recovery. ClickHouse
  Cloud, or a replicated OSS cluster, is the production path. Back up with ClickHouse
  `BACKUP` or native backups; `dev/backup_loom.py` moves a namespace, it is not disaster
  recovery. See [backup and recovery](../operations/backup-and-recovery.md).
- **Files outside ClickHouse.** Document originals (`LOOM_BLOB_DIR`) and code mirrors live
  on volumes. Mirrors can be re-cloned; originals need the volume's backup.

## Capacity

A server process keeps no durable state, so web capacity scales by running more processes.
ClickHouse does the retrieval work (vector scans, text indexes, graph walks), so capacity
past the Python tier is ClickHouse capacity. The workers do not scale by replication: one
supervisor runs them. Procedures are in [deployment](../operations/deployment.md) and
[performance tuning](../guides/performance-tuning.md).

| Tier                   | Scales by                                                  | Bounded by                                                                |
| ---------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| Web (HTTP, MCP, `/me`) | More containers behind a load balancer; `LOOM_WEB_WORKERS` | Python CPU per process; the per-process pool; per-replica rate limits     |
| Background workers     | One supervisor per deployment; a larger workers container  | Pass duration against interval; provider quota for LLM and embedding work |
| Retrieval and storage  | ClickHouse compute, memory and storage                     | Rows scanned per tenant read; merge pressure under ingest                 |
| Providers              | Provider quota and batch sizes                             | Provider rate limits; `loom.embed.pending` shows the embedding backlog    |

### Web tier

| Knob                             | Effect                                                                                                             |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| More web replicas                | N web containers behind a load balancer, all with `LOOM_BACKGROUND_WORKERS=off`.                                   |
| `LOOM_WEB_WORKERS`               | uvicorn processes per container (default 1). One process is at most one core; `min(4, cores)` is a starting point. |
| `LOOM_REPLICA_COUNT`             | Divides each rate-limit bucket by N, because buckets are per replica (below).                                      |
| `LOOM_LATENCY_BUDGET_MULTIPLIER` | Scales every op's `budget_p99_ms` before a budget violation fires (default 1.0).                                   |

A new replica needs no warm-up beyond loading its revocation snapshot. Read-your-writes
across processes is handled by ClickHouse (committed inserts, sequential re-reads), not
process affinity.

### ClickHouse client

`server/core/ch.py` gives each process one pool and one admission semaphore of the same size.

| Setting                   | Default | Effect                                                                                |
| ------------------------- | ------- | ------------------------------------------------------------------------------------- |
| `LOOM_CH_POOL_MAXSIZE`    | 64      | Connections and concurrent statements per process.                                    |
| `LOOM_CH_POOL_WAIT_MS`    | 500     | Wait for a slot; past it 503 `OVERLOADED` with `Retry-After: 1`. 0 disables the gate. |
| `LOOM_CH_TIMEOUT_READ_S`  | 30      | Read deadline.                                                                        |
| `LOOM_CH_TIMEOUT_WRITE_S` | 60      | `INSERT` / `UPDATE` deadline.                                                         |
| `LOOM_CH_TIMEOUT_DDL_S`   | 300     | DDL, `OPTIMIZE`, `SYSTEM` deadline.                                                   |

**Why a bounded wait:** when ClickHouse slows, an unbounded queue turns every request into
a client timeout after the work was queued anyway. A short wait fails fast with a retryable 503. The gauges `loom.ch.pool.in_use`, `waiters`, `overloaded_total` and `timeouts_total`
show which side is saturated.

### Traffic classes

Every call has a class: `production`, `interactive` (default), `bench` or `background`
(set by the supervisor, never by a request). It comes from the token claim, else the
namespace registry, else the request hint. It changes scheduling, never results:

- ClickHouse read settings per class (`LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS`): `production`
  priority 1, `interactive` 2, `background` 5 with 3 threads, `bench` 10 with 4 threads.
- Rate-limit buckets (`LOOM_RATELIMIT_CLASS_FRACTIONS`; `bench` gets half).
- LLM policy per class (`LOOM_LLM_POLICY_BY_TRAFFIC_CLASS`).

A fresh `bench-…` namespace is classed `bench`, so a load test does not starve production.

### Rate limits

`server/core/ratelimit.py` keeps token buckets per (namespace, op class) and per (org, op
class); the org tier is three times the namespace tier. An optional per-actor tier takes a
fraction of the namespace bucket (`LOOM_RATELIMIT_ACTOR_FRACTION`, 1.0 = off). A call must
pass every bucket, and a refusal decrements none.

| Op class           | Covers                                                                        | Capacity | Refill / s |
| ------------------ | ----------------------------------------------------------------------------- | -------- | ---------- |
| `memory`           | `memory.*` and `conversation.*` writes and searches (and `code.outcome`)      | 100      | 17         |
| `memory.read`      | Exact-key reads: `memory.get`, `bulk_get`, `history`, `working_memory.get`, … | 300      | 50         |
| `knowledge.search` | `knowledge.search`                                                            | 20       | 3          |
| `knowledge.add`    | Other `knowledge.*` ops                                                       | 10       | 2          |
| `code`             | `code.*`                                                                      | 60       | 10         |
| `default`          | Everything else                                                               | 50       | 10         |

These are the namespace-tier defaults (`DEFAULT_LIMITS`, `op_class_for`):

- **Per replica.** Buckets live in the process or host, so N replicas admit N times the
  limit unless `LOOM_REPLICA_COUNT=N`. The sum is approximate.
- **Within a host.** With `LOOM_WEB_WORKERS > 1`, processes share one bucket table in a
  memory-mapped file (`LOOM_RATELIMIT_SHARED=auto`, `LOOM_RATELIMIT_SHARED_PATH`,
  `LOOM_RATELIMIT_SHARED_SLOTS`). `/v1/health` reports `ratelimit.backend`.
- **Fail open.** A limiter error admits the call and is counted.

**Why local buckets:** a ClickHouse round trip per admission would put the database on
every request's path to protect the database.

### Background workers

The lease is a host-local file lock (`LOOM_SUPERVISOR_LOCK_PATH`), so:

- Run exactly one workers container per deployment. With several hosts, set
  `LOOM_BACKGROUND_WORKERS=off` on all but one; two hosts in `auto` each elect a leader.
- A second copy of the maintenance workers wastes ClickHouse work; a second copy of the LLM
  workers doubles their scans and spend.
- Keep workers out of the web containers (`dev/docker-compose.cloud.yml`).

Worker work is not partitioned across processes. A worker keeps up through bounded passes:
a batch or time budget per pass (for example `embedding_worker` embeds up to 100 stubs;
`usage_fold` folds up to 1000 memories within 120 s and backs off at 20 pending mutations)
and the deadline above. A pass that routinely hits its deadline means the workers
container needs more CPU or the provider more quota.

### Batch sizes and queues

| Path                              | Bound                                                                                                      |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Request body                      | `LOOM_MAX_REQUEST_BYTES` (32 MiB); 413 above it                                                            |
| `memory.bulk_set`                 | Up to 1000 entries in one `embed_many`; conflict checks up to `LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES` (200) |
| `memory.bulk_get`                 | Up to 1000 keys                                                                                            |
| `memory.set_from_messages`        | Up to 400 messages                                                                                         |
| Embedding requests                | Up to 100 inputs per provider request (OpenAI, Gemini)                                                     |
| `agent_events` emitter            | Queue of 10,000 rows, flushed every 0.1 s or 500 rows                                                      |
| Events stream (budget violations) | Queue of 20,000, flushed every `LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S` or `_FLUSH_BATCH` rows                |
| Worker status rows                | Flushed every 15 s or 200 rows                                                                             |

### Vector cost

A memory stores up to two vectors (value and key) of `Array(BFloat16)`: 6 KiB at 1536
dimensions, 12 KiB at 3072. Storage and scan bytes grow linearly with `LOOM_EMBEDDING_DIM`.
There is no HNSW index, so a read scans every vector of the tenant's namespace and latency
grows with namespace size. Quantized codes shrink the scan where the server supports them.
Details are in [ClickHouse](clickhouse.md#vector-storage).

### Measuring

This page states no latency or throughput numbers; they depend on the ClickHouse
deployment, network distance and namespace size. Measure your own, against an isolated
server:

| Command                                                     | Measures                                                                                               | Output              |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------- |
| `make bench OP=memory.set C=10 D=30`                        | p50 / p95 / p99 and RPS of one op at concurrency C for D seconds (`scripts/ops/benchmark.py`)          | stdout, or `--json` |
| `python bench/search_latency.py --namespaces … --queries …` | `memory.search` p50 / p95 per namespace size band, with per-stage times from `retrieval_work.stage_ms` | `--out` CSV         |
| `make bench-quick`                                          | Retrieval quality on a 30-question sample (needs real providers)                                       | CSV under `/tmp`    |

`scripts/ops/benchmark.py` writes into a fresh `bench-…` namespace, so it runs in the
`bench` class; a `memory.search` run needs a namespace a `memory.set` run filled. See
[running benchmarks](../benchmarks/running.md).

### What to scale

| Signal                                                          | Scale                                                     |
| --------------------------------------------------------------- | --------------------------------------------------------- |
| Web CPU high, request p99 rising, ClickHouse answering fast     | Web replicas or `LOOM_WEB_WORKERS`                        |
| `loom.ch.pool.waiters` and `OVERLOADED` rising, ClickHouse fast | `LOOM_CH_POOL_MAXSIZE`, or more replicas                  |
| `BACKEND_TIMEOUT` and slow `system.query_log` entries           | ClickHouse compute, or smaller namespaces per read        |
| `loom.embed.pending` rising                                     | Embedder throughput (provider quota)                      |
| `loom.worker.consecutive_failures` or pass durations growing    | The workers container's CPU and memory                    |
| Merge pressure, memory errors under ingest                      | ClickHouse compute and memory, or lower write concurrency |

The metrics are described in [monitoring](../operations/monitoring.md).

### Known limits

- **One worker leader per host.** Multi-host deployments pin the workers to one host.
- **Per-replica rate limits** are approximate; the sign-in and sign-up limits are per
  process with no division.
- **Namespace size drives read latency**, because vector reads are exact scans.
- **Single-node ClickHouse OSS** has no replication and no failover.

## Invariants

- A successful write has committed its primary rows; a failure of either fails the request.
- A retried INSERT carries its deduplication token, or is not retried.
- `OVERLOADED` and `AUTH_UNAVAILABLE` carry a retry hint; `SCHEMA_PENDING` waits on an
  operator.
- A worker pass is idempotent: running it twice converges.
- Access checks and erasure fail closed; availability controls and telemetry fail open,
  counted.
- No request waits unboundedly on the pool, a provider or a queue. Every statement waits at
  most `LOOM_CH_POOL_WAIT_MS` for a slot and runs at most its class deadline.
- A traffic class changes scheduling and buckets, never the rows returned.
- Every migration is additive, so the previous build runs against the current schema.

## Source entry points

| What                     | Where                                                                                                                                                                                                                                       |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Client retries and gates | `server/core/ch.py` (`_with_retry`, `_acquire_slot`, `_bounded`, `statement_timeout`), `server/core/ch_schema_guard.py`                                                                                                                     |
| Error codes              | `server/core/errors.py`                                                                                                                                                                                                                     |
| Idempotency              | `server/core/idempotency.py`                                                                                                                                                                                                                |
| Supervisor and lease     | `server/workers/supervisor.py`, `server/workers/leader.py`, `server/workers/registry.py` (`default_max_pass_seconds`)                                                                                                                       |
| Repair workers           | `server/memory/workers/projection_repair.py`, `orphan_vector_reconcile.py`, `embedding.py`, `entity_extractor.py`; `server/erasure/workers/recovery.py`; `server/tasks/workers/lease_sweep.py`; `server/insights/workers/audit_verifier.py` |
| Revocation snapshot      | `server/auth/revocation.py` (`load_at_boot`)                                                                                                                                                                                                |
| Query-embed bound        | `server/memory/app/search/embed.py`                                                                                                                                                                                                         |
| After-write projections  | `server/memory/app/after_write.py`, `server/memory/app/projection_failures.py`                                                                                                                                                              |
| Telemetry queues         | `server/observability/events.py`, `server/observability/events_stream.py`, `server/workers/status_store.py`                                                                                                                                 |
| Traffic classes          | `server/observability/traffic_class.py`, `server/identity/app/traffic_class_registry.py`                                                                                                                                                    |
| Rate limiter             | `server/core/ratelimit.py` (`DEFAULT_LIMITS`, `op_class_for`), `server/core/ratelimit_shared.py`, `server/auth/attempts.py`                                                                                                                 |
| Schema status            | `server/core/schema/status.py`                                                                                                                                                                                                              |
| Blue/green               | `redeploy.sh`, `dev/docker-compose.colour.yml`                                                                                                                                                                                              |
| Load bench               | `scripts/ops/benchmark.py`, `bench/search_latency.py`                                                                                                                                                                                       |

## Related

- [Troubleshooting](../operations/troubleshooting.md): symptom-first runbooks.
- [Monitoring](../operations/monitoring.md): the metrics named here.
- [Security](security.md): the fail-closed access checks.
- [ClickHouse](clickhouse.md): tables, vector columns, query attribution.
