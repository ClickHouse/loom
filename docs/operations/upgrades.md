# Upgrades

An upgrade replaces the image and applies the migrations the new build carries. No other
state moves: everything durable is in ClickHouse, and the code mirrors are mounted
unchanged. This page is the
operator side. How a client sees unknown and removed fields is in the
[HTTP API reference](../reference/http-api.md#unknown-and-removed-fields).

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
2. Size the ClickHouse pool for batched inserts, which are on by default. Each batched
   insert holds a pooled connection while it waits, so a process needs at least
   R_request × `ch_batched_insert_wait_max_ms` / 1000 + R_background ×
   `ch_batched_insert_background_wait_max_ms` / 1000 connections on top of its other
   statements. R_request is the batched inserts a second that requests wait on: about 2
   per `conversation.append` and up to 4 per memory write. R_background is the
   background flushes a second (usage events, write marks, webhook attempts). Treat the
   result as a floor: a waiting insert also holds its connection through the flush, and
   a process at 200 writes a second refused writes with a pool at the bound (20) and
   none at twice it. Keep `LOOM_CH_POOL_MAXSIZE` at least three times the bound: raise
   it when three times the bound passes the current value, or when the process runs
   with a pool smaller than the default. The default 64 covers about 400 request-path
   batched inserts a second, for example 200 `conversation.append` a second, the
   measured rate. A
   process that cannot grow its pool starts with `LOOM_CH_BATCHED_INSERTS=false`
   ([batched inserts](../architecture/clickhouse.md#batched-inserts)). After the
   rollout, watch `loom.ch.pool.in_use`, `loom.ch.pool.waiters` and
   `loom.ch.pool.overloaded_total`; if `waiters` stays above zero or `overloaded_total`
   climbs, raise `LOOM_CH_POOL_MAXSIZE` or set `ch_batched_inserts` to `false` (live, no
   restart).
3. From the new build's checkout, against the production ClickHouse, run
   `loom migrate --dry-run --strict`. It lists what the boot will apply and stops on
   ledger problems ([troubleshooting](./troubleshooting.md#migrations-pending-or-checksum-drift)).
   If the release declares a table rebuild, run `scripts/ops/table_rebuild.py` for that
   table now.
4. Deploy the new image. The first container to boot applies the migrations; the others
   wait for it and then find nothing pending.
5. Wait for `/v1/health` to report `status: ok` and an empty `schema.pending` before
   routing traffic to it. The [zero-downtime pair](./deployment.md#zero-downtime-pair)
   does steps 3 to 5 for you: `redeploy.sh` runs the strict dry run in its preflight and
   swaps only after the new colour is ready.
6. Run the release's backfills, if any ([maintenance](./maintenance.md#backfills-and-repairs)).
7. Update client plugins when the release mentions a plugin change: hooks run on the
   client machine and change only through the client's plugin manager
   (`claude plugin update loom@loom` for Claude Code), never through a server deploy.
8. Once the last old replica has stopped, re-verify the erasures completed during the
   rollout: `loom erasure-re-verify --completed-since <when step 4 began> --resume`. An
   old replica's saga does not know the tables the new build added
   ([rolling upgrades](../architecture/rolling-upgrades.md), rule 14). The command
   deletes only orphan row links: rows of `entity_triple_rows` and `entity_edge_rows`
   in each erasure's scope whose memory has no live row and no row written since the
   erasure's census. Without `--resume` it counts them and exits 1 if any are left.
   Data written after the erasure stays.
9. Turn on a feature the previous build cannot honour only once every replica runs
   the new build: `memory_extract_agent_learnings`, since the previous build does not
   erase `loom.learnings_supporters`
   ([rolling upgrades](../architecture/rolling-upgrades.md), rule 23).

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
There is no migration to undo: the previous build reads and writes the newer schema.
Its version writes to `loom.memory` and the other `ReplacingMergeTree` memory tables
keep the columns it does not know
([rolling upgrades](../architecture/rolling-upgrades.md)).

There are no down-migrations. To return to an earlier schema, restore a backup taken
before the upgrade ([backup and recovery](./backup-and-recovery.md)).

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

The installer keeps `.env`, so the pin holds ClickHouse through later upgrades too:
delete the `LOOM_CLICKHOUSE_IMAGE` line before installing a newer Loom version, and do not
leave `LOOM_CLICKHOUSE_IMAGE` exported in the shell, since the installer writes a value it
is given back into `.env`. To go
back on both, restore a backup taken before the upgrade.

## What stays the same across an upgrade

| Across an upgrade                           | What holds                                                                        | Enforced by                               |
| ------------------------------------------- | --------------------------------------------------------------------------------- | ----------------------------------------- |
| The previous build against the new schema   | keeps reading and writing every table it knew, so a rollback needs no schema step | `tests/unit/test_migrations_additive.py`  |
| An applied migration file                   | never edited or deleted; a correction is a new file                               | the ledger checksum; `--dry-run --strict` |
| A client sending a field the server dropped | served; the field is named in `ignored_args`                                      | `server/transport/arg_check.py`           |
| The previous and new build serving together | each reads what the other wrote, new columns included                             | `make test-mixed-version`                 |

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
