# Upgrades

An upgrade replaces the image (and the document extractor's, which holds no state) and
applies the migrations the new build carries. No other state moves: everything durable
is in ClickHouse (and in the document bucket, for originals written with
`LOOM_DOCUMENT_OBJECT_STORE=s3`), and the code mirrors are mounted unchanged. This page
is the operator side. How a client sees unknown and removed fields is in the [HTTP API
reference](../reference/http-api.md#unknown-and-removed-fields). Which releases get fixes, and which
SDK works with which server: [supported versions](./supported-versions.md).

## How migrations run

The journal, numbering, runner directives and the rules for writing a migration are in
[migrations](../development/migrations.md). What an operator needs:

- **At boot.** The server applies pending files before it reports ready or starts
  workers (`LOOM_BOOT_MIGRATE=true`, the default). Every process, in any container, takes
  the `KeeperMap` lock in `loom._migration_lock`, so each file applies once. A server
  without a Keeper is refused.
- **Provider rebuilds.** A boot with files pending first enrolls as a provider
  participant, then checks under the provider configuration lock (one Keeper key, held for
  that check only) that no provider rebuild is unfinished, and refuses to migrate if one
  is: `Finish the provider rebuild before applying schema migrations`. The files then
  apply under the migration lock alone. A reconfiguration started meanwhile waits until
  the booting process acknowledges it, so a rebuild never runs beside a migration.
- **A container killed while it migrates or reconfigures** blocks the others only for a
  while. The migration lock is taken over once its holder stops renewing it (`ttl_s`).
  The provider lock (TTL 60 s, renewed every 20 s) is taken over once it expired and its
  holder's participant row expired too: no heartbeat for 60 s (one every 10 s) and no
  query tagged with its id in `system.processes` on any node. Containers booting together
  wait up to 120 s for the provider lock instead of failing. A lock whose holder has
  no participant row is freed with `loom provider-lock clear --holder <holder>` once
  that process is confirmed stopped; the command refuses while the holder renews,
  heartbeats or has a query running.
- **Mutations.** A file is recorded only once its mutations are done. A failed mutation,
  or one running past `LOOM_MIGRATE_MUTATION_WAIT_S` (default 600), fails the file and
  stops the boot with the file named in the log.
- **Key changes.** A migration that changes a sort or partition key rebuilds an empty
  table itself and fails the boot on one with rows. Run `scripts/ops/table_rebuild.py`
  for that table before the deploy; the strict dry run catches it.
- **Fresh install.** A fresh install and an upgrade are one path: the server replays
  whatever the ledger lacks. On an empty database that is the whole journal and takes a
  few minutes.
- **Stranded ledger.** A database whose ledger stopped partway through the files a
  baseline replaces is refused (`stranded`): migrate it with a build from before that
  baseline first.
- **Renumbered files.** A ledger row whose file the repository no longer has is
  `ledger-only`, and `--strict` exits 1 on it, except when its recorded SHA-256 is a
  current file's: that is the same file under a new number. The runner reports it as
  renamed, records the new name without running it again if it is pending, and
  `--strict` passes. A file edited after it ran under its old name has another SHA-256,
  so it stays ledger-only: its new name runs as a pending file, and the old row is
  removed by hand ([troubleshooting](./troubleshooting.md#migrations-pending-or-checksum-drift)).
  `/v1/health` lists the old row under `schema.ledger_only` until then.

The CLI runs the same runner by hand:

```bash
loom migrate --status              # applied and pending; changes nothing
loom migrate --dry-run --strict    # pending files; exit 1 on ledger-only rows (not renames) or checksum drift
loom migrate                       # apply pending migrations
loom migrate --target N            # apply through file number N only
```

`make migrate`, `make migrate-status` and `make migrate-dry` run the same module from a
checkout.

## Upgrade procedure

1. Read the release's [GitHub Release](https://github.com/ClickHouse/loom/releases) notes
   for operator steps (new settings, backfills a deployment must run) and the
   client-visible wire changes.
2. From the new build's checkout, against the production ClickHouse, run
   `loom migrate --dry-run --strict`. It lists what the boot will apply and stops on
   ledger problems ([troubleshooting](./troubleshooting.md#migrations-pending-or-checksum-drift)).
   If the release declares a table rebuild, run `scripts/ops/table_rebuild.py` for that
   table now. A deployment of the Postgres authority moving to a build that keeps
   knowledge document heads in Postgres unsets `LOOM_RETENTION_DOCUMENTS_DAYS` now, by
   hand: the newer build refuses to boot with it set, and under that authority no
   retention TTL reaches `loom.documents`, and a namespace's `retention_days` does not
   reach `loom.chunks` either
   ([rolling upgrades](../architecture/rolling-upgrades.md), rule 38).
3. Deploy the new image. The first container to boot applies the migrations; the others
   wait for it and then find nothing pending.
4. Wait for `/v1/health` to report `status: ok` and an empty `schema.pending` before
   routing traffic to it. The [zero-downtime pair](./deployment.md#zero-downtime-pair)
   does steps 2 to 4 for you, except unsetting the setting step 2 names: `redeploy.sh`
   runs the strict dry run in its preflight and swaps only after the new colour is
   ready.
5. Run the release's backfills, if any ([maintenance](./maintenance.md#backfills-and-repairs)).
6. Update client plugins when the release mentions a plugin change: hooks run on the
   client machine and change only through the client's plugin manager
   (`claude plugin update loom@loom` for Claude Code), never through a server deploy.
7. Once the last old replica has stopped, re-verify the erasures completed during the
   rollout: `loom erasure-re-verify --completed-since <when step 3 began> --resume`. An
   old replica's saga does not know the tables the new build added
   ([rolling upgrades](../architecture/rolling-upgrades.md), rule 14). The command
   deletes only orphan row links: rows of `entity_triple_rows` and `entity_edge_rows`
   in each erasure's scope whose memory has no live row and no row written since the
   erasure's census. Without `--resume` it counts them and exits 1 if any are left.
   Data written after the erasure stays.
8. Turn on a feature the previous build cannot honour only once every replica runs
   the new build: `memory_extract_agent_learnings`, since the previous build does not
   erase `loom.learnings_supporters`
   ([rolling upgrades](../architecture/rolling-upgrades.md), rule 23); and
   `LOOM_DOCUMENT_OBJECT_STORE=s3`, since the previous build reads a bucket row's empty
   `data` and answers its download `410 BLOB_GONE` (rule 9).
9. A deployment with `LOOM_CODE_ASK_MEMORY_NAMESPACE` set, now or at any time before,
   that moves from a build whose cached `code.ask` answers do not name the memory they
   quoted (one before `086_code_answer_memory_scope.sql`) to one that does, a roll
   forward after a rollback included: once the newer build serves alone, with no
   provider rebuild running, clear the answer cache with
   `TRUNCATE TABLE <LOOM_CH_DATABASE>.code_answers` (`loom.code_answers` by default). The
   old build's answers and tombstones name no memory, and an erasure or redact it ran
   during the rollout did not know the table
   ([rolling upgrades](../architecture/rolling-upgrades.md), rule 36), so no saga can find
   what they hold. A rebuild publishes its copy of the table over it, so one running
   across the truncate brings the rows back. A truncate drops the parts at once, on every
   replica of a replicated table, with no mutation to wait for, and the loom user holds
   the grant. Each cleared question costs one synthesis on its next ask.
10. Moving to a build whose user erasure reaches what a consolidated summary of the
    user's memories fed, a roll forward after a rollback included: once the newer build
    serves alone, request again each user erasure without `purge` requested before then
    (the `user_id` rows of `loom.erasure_requests`, kept 90 days), including one the newer
    build finished: an older build's saga left the summary's history, chunks, vectors,
    graph rows and shared copies ([rolling upgrades](../architecture/rolling-upgrades.md),
    rule 37).
    The new saga also erases what the user wrote since: request it for a user with no
    live memory row, or only where that is wanted. A user purge an older build completed
    left the same rows, and no later user erasure finds them: the user's keys are gone.
    One it ran past `shared.tombstone` also left the search rows of shared copies of the
    user's own memories, whichever build finished it. A shared copy an older build
    promoted with `agent="*"` names `*` for its source agent, and no erasure of the
    user it copied may find it. List them, and review each, with `memory.demote` for one
    that holds an erased user's text:

    ```sql
    SELECT org, namespace, memory_key FROM loom.shared FINAL
    WHERE tombstoned_at IS NULL AND arrayExists(x -> splitByChar('/', x)[3] = '*', promoted_from)
    ```

11. A deployment of the Postgres authority that moves to a build keeping knowledge
    document heads in Postgres (`002_knowledge_documents.sql`), a roll forward after a
    rollback included: once the newer build serves alone, run again every erasure that
    completed while a replica of an older build served since the newer build first
    served. An older build's erasure removes no head or delivery: an erased document's
    text stays in `loom.knowledge_heads` until the erasure runs again, and a delivery it
    left is published again by the delivery worker
    ([rolling upgrades](../architecture/rolling-upgrades.md), rule 38). Do it before
    retrying a parked delivery.
12. A deployment of the Postgres authority that moves to a build keeping knowledge
    ingestion jobs in Postgres (`005_knowledge_jobs.sql`), a roll forward after a
    rollback included: once the newer build serves alone, run again every erasure that
    completed while a replica of an older build served since the newer build first
    served. An older build's erasure removes no job from `loom.knowledge_jobs`, and the
    newer build's worker projects such a job back into `loom.knowledge_ingestions` and
    runs it until the erasure runs again
    ([rolling upgrades](../architecture/rolling-upgrades.md), rule 40). Step 11's runs
    cover both; one run per erasure is enough.

While a migration applies, the old build keeps serving: every migration is additive.
The rules that let the two builds serve side by side are in
[rolling upgrades](../architecture/rolling-upgrades.md).

## Rollback

Redeploy the previous image; with the pair, `./redeploy.sh --rollback`. Before rolling
back to a build without assistant-turn learnings, turn `memory_extract_agent_learnings`
off and delete the rows of `loom.learnings_supporters` and the `assistant_turn` rows of
`loom.learnings_queue`
([extraction](../features/memory/extraction.md#rules-the-assistant-states-about-itself)).
The previous build serves a promoted bot rule to every agent in the namespace, not only
its bot. To prevent that, delete the documents tagged `source-kind:assistant_turn` with
`knowledge.delete` before rolling back; otherwise accept the wider serving.
A namespace erasure the previous build runs once namespace guides exist leaves that
namespace's guide; run the erasure again once the newer build serves
([rolling upgrades](../architecture/rolling-upgrades.md), rule 24).
After a rollback to a build without bot token maximums, every bot's tokens last an hour,
and a bot that build saves keeps 3600 as its maximum; reapply a shorter or longer one
once the newer build serves
([rolling upgrades](../architecture/rolling-upgrades.md), rule 26).
A user erasure the previous build runs leaves what a summary of the user's memories
fed; request again each one run without `purge` once the newer build serves, as in step
10; a purge it completed is not found again (rule 37).
Do not roll back past the build that adds the document bucket while any catalog row
names an object in it: an older build answers those downloads `410 BLOB_GONE`, its
erasure deletes the rows and cannot reach their objects, and its sweep never walks the
bucket ([where originals are stored](../features/knowledge.md#where-originals-are-stored)).
After a rollback to a build before `086_code_answer_memory_scope.sql` with a memory
namespace bound to `code.ask`, clear the answer cache as in step 9 twice: once the older
build serves alone, and again once the newer build serves again. The migration is already
in the ledger, so the roll forward applies nothing that would prompt the second.
There is no migration to undo: the previous build reads and writes the newer schema.
Its version writes to `loom.memory` and the other `ReplacingMergeTree` memory tables
keep the columns it does not know
([rolling upgrades](../architecture/rolling-upgrades.md)).

There are no down-migrations. To return to an earlier schema, restore a backup taken
before the upgrade ([backup and recovery](./backup-and-recovery.md)).

### Downgrading an installer install

Each installer bundle names the ClickHouse release it was built with, so installing an
earlier Loom version also runs an earlier ClickHouse on a volume the newer one wrote,
which it may not read. To roll back Loom alone, keep the newer ClickHouse: before running
the earlier installer (it starts the stack at once), pin the image ClickHouse runs now
in the install directory's `.env`. This prints the line to put there, replacing any
earlier `LOOM_CLICKHOUSE_IMAGE` line:

```bash
dir=${LOOM_INSTALL_DIR:-$HOME/.loom}
echo "LOOM_CLICKHOUSE_IMAGE=$(docker inspect -f '{{.Config.Image}}' \
  "$(docker compose --project-directory "$dir" ps -q clickhouse)")"
```

Then run the earlier release's installer naming its version, as an older installer over a
newer install refuses otherwise:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/vX.Y.Z/install.sh | LOOM_VERSION=X.Y.Z sh
```

The installer keeps `.env`, so the pin holds ClickHouse through later upgrades too:
delete the `LOOM_CLICKHOUSE_IMAGE` line before installing a newer Loom version, and do not
leave `LOOM_CLICKHOUSE_IMAGE` exported in the shell, since the installer writes a value it
is given back into `.env`. To go
back on both, restore a backup taken before the upgrade.

## What stays the same across an upgrade

| Across an upgrade                           | What holds                                                                                                          | Enforced by                               |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| The previous build against the new schema   | keeps reading and writing every table it knew, so a rollback needs no schema step                                   | `tests/unit/test_migrations_additive.py`  |
| An applied migration file                   | never edited or deleted; a correction is a new file                                                                 | the ledger checksum; `--dry-run --strict` |
| A client sending a field the server dropped | served; the field is named in `ignored_args`                                                                        | `server/transport/arg_check.py`           |
| The previous and new build serving together | each reads what the other wrote, new columns included, except a feature switched on only after the rollout (step 8) | `make test-mixed-version`                 |

An upgrade can change or remove operations and fields. Check what a deployment serves
with the capability catalogue
([HTTP API](../reference/http-api.md#discovering-what-a-deployment-serves)), and list what
changed between two builds with `scripts/codegen/wire_changelog.py <previous> <new>`
([code generation](../development/code-generation.md#wire-changelog)).

## Related

- [Deployment](./deployment.md#zero-downtime-pair): the pair that makes an upgrade a swap
- [Migrations](../development/migrations.md): the rules a new migration follows
- [Rolling upgrades](../architecture/rolling-upgrades.md): the rules for builds N-1 and N
- [Troubleshooting](./troubleshooting.md#migrations-pending-or-checksum-drift)
