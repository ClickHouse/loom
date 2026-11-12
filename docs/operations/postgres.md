# Postgres memory authority

This guide sets up a Loom deployment whose live memory and knowledge document heads are
held in Postgres, on ClickHouse Managed Postgres or on a self-hosted Postgres, and covers
what to check before it serves. How it works inside is in
[storage authority](../architecture/storage-authority.md).

The authority is chosen once, at a deployment's first boot. A ClickHouse that already
holds memory keeps the ClickHouse authority; start a Postgres deployment on an empty
ClickHouse.

## Requirements

- Postgres 16 or newer. No extension is needed.
- A database and a role that owns it (Loom creates the `loom` schema in it).
- The role may run `pg_logical_emit_message` (the public may by default; the boot refuses
  otherwise and names the `GRANT`).
- For `LOOM_PG_DURABILITY=synchronous` (the default): at least one synchronous standby
  in another failure domain, so an acknowledged write survives the loss of the primary.
- `wal_level = logical` if the database will also feed a replication tool; Loom itself
  does not need it.

## Settings

| Setting                                          | Value                                                                                                                                          |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_STORAGE_AUTHORITY`                         | `postgres`                                                                                                                                     |
| `LOOM_PG_DSN`                                    | `postgres://loom:<password>@<host>:5432/<database>?sslmode=verify-full&sslrootcert=<CA file>`, the direct endpoint, never a transaction pooler |
| `LOOM_PG_POOL_MIN_SIZE`, `LOOM_PG_POOL_MAX_SIZE` | connections per Loom process (2 and 20); size `max_connections` for every process of every replica                                             |
| `LOOM_PG_DURABILITY`                             | `synchronous` (refuse to start without a synchronous standby) or `local`                                                                       |

Name the server's certificate authority and require `sslmode=verify-full`: it checks
the certificate and the host name, so nothing on the path can pose as the authority and
read the memory or the password. `require` and below encrypt without checking whom,
for a local development server only.

Loom pools its own connections. Point `LOOM_PG_DSN` past any pooler that hands a
client a different server connection per transaction (transaction pooling): Loom's
migrations hold a session advisory lock and its driver prepares statements, and such a
pooler keeps neither across transactions.

Every replica of the deployment, and its worker process, sets the same values. The full
list, with the admission and delivery settings, is in the
[configuration reference](../reference/configuration.md).

## ClickHouse Managed Postgres

1. Create the service with **two HA standbys**. With one standby the service
   replicates asynchronously, and an acknowledged write can be lost with the primary.
2. Create the database and the role, grant the role `pg_read_all_stats` so Loom can
   see which standbys are attached, and take the **direct** connection string, on port
   5432, from the service's connection details, not the pooler's: the service's pooler
   runs in transaction mode.
3. Set the [settings](#settings) and start Loom. Check that
   `GET /v1/health?include=postgres` reports `postgres.standby` `synchronous`: the boot
   refuses an empty `synchronous_standby_names` and warns when fewer standbys are
   attached than it names ([what the boot checks](#what-the-boot-checks)).

Failover, standby placement and the old primary's fencing are the service's. On
Kubernetes, the chart's `postgres` values set the [settings](#settings) ([ClickHouse Cloud
and Managed Postgres](clickhouse-cloud.md)).

## Self-hosted Postgres

Run Postgres under an HA manager that keeps a synchronous standby, fences the old
primary on promotion and promotes only a standby that holds every acknowledged commit.
Configure it in its strict synchronous mode: at least one synchronous standby
required, and writes refused rather than acknowledged by the primary alone when none
is attached. A manager that is not strict empties `synchronous_standby_names` when its
standby drops, and commits are then acknowledged by the primary alone.

Loom checks the standby setting at boot, and `GET /v1/health?include=postgres` reports
`postgres.standby` while it serves: `synchronous` when the standbys a commit waits for
(`n` of `FIRST n`, `ANY n` or `n (...)`, else one) are attached; `primary_only` when none
is named, so the primary alone acknowledges commits; `waiting` when fewer are attached,
so commits wait until they are; `null` when the state could not be read, because
Postgres did not answer or the role cannot see `pg_stat_replication`.

Point `LOOM_PG_DSN` at the manager's primary endpoint (the service or address that
follows the leader), never at one host, so a promotion moves Loom with it.

