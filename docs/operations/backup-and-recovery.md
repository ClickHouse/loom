# Backup and recovery

A Loom backup is a ClickHouse backup, plus the document bucket while any catalog row
names an object in it, and under the Postgres authority a backup of Postgres too.
ClickHouse holds the originals' catalog (`loom.document_blobs`, and on the default store
their bytes), the token signing keys (`loom.signing_keys`) and the admin runtime
overrides (`loom.runtime_overrides`). These sit outside ClickHouse:

- `LOOM_JWT_SECRET`, the root secret. It seals the stored credentials and the signing
  keys, so a backup restored without it opens neither. Keep it with the env config.
- the code index's git mirrors under `var/code_repos`, a cache of the remotes recorded in
  ClickHouse. A host without a mirror clones it again.
- `LOOM_DOCUMENT_S3_BUCKET`, holding the bytes of the originals written while
  `LOOM_DOCUMENT_OBJECT_STORE=s3` (switching back keeps them there); ClickHouse keeps
  each object's location.

## Choosing a backup

| Method                                                                  | Covers                                                    | Consistent snapshot          | Use for                                                                                                      |
| ----------------------------------------------------------------------- | --------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------ |
| ClickHouse Cloud backups                                                | every table of the service                                | yes (service-level)          | disaster recovery on Cloud                                                                                   |
| `BACKUP DATABASE loom TO Disk(...)` / `S3(...)`, or `clickhouse-backup` | every `loom.*` table                                      | yes                          | disaster recovery on self-managed OSS                                                                        |
| `BACKUP DATABASE loom_telemetry TO ...`                                 | the SLI rollup (`loom_telemetry.sli_minute`)              | yes                          | keeping SLO history past the 31 days a refold of the ledger recovers                                         |
| `dev/backup_loom.py` portable dump                                      | every Loom table (below)                                  | per table, not across tables | moving a deployment, an org or a namespace (originals in a bucket: [below](#the-portable-dump)); cold copies |
| `namespace.snapshot`                                                    | one namespace's curated memory, types, grants, audit tail | one call                     | copying or freezing a namespace through the API                                                              |

## The portable dump

`dev/backup_loom.py` writes one `JSONEachRow` file per table, then a `manifest.json`
with the source database and, per table, the file, row count, byte size, SHA-256 and
`CREATE` statement (or the error that stopped it). Exit 1 means a table failed
(`complete: false`); exit 2 means bad arguments, no ClickHouse target, or an out-dir
that already holds a backup.

It dumps every MergeTree-family table of the database, read from `system.tables`. It
leaves out the migration runner's `_`-prefixed tables, `.inner` tables, the KeeperMap
leases (live coordination, not data) and views, which `loom migrate` creates. The
manifest's `backup_version` is 3. `--org` filters every table with an `org` column and
skips the tables without one (`idempotency`, `signing_keys`, ...), which hold every org's
rows.

`dev/restore_loom.py` verifies every file against the manifest (size, row count,
SHA-256), prints the plan, creates the target database and missing tables from the
recorded `CREATE` statements, then loads each file into a staging copy of its table and
attaches the staged parts to the table. An attach fires no materialized view, so a
restored row is not copied again into a table the backup also restores (`memory_versions`
would otherwise hold every version twice), and the views keep running for the writes of a
live database. Restore into a database `loom migrate` prepared (the views exist) or into a
scratch database (tables are created from the recorded DDL). A backup holding a coded
vector table (`Quantized(` in its recorded `CREATE`) is refused before anything is
written, `--dry-run` included, where the target's codec settings cannot be read or
`enable_quantized_codec` cannot be on; where it can, the table loses a general codec after
`Quantized` if `allow_suspicious_codecs` cannot be on
([gated settings](../architecture/clickhouse.md#gated-clickhouse-settings)). The insert runs with
`optimize_on_insert = 0`, so every dumped row lands, duplicates of a
`ReplacingMergeTree` key included, and the table collapses them at its own merges.
Each table prints `… attaching <table>` before its attach and `✓` after it: if a run
is cut off, its last line names the table it was in; check that table's count before a
rerun with `--into-nonempty`, which would append it again.

The manifest records each insert-fired materialized view's source and target table.
Because an attach fires no view, restore refuses a selection that holds one end of a view
without the other: `memory` without `memory_versions` (and `memory_code_link_queue`) would
leave `memory.history`, `memory.redact` and `namespace.rollback` blind to the restored
versions, and `code_term_freq` without `code_chunks` would be filled again when the code
is re-ingested. No view joins `documents` and `document_blobs`, but restore refuses one
without the other too: documents without their originals answer 410 on every download, and
originals without their documents are what the originals sweep deletes. Both tools take
`documents` before every other table, so with `--into-live` and workers running, the sweep
never sees a restored original row before the document that names it (for the bucket's
objects, see below). Select both ends, or pass `--allow-view-split` when the target
already holds the rows. `dev/backup_loom.py --exclude t1,t2` leaves out tables a pass
rebuilds, together with every table a view fills from them (`code_chunks` takes
`code_term_freq` and `code_doc_stats` with it); `restore_loom.py --exclude` leaves tables
of a backup out.

Document originals (`document_blobs`) are binary. The dump writes them as raw bytes and
sets `output_format_json_validate_utf8 = 0` itself, so a profile that validates UTF-8
cannot replace them; a binary original takes about 1.6 times its size in the dump.

A row written with `LOOM_DOCUMENT_OBJECT_STORE=s3` holds only the object's location, and
the bytes are in the bucket. Back the bucket up with its own versioning or replication,
and restore it, into the bucket of the same name (each row names it), after the
ClickHouse backup that names its objects, or with `LOOM_DOCUMENT_BLOB_GC_ENABLED=false`
until both are back: a restored object may keep its old modification time (it depends on
how it is restored), so the bucket walk would delete each one whose `document_blobs` row
has not landed yet.

A versioned bucket keeps every deleted object as a noncurrent version, and replication
does not replicate deletes by default, so erased and swept originals stay in the bucket
(or its replica) until a lifecycle rule (`NoncurrentVersionExpiration`, and expiry on the
replica) removes them. That rule's window is how long an erased original can still be
restored, as a ClickHouse backup's retention is for rows.

A dump carries no original's bytes for such rows, so it cannot move them: rows restored
into another deployment still name the source bucket, its erasure refuses them as outside
its bucket and prefix, the source's sweep deletes the objects once the source's rows are
gone, and a target pointed at the source's bucket and prefix would sweep every object of
the source that the target's catalog does not name. Move them by re-uploading the
documents into the target, or move the whole deployment with the source stopped.

The staging copy repeats its table's engine, except that a replicated engine in a
database that is not `Replicated` becomes its local form with the same parameters (a
copy would claim the table's Keeper path again); the attach then replicates the parts.
A replicated table the restore creates keeps its recorded Keeper path only in the
backup's own database, and only once nothing replicates on that path any more (it refuses
while live replicas remain). In another database it gets an independent one: the server's
`default_replica_path` under a fresh UUID, or the database's own in a Replicated database.
It refuses when the server lacks the macros that default needs.
Each run names its staging tables `_restore_<run id>_<table>` and prints the run id
first, and each drops only its own. Restores into one database (say, two orgs) run one
at a time: a run claims `default._loom_restore_lock_<db>` (waiting up to five minutes
for another run's claim), looks at the target again, and applies the refusals below to
what the previous run left, so a second restore of the same scope refuses instead of
attaching rows that fold into the first's. The claim is a table in `default`, so on a
self-managed cluster it only orders runs through the same server: point every restore
at one. A run that crashed leaves its staging tables and its claim behind;
`restore_loom.py --database <db> --drop-staging <run id>` drops them, once no process of
that run is left.
Nothing is written before the insert step; `--dry-run` stops after the
plan. It refuses:

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
python dev/restore_loom.py --in-dir ./backups/<date> --into-live \
  --tables memory,memory_versions,memory_code_link_queue   # one end of a view needs the other
./scripts/ops/backup_drill.sh [--full-restore]   # round-trip check into a scratch database
```

`scripts/ops/backup_drill.sh` dumps the live database to a temporary directory and
checks the manifest against a row-count snapshot. With `--full-restore` it restores into
a scratch database `<db>_drill`, compares every table's row count, and drops the scratch
database on exit. A table that collapses rows of one key (`ReplacingMergeTree`,
`AggregatingMergeTree`, ...) may merge in the scratch database right after its attach;
fewer rows there is a warning once a merge has run, and a failure otherwise. It only reads the live database. Run the drill on a schedule.

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
by name or over HTTP, or use the [dashboard](../features/dashboard.md)'s Health →
Repairs tab, where a namespace admin downloads a snapshot, restores one and rolls back.

| Operation            | Role                                                   | Does                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.restore`     | `WRITER`                                               | brings one tombstoned key back                                                                                                                                                                                                                                                                                                                                            |
| `namespace.snapshot` | `ADMIN`                                                | freezes a namespace's memories, types, grants and an audit tail (`include_audit_tail_n`, default 1000) as a JSON payload; `include_embeddings=false` (the default) lets the target re-embed under its own model                                                                                                                                                           |
| `namespace.restore`  | `VIEWER` on a fresh target, `ADMIN` on an existing one | writes a snapshot payload into `target_namespace` (optionally `rebind_agent`); idempotent by `snapshot_id`. Rows keep `version`, `content_sha256` and the attestation columns, so `verified_by` stays; a payload value its column cannot hold (a memory id that is not a UUID) is refused with 422. A collection is refused: its snapshot restores into a plain namespace |
| `namespace.rollback` | `ADMIN`                                                | returns every memory head (or `memory_keys`) to the version it had at `as_of`: reinstates the older version as the new head, tombstones keys created after it. Dry run by default (`dry_run=false` applies); at most `max_keys` per call (`truncated` says when to call again)                                                                                            |

Under the Postgres authority a restore leaves out a memory an erasure removed from the
target while its [deletion evidence](../architecture/storage-authority.md) is kept, and
counts it in `counts.erased_skipped`. The evidence covers only the namespaces the
erasure removed the memory from, so a snapshot from before an erasure restored
elsewhere brings the erased memories back: re-run the same erasure (its scope) after
such a restore.

`namespace.rollback` and `memory.history` read `loom.memory_versions`, the append-only
mirror of every insert into `loom.memory` (merges drop superseded versions from
`loom.memory` itself). Rollback:

- refuses an `as_of` older than the namespace's history or in the future;
- skips hard-deleted keys (`skipped_no_head`);
- leaves the embedding worker to re-embed the reinstated heads.

```bash
AS_OF=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)   # GNU date; the instant before the bad writes
curl -s -XPOST http://localhost:7777/v1/namespace.rollback -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","as_of":"'"$AS_OF"'"}' | jq '{dry_run, reinstate, remove, truncated}'
```

## Under the Postgres authority

With `LOOM_STORAGE_AUTHORITY=postgres`, live memory, its revision journal and the
knowledge heads are in Postgres ([storage authority](../architecture/storage-authority.md)),
and a deployment has two stores to back up: Postgres and ClickHouse.

| Store      | Holds alone                                                                                                                                                | RPO                                                                                                                                                                                                                                                                                                                                        | Backed up by                                                   |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------- |
| Postgres   | every memory head and revision, knowledge heads, idempotency answers                                                                                       | 0 for acknowledged writes across a failover to the synchronous standby; a point-in-time restore loses what followed its point                                                                                                                                                                                                              | the HA manager's or the service's base backups and WAL archive |
| ClickHouse | search documents and vectors, the chunks of knowledge documents and of long memory values, history past what Postgres keeps, analytics, traces, audit rows | the time since its last backup, for what only it holds; memory revisions and knowledge documents Postgres holds are published again, but not their chunks: `loom reconcile` names each knowledge source to re-chunk, or past 20 its namespace (`loom rechunk`), and a long memory value is searchable by its excerpt until it is set again | the methods above                                              |

The recovery time of a restore is the store's own restore time plus `loom reconcile`,
which runs again what the restored store lost of the other's record.

### Restoring one store

1. Stop every server and worker of the deployment.
2. Restore the store from its backup, and note the backup's time from the backup's own
   metadata (for ClickHouse, the start of `BACKUP`; for Postgres, the recovery target).
3. Run `loom reconcile --restored clickhouse --point <time>` (or `--restored postgres`),
   which prints what it would run again; then the same with `--apply`.
4. Start the deployment.
5. After a ClickHouse restore, run each `loom rechunk` the reconcile printed: a
   knowledge document's chunks live in ClickHouse alone.

On the Helm chart ([Kubernetes](kubernetes.md)), with release `loom`:

1. Stop it: `kubectl scale deploy/loom --replicas=0`, and `deploy/loom-workers` too with
   `workers.enabled`; suspend the analytics export, if it is on:
   `kubectl patch cronjob loom-analytics-export -p '{"spec":{"suspend":true}}'`.
2. A managed service restores into a new service: point the release at it with
   `helm upgrade ... --set replicaCount=0 --set workers.replicaCount=0 --set autoscaling.enabled=false`
   (and the new `clickhouse.host` or `postgres.host`), so no pod starts. The point is the
   start time the service lists for the backup, or the Postgres recovery target.
3. Run the reconcile in a Job made from the web pod's own spec, so it has the release's
   environment, Secret and Postgres CA, its own component label (the NetworkPolicy
   selects it, the Service does not), and no probes:

   ```bash
   kubectl get deploy/loom -o json | jq '{apiVersion: "batch/v1", kind: "Job",
     metadata: {name: "loom-reconcile"}, spec: {backoffLimit: 0, template: {
     metadata: {labels: (.spec.selector.matchLabels
       | .["app.kubernetes.io/component"] = "reconcile")},
     spec: (.spec.template.spec | .restartPolicy = "Never" | .containers |= [.[0]
       | del(.livenessProbe, .readinessProbe, .startupProbe)
       | .command = ["loom", "reconcile", "--restored", "clickhouse", "--point", "<time>"]])}}}' \
     | kubectl apply -f -
   kubectl logs -f --pod-running-timeout=10m job/loom-reconcile
   kubectl wait --for=condition=complete --timeout=10s job/loom-reconcile
   ```

   Use `"postgres"` after a Postgres restore. A `wait` that fails while the Job is still
   active (`kubectl get job loom-reconcile -o jsonpath='{.status.active}'` reads `1`; a
   dropped log stream ends `logs -f` early) means it is still running: follow its logs
   again. A Job that failed (`.status.failed` reads `1`) exited non-zero: read its
   output, delete the Job, and run it again. After a clean dry run, run it with
   `"--apply"` appended.

4. Start it again: `helm upgrade` with the release's values file, carrying step 2's new
   host and without its three `--set` overrides, and resume the analytics export. Run
   each `loom rechunk` the reconcile printed with
   `kubectl exec deploy/loom -- loom rechunk ...`.

`loom reconcile --apply`:

- runs again every erasure the other store records as completed after the point (from
  the point's whole second, as the ledgers keep whole seconds), with no census (the
  other store holds what the user wrote after the erasure), and without the steps the
  restored copy shows ran before the point: the steps of the last ClickHouse checkpoint
  written before the point (a `BACKUP` copies its tables one after another from its
  start, so only those deletes are in every table it copied; a row merged away leaves
  every step to run) less those its census reopened, or the Postgres steps the ledger
  row records in the transaction each committed in (a step a process of the previous
  build ran is not recorded, and runs again), which run again would delete what the user
  wrote after them. Its restored ledger row reads completed once every step ran; until
  then, after a ClickHouse restore, it reads `failed` with "replay after a restore
  pending", a row neither the recovery worker nor `loom erasure-re-verify --resume`
  runs. One whose restored row read completed before the point, or that an earlier run
  completed, is not run again. Its replay answers' step drops the org's whole replay
  cache in both stores, as any erasure does. An erasure with no terminal state is left
  to the recovery worker, its checkpoint written again with the steps the restore undid
  reopened; after a ClickHouse restore, one whose restored row shows it completed or
  abandoned has its Postgres ledger row marked so instead. The resumed saga runs as any
  does, its census included, which counts what the user wrote in its scope since; so
  does one that completed after the point while its Postgres ledger row missed the
  completion, an edge the recovery pass closes at its first pass ten minutes after the
  request, or after the completion if that is later. An abandoned one is never run
  (`loom erasure-re-verify` owns it);
- after a ClickHouse restore: first holds, as deliveries that are not yet due, every
  Postgres revision and knowledge head committed after the point (less a writer's lease
  and an insert's bound, the margin a write drawn before the point may still commit
  after it) and every Postgres head ClickHouse holds at an older version; then runs the
  erasures, skipping their Postgres steps (they hold, and running them again would
  delete what the user wrote after the erasure); publishes the held deliveries; then
  runs again every redaction Postgres records after the point, which scrubs what
  ClickHouse restored and the cached answers and precedents quoting it;
- after a Postgres restore: runs the erasures' Postgres steps and the replay answers'
  on the restored copy, and every redaction ClickHouse records after the point; a
  redacted version that is the copy's live head again (the write that replaced it was
  lost) is scrubbed in place. What was written after the point is lost, as with any
  point-in-time restore; ClickHouse still holds copies of it, which search does not
  serve once Postgres has no matching head.

Knowledge ingestion jobs need no step of their own: a job's reads take the newer of
Postgres's row and ClickHouse's, and a job Postgres holds is projected again when a
queue offers it. A job that ended after a ClickHouse restore point is not offered again,
so `knowledge.inspect`, which reads ClickHouse, does not list it until its row expires.

A replay that did not finish leaves its ClickHouse row `failed` with "replay after a
restore pending"; run `loom reconcile` again until it exits 0 before starting the
deployment, and before rolling back to a build that predates the reconcile, whose
recovery worker would resume that row with a census.

It refuses before writing anything when the point is later than now, when a worker
reported a pass within two workers-lease terms (`LOOM_SUPERVISOR_LEASE_TTL_S`, a minute
by default), when the point is older than `LOOM_ERASURE_LEDGER_DAYS` (ClickHouse's
ledger reads keep 89 days), and for `--restored both`: erasures and redactions made
after both restore points are in neither store, and must be issued again from the audit
export. Both ledgers keep an erasure by when it was requested, so a point within the
longest an erasure runs of that edge can miss one requested before the edge and
completed after the point: keep backups younger than the ledger's days by that much. It
reads the code repository registry before it writes, and refuses when that read fails.
Every step it runs is idempotent, so a reconcile cut off, or one that exits 1, is run
again whole before the deployment starts; deliveries it held are listed as parked by
`GET /v1/health?include=postgres` until a run completes.

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

After a rollback, the rolled-forward colour's web container keeps running until you stop
it (`docker stop -t 75 loom-<colour>`, as the rollback prints). Its workers container was
already stopped before the swap, so the workers lease is with the rolled-back colour; only
when that colour has no workers container does the rolled-forward one keep running them.

## Moving a deployment

A new Loom process pointed at the same ClickHouse, or at a moved or restored one, gets
back every feature. Carry these:

- ClickHouse: the same service, a copy with its Keeper state, or a `dev/backup_loom.py`
  dump restored into a ClickHouse that `loom migrate` prepared;
- the env config (`.env` or the container environment);
- `LOOM_JWT_SECRET`. Without it no token verifies, and the stored credentials and
  signing keys do not open;
- while any catalog row names an object in it, `LOOM_DOCUMENT_S3_BUCKET` under the same
  name and prefix: the same bucket reached from the new host, or a bucket of that name on
  another endpoint (`LOOM_DOCUMENT_S3_ENDPOINT_URL` is not part of a row's location).
  Every row names its bucket, and Loom has no tool to rewrite it: a copy under another
  name leaves every download and erasure pointed at the old one.

Everything else comes back from ClickHouse: document originals stored in it, signing keys,
runtime overrides, the supervisor lease, revocations, sign-in attempt counts and
graph-walk invalidation marks. Git mirrors re-clone from their recorded remotes on the
next ingest pass, or on the first history read on the new host.

Bounded exceptions:

- The events stream (budget violations) is drained on `SIGTERM`. A `SIGKILL` or OOM loses
  at most one flush interval of that telemetry, never audit or memory rows.
- Provider reconfiguration waits on a dead process until a process on its host proves it
  gone, or, on Kubernetes, until the workers leader sees its pod gone. Elsewhere, one
  whose host is gone too (a move to a new host) is retired by hand with
  `loom participant retire --participant <id> --yes`
  ([provider reconfiguration](../architecture/provider-reconfiguration.md)).
- Rate-limit bucket fill is per host. It refills in seconds, so a new host starts with
  full buckets.

## Related

- [Upgrades](./upgrades.md): the rollback procedure and the additive-migration rule
- [Maintenance](./maintenance.md#erasure): erasure sagas and their recovery
- [History and provenance](../features/memory/history-and-provenance.md): what
  `memory.history` returns
- [Failure and recovery](../architecture/failure-and-recovery.md): how the system
  behaves when a component fails
