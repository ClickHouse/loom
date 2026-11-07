# Postgres memory authority

This guide sets up a Loom deployment whose live memory is held in Postgres, on
ClickHouse Managed Postgres or on a self-hosted Postgres, and covers what to check
before it serves. How it works inside is in [storage authority](../architecture/storage-authority.md).

The authority is chosen once, at a deployment's first boot. A ClickHouse that already
holds memory keeps the ClickHouse authority; start a Postgres deployment on an empty
ClickHouse.

## Requirements

- Postgres 16 or newer. No extension is needed.
- A database and a role that owns it (Loom creates the `loom` schema in it).
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
2. Create the database and the role, and take the **direct** connection string, on
   port 5432, from the service's connection details, not the pooler's: the service's
   pooler runs in transaction mode.
3. Set the [settings](#settings) and start Loom. The boot log reads the service's
   `synchronous_standby_names`; with two standbys it names them, and Loom starts.

Failover, standby placement and the old primary's fencing are the service's.

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

| Check      | Refuses to start when                                                                                                                   |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Connection | `LOOM_PG_DSN` is empty or Postgres does not answer                                                                                      |
| Migrations | an applied file of `schemas/pg_migrations/` was edited, or one this build lacks is older than its last                                  |
| Durability | with `LOOM_PG_DURABILITY=synchronous`, `synchronous_standby_names` is empty                                                             |
| Authority  | the deployment recorded the ClickHouse authority, this ClickHouse holds memory under it, or this Postgres belongs to another deployment |

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
the primary alone, and the delivery worker publishes it.

`GET /v1/health` reports `storage_authority` and, under Postgres, `pg_ok`; with
`?include=postgres` also a `postgres` block of `standby`, `parked_deliveries` and
`parked_audits`, statements the default answer leaves out of a liveness probe. `pg_ok` is
false when a query on the request pool got no answer within two seconds, every pooled
connection busy included. `/v1/health/ready` does not wait for Postgres: a replica that
cannot reach it still serves the planes that do not use it, and answers memory ops with
503 `POSTGRES_UNAVAILABLE`. `parked_deliveries` counts revisions committed in Postgres
that ClickHouse refused `LOOM_PG_DELIVERY_MAX_ATTEMPTS` times. One whose `targets` holds
none of 1, 2 and 4 has its memory row in ClickHouse (its search document too when it gets
one), its audit row there or in `loom.audit_outbox`, and owes only retirements: 8, its
deleted decision's record, which `decision.chain` still serves until it is retired; 16,
the vectors of the version it replaced, which search still finds until they are retired
(for a `high` row, its fence hides the served model's vectors meanwhile). A parked
revision is not retried; its `last_error` in `loom.memory_deliveries` holds the error's
type and its ClickHouse code, HTTP status or Postgres SQLSTATE, never its text, which may
quote the memory. This statement retries one:

```sql
UPDATE loom.memory_deliveries SET attempts = 0, held = false, lease_until = now() WHERE …
```

A publish that ends the worker process (out of memory on a huge value) counts as an
attempt when the next pass takes its claim over (`last_error` `stopped mid-publish`):
at the limit the revision is parked, or, when all it still owes is its search
document, gets a stub the embedding worker fills.

A publish that runs past its ClickHouse statement deadline counts as the rows' own
failure, since ClickHouse still answers: after a slowdown that parked healthy
revisions, the statement above with `WHERE attempts >= …` retries them all.

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

Back up Postgres with the provider's or the manager's tooling, together with
ClickHouse ([backup and recovery](backup-and-recovery.md)). A restored pair must record
the same deployment id and epoch; a Postgres from another deployment refuses to start.
