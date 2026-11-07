# Failure and recovery

The primary write and every access decision fail closed. Derived data and telemetry fail
open and are repaired. A request that returns success has durably stored what it
promised; anything projected beside it is caught up by a worker. The last section,
[Capacity](#capacity), covers what scales how and where the limits are. Symptom runbooks
are in [troubleshooting](../operations/troubleshooting.md).

## Errors, retries and idempotency

| Boundary             | Behaviour                                                                                                                                                                                                                                                                                                                                                                                           |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse client    | A transient network failure of an idempotent statement is retried once on a fresh connection. A replica-bound server error (codes 10, 242, 999) is retried on up to two new connections. Anything else raises.                                                                                                                                                                                      |
| Statement deadlines  | Past its class deadline (`LOOM_CH_TIMEOUT_READ_S`, `_WRITE_S`, `_DDL_S`) a request fails with 504 `BACKEND_TIMEOUT`, not retried.                                                                                                                                                                                                                                                                   |
| Pool admission       | No free slot within `LOOM_CH_POOL_WAIT_MS`: 503 `OVERLOADED` with `Retry-After: 1`.                                                                                                                                                                                                                                                                                                                 |
| Inserts              | A retried INSERT carries the same `insert_deduplication_token`, so a committed batch is not written twice. Without the token, an insert is not retried. A batched insert shares ClickHouse's flush with concurrent inserts into the same table, a flush that fails as a whole fails each of them, and each retry lands once under its own token ([batched inserts](clickhouse.md#batched-inserts)). |
| Unsupported settings | A replica that rejects the quantized-codes read setting makes the client drop it for the process and retry.                                                                                                                                                                                                                                                                                         |
| Point reads          | Reads after the caller's own write use sequential consistency; a miss right after a write re-reads once (`LOOM_CH_READ_YOUR_WRITES`); a search on a namespace written within 20 s reads sequentially.                                                                                                                                                                                               |
| Schema skew          | A statement naming a table a pending migration would create is refused with 503 `SCHEMA_PENDING` (below).                                                                                                                                                                                                                                                                                           |
| Op idempotency       | Ops declared `idempotent` accept `Idempotency-Key` (HTTP) or `_idempotency_key` (MCP). The answer is kept in `loom.idempotency` for 24 hours, keyed on org, subject, op and key. A duplicate while the first runs gets a 409.                                                                                                                                                                       |
| Primary write        | The fact row and its vector row are awaited committed inserts. A failure fails the request.                                                                                                                                                                                                                                                                                                         |
| Postgres authority   | Under `LOOM_STORAGE_AUTHORITY=postgres` the write is the Postgres commit; its ClickHouse rows are published from it and repaired by the `pg_delivery` worker. Postgres down fails writes and point reads closed (503 `POSTGRES_UNAVAILABLE`) ([storage authority](storage-authority.md#failure)).                                                                                                   |
| Projections          | Graph, chunks, evidence and supersession side tables fail open and are repaired by workers.                                                                                                                                                                                                                                                                                                         |
| Telemetry            | `agent_events` and the events stream are queued in process; an `agent_events` flush is a batched insert, an events-stream flush a committed one. Neither fails a request; a crash can lose the unflushed queue.                                                                                                                                                                                     |
| Workers              | A failing pass is retried with backoff. Work is written as absolute values or new versions, so a repeated pass converges.                                                                                                                                                                                                                                                                           |
| Erasure              | `erasure.request` is a checkpointed saga. `erasure_recovery` resumes it, and a residue census over every table decides success.                                                                                                                                                                                                                                                                     |

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

| Derived data                  | Catches up                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| BM25 statistics               | Refreshable views every minute; search reads a live tail, so ranking stays exact (`LOOM_MEMORY_SEARCH_BM25_LIVE_TAIL`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Usage counters, reinforcement | One `usage_fold` pass (every 300 s).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Entity-graph projection       | Shortly after the response. A `facts.traverse` of the same namespace that starts after the response waits for it (at most 3 s) on every process of the deployment: each projection in flight is recorded in `loom.graph_projections` (Keeper keys where the server has a Keeper), and its end records the namespace's graph write before clearing it, on the server's clock. A process that dies mid-projection leaves its record for at most 15 s: each process waits it out once (3 s), and until the record goes stale, walks of that namespace there are read fresh, never served from or kept in the memo. Past 32 projections of one namespace at once, the extra ones are not waited on; each counts as a graph write before its `memory.set` answers and again at its end. |

**Why not transactions:** ClickHouse has none across tables, and emulating them adds a
round trip and a failure mode to every write. A durable primary pair plus idempotent repair
keeps the write to one round trip.

## Backpressure

No request waits unboundedly on a shared resource:

- **ClickHouse pool:** a bounded wait, then `OVERLOADED` ([ClickHouse client](#clickhouse-client)).
- **Document bucket:** 8 transfers and 4 streams per process, each a bounded wait
  (`LOOM_CH_POOL_WAIT_MS`), then `OVERLOADED`; listings and deletes (erasure, the sweep)
  wait for one of 4 slots of their own.
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

| Down                  | What happens                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse            | `/v1/health` and `/v1/health/ready` answer 503; `/v1/health/live` stays 200. Requests fail with `OVERLOADED`, `BACKEND_TIMEOUT` or a 500 carrying the request id. Tokens verify on the last revocation snapshot. Workers fail their passes and back off.                                                                                                                                                                                                                                                                                                                                                                   |
| Embedding provider    | `memory.set` and `memory.bulk_set` fail: no fact is stored without its vector. Paths that can wait (`memory.promote`, chunk embeds) store a stub for `embedding_worker`. A conversation turn is stored with its embed error recorded and stays unembedded (nothing retries it); `conversation.compact` writes no summary and tombstones no turn (`embed-failed`). `memory.search` fails with 503 `EMBED_TIMEOUT` unless the query's vector is in the embedding cache. A hung provider sets no health flag (the log shows `query embed exceeded`); one that answers errors shows `provider_failing:<name>` on `/v1/health`. |
| Embedding key missing | The process serves the fake embedder, with an error log and a `problems` entry; searches match only rows written under the fake model.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| LLM provider          | Each call retries (four attempts). Fail-open stages (HyDE, rerank, the extraction verifier, titles) are skipped; extraction and `memory.ask` synthesis have no substitute. `LOOM_LLM_FALLBACK_MODEL` hands an outage to a second model.                                                                                                                                                                                                                                                                                                                                                                                    |
| Document bucket       | Uploads and downloads of originals kept in the bucket answer 503 `BACKEND_UNAVAILABLE`, and a download of one that is gone 410. An erasure step that reaches the bucket fails closed and `erasure_recovery` retries it. The originals sweep records the pass's failure and its bucket walk resumes from its cursor on the next pass.                                                                                                                                                                                                                                                                                       |
| Langfuse, ClickStack  | Tracing and export drop; requests are unaffected. A configured erasure backend that is down fails its saga step, and recovery retries it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

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

## A replica that dies mid-migration or mid-reconfiguration

Every process is a provider participant: one row in `loom._provider_participants`, written
every 10 s from a thread of its own, and a User-Agent naming its id on every ClickHouse
query it sends. A participant counts as stopped when it retired, or when its last heartbeat
is older than 60 s (server clock) and `system.processes` shows no query of its own on any
node. Nothing else counts: an unreachable process is not a stopped one.

- **Killed mid-migration.** Migrations hold the migration lock only (`ttl_s`, renewed while
  they apply), never the provider lock, which a boot holds just to check that no rebuild is
  unfinished. A second container takes the migration lock over once it expired and applies
  what is left.
- **Killed holding the provider lock** (a rebuild step, a saved configuration). The key's
  TTL is 60 s, renewed every 20 s; a contender takes it over once it expired and the
  holder's participant counts as stopped. `loom provider-lock clear --holder <holder>`
  frees one whose holder has no participant row, which never counts as stopped.
- **Killed mid-reconfiguration.** The transition waits on a participant whose heartbeat
  expired, since a paused process may resume with admitted work, until a process on
  its host proves it gone (its container restarted, or its pid exited) and retires it.
  The workers leader
  deletes the rows of participants retired a day ago and lists, once a minute, those a
  transition would wait on as `provider_participants_stale` in its `/v1/health`. Retire
  one by hand with `loom participant retire --participant <id> --yes` once its process
  is confirmed gone; the command refuses a participant whose heartbeat is fresh or whose
  queries still run.
- **A boot that fails after enrolling** retires its row; a crash-looping container leaves
  none behind.

A process frozen past its heartbeat (a stopped container, a VM pause) holds the
transition until it resumes and drains, or an operator retires it.

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
  `BACKUP` or native backups; `dev/backup_loom.py` moves a deployment, an org or a
  namespace, it is not disaster recovery. See [backup and recovery](../operations/backup-and-recovery.md).
- **Files outside ClickHouse.** Document originals are in ClickHouse
  (`loom.document_blobs`), or, for those written with `LOOM_DOCUMENT_OBJECT_STORE=s3`,
  in an S3-compatible bucket, which is backed up with the bucket's own versioning or
  replication; a lifecycle rule bounds how long those copies keep erased originals. Code
  mirrors live on a volume and re-clone from the remote ClickHouse records on a host
  without them. Provider reconfiguration keeps nothing on the host
  ([provider reconfiguration](provider-reconfiguration.md)).
- **A move to another host or ClickHouse.** Carry ClickHouse, the env config,
  `LOOM_JWT_SECRET` and, while rows name objects in it, the document bucket; everything
  else comes back
  ([moving a deployment](../operations/backup-and-recovery.md#moving-a-deployment)).

## Capacity

A server process keeps no durable state, so web capacity scales by running more processes.
ClickHouse does the retrieval work (vector scans, text indexes, graph walks), so capacity
past the Python tier is ClickHouse capacity. The workers do not scale by replication: one
supervisor runs them. Procedures are in [deployment](../operations/deployment.md) and
[performance tuning](../guides/performance-tuning.md).

| Tier                       | Scales by                                                  | Bounded by                                                                |
| -------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| Web (HTTP, MCP, dashboard) | More containers behind a load balancer; `LOOM_WEB_WORKERS` | Python CPU per process; the per-process pool; per-replica rate limits     |
| Background workers         | One supervisor per deployment; a larger workers container  | Pass duration against interval; provider quota for LLM and embedding work |
| Retrieval and storage      | ClickHouse compute, memory and storage                     | Rows scanned per tenant read; merge pressure under ingest                 |
| Providers                  | Provider quota and batch sizes                             | Provider rate limits; `loom.embed.pending` shows the embedding backlog    |

### Web tier

| Knob                             | Effect                                                                                                                                                                                                                                       |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| More web replicas                | N identical replicas behind a load balancer, all with `LOOM_BACKGROUND_WORKERS=auto`.                                                                                                                                                        |
| `LOOM_WEB_WORKERS`               | uvicorn processes per container; unset, the image starts one per CPU the container may use, at most one per GiB of memory and at most 8. Each process runs its Python on one core; the local embedder gives each an equal share of the CPUs. |
| `LOOM_REPLICA_COUNT`             | Floor of the replica count that divides each rate-limit bucket (the live serving `loom.instances` rows, below).                                                                                                                              |
| `LOOM_LATENCY_BUDGET_MULTIPLIER` | Scales every op's `budget_p99_ms` before a budget violation fires (default 1.0).                                                                                                                                                             |

A new replica serves once `/v1/health/ready` answers 200: ClickHouse answers, no
migration is pending, the revocation snapshot and signing keyring are loaded and fresh,
the tracker registry and runtime overrides have loaded once (boot waits up to
`LOOM_BOOT_WARMUP_TIMEOUT_S` for both), the similarity calibration is adopted, its
configuration matches the deployment's, and the sandbox runs where it is required. On
SIGTERM it answers 503 there and keeps serving for `LOOM_SHUTDOWN_READY_DELAY_S` before
its port closes. Read-your-writes across processes is handled by ClickHouse (waited
inserts, committed or batched, sequential re-reads), not process affinity.

### ClickHouse client

`server/core/ch.py` gives each process one pool and one admission semaphore of the same size.

| Setting                                         | Default | Effect                                                                                                                 |
| ----------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CH_POOL_MAXSIZE`                          | 64      | Connections and concurrent statements per process; a batched insert holds one for its wait and flush.                  |
| `LOOM_CH_POOL_WAIT_MS`                          | 500     | Wait for a slot; past it 503 `OVERLOADED` with `Retry-After: 1`. 0 disables the gate.                                  |
| `LOOM_CH_TIMEOUT_READ_S`                        | 30      | Read deadline.                                                                                                         |
| `LOOM_CH_TIMEOUT_WRITE_S`                       | 60      | `INSERT` / `UPDATE` deadline.                                                                                          |
| `LOOM_CH_TIMEOUT_DDL_S`                         | 300     | DDL, `OPTIMIZE`, `SYSTEM` deadline.                                                                                    |
| `LOOM_CH_BATCHED_INSERTS`                       | true    | Batch the inserts written on every request, each holding a connection for its wait; false commits them, one part each. |
| `LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS`            | 10      | Shortest wait of a batched or waited async insert, what a lone insert waits; the connection is held for the wait.      |
| `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS`            | 50      | Longest wait of a batched or waited async insert a request waits on; the connection is held for it.                    |
| `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` | 200     | The same for the usage-event flusher's ticks and drain and the write-mark flush.                                       |

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

| Op class           | Covers                                                                                                        | Capacity | Refill / s |
| ------------------ | ------------------------------------------------------------------------------------------------------------- | -------- | ---------- |
| `memory`           | `memory.*`, `conversation.*` (except the exact-key reads and `memory.import`)                                 | 100      | 17         |
| `memory.read`      | Exact-key reads: `memory.get`, `bulk_get`, `history`, `working_memory.get`, …                                 | 300      | 50         |
| `memory.import`    | `memory.import`                                                                                               | 10       | 2          |
| `knowledge.search` | `knowledge.search`                                                                                            | 20       | 3          |
| `knowledge.query`  | `knowledge.query`                                                                                             | 20       | 3          |
| `knowledge.read`   | The other `read_only` `knowledge.*` ops, `knowledge.inspect` aside: `knowledge.get`, `knowledge.list`, …      | 50       | 10         |
| `knowledge.add`    | Every other `knowledge.*` op, `knowledge.inspect`, `knowledge.synthesize` and `knowledge.dedup_list` included | 10       | 2          |
| `code`             | `code.*` (except `code.outcome`, which draws from `memory`)                                                   | 60       | 10         |
| `default`          | Everything else                                                                                               | 50       | 10         |

These are the namespace-tier defaults (`DEFAULT_LIMITS`, `op_class_for`):

- **Per replica.** Buckets live in the process or host. Each replica holds the limit
  divided by the hosts with a live serving `loom.instances` row, so the replicas' sum
  approximates the limit. `LOOM_REPLICA_COUNT` is the floor of that count, and the count
  while the rows cannot be read.
- **Within a host.** With `LOOM_WEB_WORKERS > 1`, processes share one bucket table in a
  memory-mapped file (`LOOM_RATELIMIT_SHARED=auto`, `LOOM_RATELIMIT_SHARED_PATH`,
  `LOOM_RATELIMIT_SHARED_SLOTS`). `/v1/health` reports `ratelimit.backend`.
- **Fail open.** A limiter error admits the call and is counted.

**Why local buckets:** a ClickHouse round trip per admission would put the database on
every request's path to protect the database.

### Background workers

One supervisor runs per deployment. In `auto` mode every process of the deployment
(`LOOM_DEPLOYMENT=1`) contends for one lease in ClickHouse: key `workers_supervisor` in
the Keeper lock table `loom._migration_lock` (strict KeeperMap). Outside the deployment
(an iso or branch server on the same ClickHouse) `auto` runs no workers and the boot log
says so; `on` runs them there, but only the deployment's holder does the leader's work
(retention ALTERs, tracker namespace rows, the published configuration, the provider
participant sweep).
Expiry is judged on the ClickHouse server's clock. With T = `LOOM_SUPERVISOR_LEASE_TTL_S`
(default 30 s, at least 24):

| Step   | When                                                                         | Why                                                                                         |
| ------ | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Renew  | every T/6; a failed renewal is retried every T/12                            | Several failures fit before the holder gives up                                             |
| Lose   | T/2 after the last renewal that landed was sent                              | Measured from the send, so the server's stamp is never earlier                              |
| Stop   | at that deadline, even while a renewal call is still blocked                 | A hung call cannot keep the workers running; each call is sent once, timeout max(1, T/24) s |
| Drain  | up to T/6, so the workers are stopped by 2T/3                                | A workers child still running then is killed                                                |
| Expiry | T after the server stamped the last renewal; then another process takes over | T/3 of margin after the drain                                                               |

A holder that missed its renewals starts no new pass until it renews (a paused process
resuming does not run one beside the new holder), and one whose Keeper key still names it
takes the lease back at once instead of waiting out the TTL. A holder that finds the lease
taken stops its workers; when the holder dies, another process takes over after T. A
process that becomes the holder after boot does the leader's boot work too (tracker
namespace rows, published settings, `LOOM_RETENTION_*_DAYS`).

Stopping the workers cancels each pass at its next `await`. Work the cancel cannot reach
(a thread such as the PageRank or community computation, a pass that resumes after a
pause before its lease loop runs) stops at the pass's next `pass_context.check_stop()`,
which the long loops call between batches: the ingest legs, the SCIP delta rounds,
`code_pr_history`, `code_graph_stats` and `entity_communities`. It raises once the pass
was stopped or the lease is no longer fresh, so the pass sends no further write. There
are no fencing tokens: a statement already sent still lands.

A process stands aside (the holder stops its workers and gives the key up; a contender
does not ask) while:

- it drains after SIGTERM, so another process takes over during its ready delay;
- its configuration differs from the deployment's published one (it is not ready, and
  could write vectors in the wrong space or sign with the wrong root);
- a live `loom.instances` row of a newer build contends for the same lease.

The last is the rolling-upgrade hand-over. Each row carries its build's schema version
(its last migration number) and the lease it contends for right now: none while that
process drains, has drifted or gave the lease up, so such a row never makes an older
build yield. The hand-over fires only when the schema versions differ, so a code-only
release never hands over: the old holder keeps the workers until it stops. Once no
newer build is live (a rollback), the older one contends again. A process whose workers
child keeps failing to start gives the lease up for five minutes (below).

So:

- Several hosts can run in `auto`; one of them runs the workers. `on` skips the election.
- A second copy of the maintenance workers wastes ClickHouse work; a second copy of the LLM
  workers doubles their scans and spend.
- The holder runs the workers in a child process (`LOOM_WORKERS_PROCESS=child`), so a
  long pass does not share the event loop that serves its requests (passes on the
  serving loop roughly doubled request p90: `memory.get` 148 ms against 74–79 ms during
  a `code_ingest` pass). The child runs the
  supervisor and what its workers use (the ClickHouse client, the provider configuration,
  the event emitter, the tracker and repository registries), not the serving lifespan.
  It reads the lease deadline from its parent every second; the deadline stops moving
  once the lease is not fresh. The child is gone by that deadline plus T/6: it exits by
  itself then, its parent kills it then, and the kernel kills it when the parent dies
  (`PR_SET_PDEATHSIG`). The child's event loop beats to its parent every second, and
  `/v1/health` shows a child with no beat for 15 s as not alive. A draining process
  (SIGTERM) stops its workers, inline or in a child, and gives the lease up while it
  still serves. A child that exits within 60 s of starting five times in a row makes its
  process give the lease up and report `workers_lease_given_up`; another process takes
  the workers, and this one asks again five minutes later.

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
grows with namespace size. Quantized codes shrink the scan where the codec can be on
([gated settings](clickhouse.md#gated-clickhouse-settings)).
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

- **One worker leader per deployment.** Worker passes are not spread across hosts.
- **Per-replica rate limits** are approximate: bucket fill is per host. The sign-in and
  sign-up attempt limits are counted across every process in `loom.auth_attempts`.
- **Check-then-insert across replicas** (a sign-up's name and email, a personal
  namespace, an OAuth code or refresh token, an idempotency key in flight) runs under a
  deployment-wide claim, a strict KeeperMap key in `loom._migration_lock`, with the
  check read sequentially. A second
  contender gets a 409; a claim store that cannot answer is a 503 (an idempotency key
  then runs as a plain miss).
- **Events-stream telemetry** (budget violations) is drained on `SIGTERM`; a `SIGKILL` or
  OOM loses at most one flush interval of it, never audit or memory rows.
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
