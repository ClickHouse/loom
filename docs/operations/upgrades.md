# Upgrades

An upgrade replaces the image and applies the migrations the new build carries. No other
state moves: the blob volume and the code mirrors are mounted unchanged. This page is the
operator side. How a client sees unknown and removed fields is in the
[HTTP API reference](../reference/http-api.md#unknown-and-removed-fields).

## How migrations run

The journal, numbering, runner directives and the rules for writing a migration are in
[migrations](../development/migrations.md). What an operator needs:

- **At boot.** The server applies pending files before it reports ready or starts
  workers (`LOOM_BOOT_MIGRATE=true`, the default). Processes in one container take a file
  lock (`LOOM_BOOT_MIGRATE_LOCK_PATH`). Across containers the runner takes a `KeeperMap`
  lock in `loom._migration_lock`, or a lease row in `loom._migration_lease` where the
  server has no Keeper, so each file applies once.
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

The CLI runs the same runner by hand:

```bash
loom migrate --status              # applied and pending; changes nothing
loom migrate --dry-run --strict    # pending files; exit 1 on ledger-only rows or checksum drift
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
   table now.
3. Deploy the new image. The first container to boot applies the migrations.
4. Wait for `/v1/health` to report `status: ok` and an empty `schema.pending` before
   routing traffic to it. The [zero-downtime pair](./deployment.md#zero-downtime-pair)
   does steps 2 to 4 for you: `redeploy.sh` runs the strict dry run in its preflight and
   swaps only after the new colour is ready.
5. Run the release's backfills, if any ([maintenance](./maintenance.md#backfills-and-repairs)).
6. Update client plugins when the release mentions a plugin change: hooks run on the
   client machine and change only through the client's plugin manager
   (`claude plugin update loom@loom` for Claude Code), never through a server deploy.

While a migration applies, the old build keeps serving: every migration is additive.

## Rollback

Redeploy the previous image; with the pair, `./redeploy.sh --rollback`. There is no
schema step: the previous build reads and writes the newer schema, and ignores columns
the newer build added.

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

An upgrade can change or remove operations and fields. Check what a deployment serves
with the capability catalogue
([HTTP API](../reference/http-api.md#discovering-what-a-deployment-serves)), and list what
changed between two builds with `scripts/codegen/wire_changelog.py <previous> <new>`
([code generation](../development/code-generation.md#wire-changelog)).

## Related

- [Deployment](./deployment.md#zero-downtime-pair): the pair that makes an upgrade a swap
- [Migrations](../development/migrations.md): the rules a new migration follows
- [Troubleshooting](./troubleshooting.md#migrations-pending-or-checksum-drift)