A primary with one standby and no manager, as `dev/ci_start_postgres.sh` starts for
tests, shows the settings:

```text
synchronous_commit = on
synchronous_standby_names = 'FIRST 1 (loom_standby)'
```

## What the boot checks

| Check      | Refuses to start when                                                                                                                        |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Connection | `LOOM_PG_DSN` is empty or Postgres does not answer                                                                                           |
| Migrations | an applied file of `schemas/pg_migrations/` was edited, or one this build lacks is older than its last                                       |
| Durability | with `LOOM_PG_DURABILITY=synchronous`, `synchronous_standby_names` is empty; under any value, the role may not run `pg_logical_emit_message` |
| Authority  | the deployment recorded the ClickHouse authority, this ClickHouse holds memory under it, or this Postgres belongs to another deployment      |

A migration waits at most 5 s for a table lock and is tried five times; a boot that
logs `Postgres migration <file> waited too long for a lock` four times and then fails
with `LockNotAvailableError` found a long transaction holding the table: end it
(`pg_stat_activity` names it) and start again. A boot that finds another boot
migrating waits for it, without a limit, and starts once its migrations are done.

Every Loom connection sets `synchronous_commit = on` itself, whatever the server's
default. The boot warns when fewer standbys are attached than a commit waits for, and
when its role cannot see `pg_stat_replication`. While they are not attached a commit
waits for them, up to `LOOM_PG_STATEMENT_TIMEOUT_MS` plus one second, and then answers 503
`POSTGRES_UNAVAILABLE` with `outcome_unknown`: the cancelled wait leaves the commit on
the primary alone. The delivery worker publishes it to ClickHouse only once the standbys
hold it, so a failover in between leaves nothing of it in ClickHouse
([storage authority](../architecture/storage-authority.md#only-durable-revisions-are-published)).
A publisher that sends another session's commit reads it again in a transaction that
commits a transactional logical message with the prefix `loom.durable`, so a
change-data-capture consumer of the database receives one per such read; the boot
refuses a role that may not write one.

`GET /v1/health` reports `storage_authority` and, under Postgres, `pg_ok`; with
`?include=postgres` also a `postgres` block of `standby`, `parked_deliveries`,
`parked_audits`, `parked_knowledge_deliveries` and `backlog` (each source's pending
and parked deliveries counted up to `LOOM_PG_BACKLOG_COUNT_CAP`, `capped` past it, the
oldest one's age and the reading's,
[pipeline SLOs](monitoring.md#pipeline-slos)), statements the default answer leaves
out of a liveness probe. `pg_ok` is false when a query on the request pool got no answer
within two seconds, every pooled connection busy included. `/v1/health/ready` does not
wait for Postgres: a replica that cannot reach it still serves the planes that do not
use it. It answers 503 `POSTGRES_UNAVAILABLE` for memory ops, for every knowledge op
that reads a document's current row from its head or commits one, for search and the
reads that check heads (`knowledge.search`, `knowledge.outline`,
`knowledge.synthesize`), and for the dataset ops, which read or commit the dataset
catalog there (`knowledge.dataset_get`, `knowledge.dataset_approve`, `knowledge.query`).
The knowledge ops that read or write ClickHouse alone, such as `knowledge.list` and
`knowledge.dedup_resolve`, keep serving. `knowledge.ingest`, `knowledge.ingest_status`
and `knowledge.ingest_cancel` answer 503 too, as each reads or commits its job there
(`loom.knowledge_jobs`); a run stopped by an outage counts that attempt and runs again
once Postgres answers.

`parked_deliveries` counts every memory delivery with an endless lease: revisions
committed in Postgres that ClickHouse refused `LOOM_PG_DELIVERY_MAX_ATTEMPTS` times, the
superseded versions whose cleared payload left nothing to send (below), and the
deliveries `loom reconcile` holds (`last_error` `held by loom reconcile`) until it
releases them. One whose `targets` holds none of 1, 2 and 4 has its memory row in
ClickHouse (its search document too when it gets one), its audit row there or in
`loom.audit_outbox`, and owes only retirements: 8, its deleted decision's record, which
`decision.chain` still serves until it is retired; 16, the vectors of the version it
replaced, which search still finds until they are retired (for a `high` row, its fence
hides the served model's vectors meanwhile). One out of attempts is not retried; its
`last_error` in `loom.memory_deliveries` holds the error's type and its ClickHouse code,
HTTP status or Postgres SQLSTATE, never its text, which may quote the memory. This
statement retries one (never a reconcile hold, which `loom reconcile` releases itself):

```sql
UPDATE loom.memory_deliveries SET attempts = 0, held = false, lease_until = now() WHERE …
```

Once the [payload clearing](../architecture/storage-authority.md#payload-clearing) pass
has run, a build without `010_revision_delivered_at.sql` cannot send a revision whose
payload was cleared: while such a build serves, a retry leaves those out, adding this to
its `WHERE` (one primary-key probe per delivery):

```sql
AND NOT EXISTS (SELECT 1 FROM loom.memory_revisions r
                WHERE r.org = memory_deliveries.org
                  AND r.namespace = memory_deliveries.namespace
                  AND r.seq = memory_deliveries.seq
                  AND r.row IS NULL)
```

A publish that ends the worker process (out of memory on a huge value) counts as an
attempt when the next pass takes its claim over (`last_error` `stopped mid-publish`):
at the limit the revision is parked, or, when all it still owes is its search
document, gets a stub the embedding worker fills.

A publish that runs past its ClickHouse statement deadline counts as the rows' own
failure, since ClickHouse still answers: after a slowdown that parked healthy
revisions, the retry statement above with `WHERE attempts >= …` retries them all.

One parked with `last_error` `payload cleared, nothing to send` is a version whose
payload the [payload clearing](../architecture/storage-authority.md#payload-clearing)
pass cleared and that a later version of its key has replaced: ClickHouse serves the
key's head, and that version's content is gone (a restore that skipped the
[runbook](backup-and-recovery.md#the-postgres-journal-and-a-clickhouse-restore)). Its
audit row is not sent again either: it stays in the revision's `audit` in
`loom.memory_revisions`. Retrying the delivery cannot succeed; once noted, delete its row:

```sql
DELETE FROM loom.memory_deliveries
WHERE lease_until = 'infinity' AND last_error = 'payload cleared, nothing to send';
```

The knowledge delivery worker publishes one delivery at a time, each retirement its own:
up to two inserts per delivery and `LOOM_PG_DELIVERY_BATCH_ROWS` deliveries a pass,
within one `LOOM_PG_DELIVERY_LEASE_SECONDS` lease, which on a backlog usually ends the
pass first. A pass the lease's end cuts short releases the rest uncounted, and the next
pass takes them.

`parked_knowledge_deliveries` counts knowledge documents committed in Postgres whose
`loom.documents` row ClickHouse refused `LOOM_PG_DELIVERY_MAX_ATTEMPTS` times: the head
serves `knowledge.get` and search, which leaves out the older version's chunks that
ClickHouse reads as live until the delivery's retirement runs
([knowledge search](../architecture/storage-authority.md#knowledge-search)). A parked
delete's retirement runs before its row: `knowledge.get` answers it deleted, search
serves none of its chunks, and `knowledge.restore` answers 503 while a publication of the
source may still land: until the delivery lands, and for the insert bound after. The
originals sweep keeps the original a parked live document names, and so does the erasure
of a user other than its writer. Its `last_error` in `loom.knowledge_deliveries` holds
the error's type and code. A delivery of a scope an older build erased during a rolling
upgrade is not retried: run that erasure again first ([rolling
upgrades](../architecture/rolling-upgrades.md)). This statement retries one:

```sql
UPDATE loom.knowledge_deliveries SET attempts = 0, lease_until = now() WHERE …
```

`parked_audits` counts the rows of `loom.audit_outbox` that ClickHouse refused as many
times, left there with their `last_error`: an erasure's audit rows, and those a claim
deferred because the holder it replaced may still insert them. This statement retries
one:

```sql
UPDATE loom.audit_outbox SET attempts = 0, due_at = now() WHERE …
```

## Rolling upgrades and rollback

Every replica must run a build that records the authority. A deployment of the
Postgres authority does not roll back to a build without it: such a build reads no
record and writes `loom.memory` directly ([rolling upgrades](../architecture/rolling-upgrades.md)).

## Backup

Back up Postgres with the provider's or the manager's tooling, and ClickHouse with its
own. A restore is of one store, followed by `loom reconcile`, which runs again in it
what the other records past the restore point ([restoring one
store](backup-and-recovery.md#restoring-one-store)). A Postgres from another deployment
refuses to start.
