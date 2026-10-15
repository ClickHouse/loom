# Backup and recovery

A Loom backup is a ClickHouse backup plus the blob volume. All durable state is in
ClickHouse except two directories:

- the blob volume (`LOOM_BLOB_DIR`, default `var/blobs`): uploaded document originals,
  content-addressed by SHA-256. Back it up with ClickHouse.
- the code index's git mirrors under `var/code_repos`, which are rebuilt from their
  remotes.

## Choosing a backup

| Method                                                                  | Covers                                                    | Consistent snapshot          | Use for                                                  |
| ----------------------------------------------------------------------- | --------------------------------------------------------- | ---------------------------- | -------------------------------------------------------- |
| ClickHouse Cloud backups                                                | every table of the service                                | yes (service-level)          | disaster recovery on Cloud                               |
| `BACKUP DATABASE loom TO Disk(...)` / `S3(...)`, or `clickhouse-backup` | every `loom.*` table                                      | yes                          | disaster recovery on self-managed OSS                    |
| `dev/backup_loom.py` portable dump                                      | the core tables only (below)                              | per table, not across tables | moving an org or namespace between clusters; cold copies |
| `namespace.snapshot`                                                    | one namespace's curated memory, types, grants, audit tail | one call                     | copying or freezing a namespace through the API          |

## The portable dump

`dev/backup_loom.py` writes one `JSONEachRow` file per table, then a `manifest.json`
with the source database and, per table, the file, row count, byte size, SHA-256 and
`CREATE` statement (or the error that stopped it). Exit 1 means a table failed
(`complete: false`); exit 2 means bad arguments, no ClickHouse target, or an out-dir
that already holds a backup.

It covers `namespaces`, `memory`, `memory_embeddings`, `documents`, `chunks`, `shared`,
`erasure_requests`, `agent_events`, `audit_log` and `idempotency`, not every table in
`schemas/schema.sql`. Derived tables (such as `agent_cost_rollup_daily`, which refills
from `agent_events`) are not dumped. `--org` filters every table with an `org` column and
skips `idempotency`, which has none: its cached responses belong to every org.

`dev/restore_loom.py` verifies every file against the manifest (size, row count,
SHA-256), prints the plan, creates the target database and missing tables from the
recorded `CREATE` statements, then inserts. Nothing is written before the insert step;
`--dry-run` stops after the plan. It refuses:

- the live database (`loom`, or the database the server config names) without
  `--into-live`;
- a table that already holds rows in the backup's scope without `--into-nonempty`. Inserts
  append, so the plain MergeTree tables (`audit_log`, `agent_events`) would hold every row
  twice.

Both tools resolve their ClickHouse target like the server (`LOOM_CH_*` from the
environment or `.env`, each overridable by `--host`, `--port`, `--user`, `--database`,
`--secure`), refuse to run when no host is named, and read the password from
`LOOM_CH_PASSWORD` only.

```bash
python dev/backup_loom.py --out-dir ./backups/$(date +%F) [--org acme] [--tables memory,namespaces]
python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --dry-run
python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --skip-idempotency
./scripts/ops/backup_drill.sh [--full-restore]   # round-trip check into a scratch database
```

`scripts/ops/backup_drill.sh` dumps the live database to a temporary directory and
checks the manifest against a row-count snapshot. With `--full-restore` it restores into
a scratch database `<db>_drill`, compares every table's row count, and drops the scratch
database on exit. It only reads the live database. Run the drill on a schedule.

The dump is per table, not a snapshot across tables. Use it to move data between
clusters, not for disaster recovery.

## Verifying the audit log after a restore

`python dev/verify_audit_chain.py [--org X] [--ts-since YYYY-MM-DD] [--json]` recomputes
every `audit_log` row's content hash and exits 1 on a mismatch, 2 when it could not
verify (bad arguments, no target, a failed query), 0 otherwise (zero rows is a pass). A
mismatch report names the row by partition, timestamp and request id, never its actor,
subject or namespace. The `audit_verifier` worker runs the same per-row check on a
schedule. The check is per-row tamper detection, not a hash chain over rows; the hourly
checkpoints are chained separately ([audit](../features/observability/audit.md)).

## Undoing changes inside a namespace

These operations undo a bad write, a misbehaving agent or a wrong import without a
ClickHouse restore. Each is scoped to one namespace. `namespace.snapshot`,
`namespace.restore` and `namespace.rollback` are not on the MCP `all` listing; call them
by name or over HTTP.

| Operation            | Role                                                   | Does                                                                                                                                                                                                                                                                                                                                        |
| -------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.restore`     | `WRITER`                                               | brings one tombstoned key back                                                                                                                                                                                                                                                                                                              |
| `namespace.snapshot` | `ADMIN`                                                | freezes a namespace's memories, types, grants and an audit tail (`include_audit_tail_n`, default 1000) as a JSON payload; `include_embeddings=false` (the default) lets the target re-embed under its own model                                                                                                                             |
| `namespace.restore`  | `VIEWER` on a fresh target, `ADMIN` on an existing one | writes a snapshot payload into `target_namespace` (optionally `rebind_agent`); idempotent by `snapshot_id`. Rows keep `version`, `content_sha256` and the attestation columns, so a restore never strips `verified_by`                                                                                                                      |
| `namespace.rollback` | `ADMIN`                                                | returns a namespace's memories (or `memory_keys`) to what they were at `as_of`, in the bi-temporal journal: each changed key serves its earlier claims again as they were, and a key that served nothing then is retracted. Dry run by default (`dry_run=false` applies); at most `max_keys` per call (`truncated` says when to call again) |

`namespace.rollback` reads the bi-temporal journal (`loom.memory_temporal_commits`),
which records every write and is the authority on what each key served when. Rollback:

- refuses an `as_of` older than the namespace's history or in the future;
- leaves alone a key whose lock refuses the change, or whose earlier value a blacklist
  now refuses (`left_out`);
- never serves a redacted or erased value again;
- rewrites the current rows and their search projections after its commits.

```bash
AS_OF=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)   # GNU date; the instant before the bad writes
curl -s -XPOST http://localhost:7777/v1/namespace.rollback -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","as_of":"'"$AS_OF"'"}' | jq '{dry_run, reinstate, remove, truncated}'
```

## What is and is not recoverable

| Situation                                                  | Recoverable?   | How                                                                                                                                                                                      |
| ---------------------------------------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A memory tombstoned by `memory.delete` or `memory.clear`   | yes            | `memory.restore`, or `namespace.rollback` to before the delete                                                                                                                           |
| A memory overwritten by a later version                    | yes            | `memory.history` shows the versions; `namespace.rollback` reinstates one                                                                                                                 |
| An erasure with `purge: true`, or a purged bench namespace | no             | purge hard-deletes; there is no restore window. Only a backup taken before it                                                                                                            |
| An erasure that tombstoned (no `purge`)                    | treat as final | the saga's census verified the scope is gone from every read path; bringing it back would undo a deletion request, so restore from a backup only when the erasure itself was the mistake |
| A newer build's schema, after rolling back the image       | not needed     | migrations are additive; the previous build reads the newer schema ([upgrades](./upgrades.md#rollback))                                                                                  |
| An earlier schema                                          | from a backup  | there are no down-migrations; restore a backup taken before the upgrade                                                                                                                  |
| A single-node ClickHouse OSS server's disk                 | from a backup  | no replication and no point-in-time recovery: the node is durable only to its own disk                                                                                                   |
| Code index tables                                          | rebuildable    | `loom code backfill` re-derives them from the git mirror ([code index operations](./code-index-operations.md#first-index))                                                               |
| Rows removed by an opt-in retention TTL                    | no             | TTL deletes are final; only a backup taken before them                                                                                                                                   |

On a single self-managed OSS node, schedule `BACKUP DATABASE` (or `clickhouse-backup`)
to storage off the host. ClickHouse Cloud replicates and backs up the service itself, so
the portable dump there is for moving data, not for durability.

## The rollback colour

In a [zero-downtime pair](./deployment.md#zero-downtime-pair), `redeploy.sh` stops the
previous colour's containers (`loom-<colour>`, `loom-<colour>-workers`) instead of
removing them, and tags the previous image `loom:prev`. `./redeploy.sh --rollback`
starts them, health-checks them and swaps the proxy back. If the previous colour's
container is gone, the rollback refuses ("the previous build is gone"); redeploy a
known-good commit instead.

> **Warning.** Never run `docker container prune` or `docker system prune` on a host
> that runs the pair: it removes the stopped rollback colour. Free disk with
> `docker builder prune` and `docker image prune`. Never name a throwaway container
> `loom-blue` or `loom-green`: removing it by name removes a colour.

After a rollback, the rolled-forward colour keeps running until you stop it
(`docker stop -t 45 loom-<colour> loom-<colour>-workers`); the workers lease moves to the
rolled-back colour within `LOOM_SUPERVISOR_LEASE_RETRY_S` of that stop.

## Related

- [Upgrades](./upgrades.md): the rollback procedure and the additive-migration rule
- [Maintenance](./maintenance.md#erasure): erasure sagas and their recovery
- [History and provenance](../features/memory/history-and-provenance.md): what
  `memory.history` returns
- [Failure and recovery](../architecture/failure-and-recovery.md): how the system
  behaves when a component fails
