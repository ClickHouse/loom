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
  wait up to 120 s for the provider lock instead of failing. A lock held by an older
  build (whose participant row never expires) is freed with `loom provider-lock clear
--holder <holder>` once that process is confirmed stopped; the command refuses while
  the holder renews, heartbeats or has a query running.
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
  current file's: that is the same file under a new number (a branch migration
  renumbered past main's, as `026_clickhouse_only_state` became
  `033_clickhouse_only_state`). The runner reports it as renamed, records the new name
  without running it again if it is pending, and `--strict` passes. A file edited after
  it ran under its old name has another SHA-256, so it stays ledger-only: its new name
  runs as a pending file (every statement of `033_clickhouse_only_state` is
  `IF NOT EXISTS`, so that is safe for it), and the old row is removed by hand
  ([troubleshooting](./troubleshooting.md#migrations-pending-or-checksum-drift)).
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
   table now.
3. Deploy the new image. The first container to boot applies the migrations; the others
   wait for it and then find nothing pending.
4. Wait for `/v1/health` to report `status: ok` and an empty `schema.pending` before
   routing traffic to it. The [zero-downtime pair](./deployment.md#zero-downtime-pair)
   does steps 2 to 4 for you: `redeploy.sh` runs the strict dry run in its preflight and
   swaps only after the new colour is ready.
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
   erasure's census. Without `--resume` it counts them and exits 1 if any are left. Data written after the erasure stays.

While a migration applies, the old build keeps serving: every migration is additive.
The rules that let the two builds serve side by side are in
[rolling upgrades](../architecture/rolling-upgrades.md).

### From a build that kept state on the host

Builds before migration `033_clickhouse_only_state` kept some state outside ClickHouse.
After upgrading:

- **Document originals.** Move them from the old blob directory into
  `loom.document_blobs` once: `loom blobs import <dir>` lists what it would move (a dry
  run), `loom blobs import <dir> --apply` moves it. Until then `knowledge.download` of an
  older document answers 410, naming the command. The import then deletes the
  directory's files and stamps `blob_erased_at` on the deleted and erased documents' rows
  naming one of them. The old build kept a blob directory per host, so run the import on
  every host that held one. A live document whose file is not on this host, or fails its
  SHA-256, keeps its row (and its file, if any), and the command exits 1. Once every
  host has run it, restore the files still missing and run it again, or pass
  `--accept-missing` to give them up: that stamps every legacy row left, but never
  deletes a file a live document names, and a later run on the host that holds such a
  file still imports it. `loom doctor`'s `legacy_blobs` line warns while rows or
  old-layout files in this host's `LOOM_BLOB_DIR` remain. Nothing writes to `LOOM_BLOB_DIR`
  any more; keep its volume mounted until the import has run on that host.
- **Erasure before the import.** The new build's erasure deletes originals in
  `loom.document_blobs` only: it cannot delete a file on the old directory. It leaves
  such a document row unstamped (a purge supersedes it instead of deleting it), its
  census counts the row, and the saga fails closed (`failed`, retried by the recovery
  worker, abandoned after its attempts) until `loom blobs import --apply` on the host
  holding the file has deleted it and stamped the row. Run the import
  right after the upgrade; re-run an erasure abandoned meanwhile with
  `loom erasure-re-verify --resume` once it has. An erasure running during the import
  is safe: the import copies each original under a per-namespace claim that the
  erasure's documents step also holds, re-reading the document first, so it never
  copies the original of a document erased after it started.
- **Signing keys.** `LOOM_JWT_SECRETS_JSON`, `LOOM_JWT_ACTIVE_KID` and
  `LOOM_JWT_ALGORITHM` are removed, and the new build refuses to start while any is set
  (`LOOM_JWT_ALGORITHM=HS256` and `LOOM_JWT_ACTIVE_KID=default` alone excepted). Before
  the new build serves, copy the old keys into the keyring with the new image, the old
  variables still set: `loom signing-key import --active <the old LOOM_JWT_ACTIVE_KID>`.
  It reads `LOOM_JWT_SECRETS_JSON`, else `{<--active>: LOOM_JWT_SECRET}` as the old build
  did. Tokens signed under the old kids keep verifying; without the import only kid
  `default` (`LOOM_JWT_SECRET`) does. The active kid signs at once, which is right while
  no new-build process serves; import later with `--activate-in 300` so every new process
  loads it first. Then delete the old variables. Rotate with `loom signing-key rotate`
  ([tokens and grants](../guides/tokens-and-grants.md#revoke-and-rotate)).
  - **HS256.** A `default` entry in the map must equal `LOOM_JWT_SECRET` (the import
    refuses otherwise): this build signs and verifies HS256 `default` with the root
    secret only.
  - **RS256** (`LOOM_JWT_ALGORITHM=RS256`, a PEM `LOOM_JWT_SECRET` or PEM keys in the
    map). Add `--algorithm RS256`. The map's `default` key is stored as kid
    `default-rs256`: it signs from then on, as kid `default` so the old build's
    processes still verify what it signs, and it verifies the old RS256 tokens of
    `default`, and `/v1/.well-known/jwks.json` serves it under both kids. Keep
    `LOOM_JWT_SECRET` as it is: it still seals the stored credentials. A deployment
    without a map (`LOOM_JWT_ALGORITHM=RS256` and a PEM `LOOM_JWT_SECRET` alone) needs
    no import: its PEM root signs and verifies RS256 as kid `default`, and
    `LOOM_JWT_ALGORITHM` may stay set. The old RS256 kids also verify while
    `LOOM_JWT_PUBLIC_KEYS_JSON` still lists them, its `default` beside the root's.
- **Read-replicas of the old build.** The new build also publishes the root fingerprint
  in the fields the old build's replica checks (a key map holding `default`), so an old
  replica that restarts during the upgrade still boots, if it signs as `default`. A
  replica that signs with another kid of `LOOM_JWT_SECRETS_JSON` is refused until it is
  upgraded: upgrade those replicas with the main Loom.
- **Admin overrides.** Rate-limit and budget overrides set through `/v1/admin/*` are
  read from `loom.runtime_overrides`; the old `<bucket file>.overrides.json` is not
  read. Set them again.
- **Workers during this deploy.** The old build elects its supervisor with a file
  lock this build does not read, so while both colours' workers run, both run the
  workers. `redeploy.sh` recognises the old build by its lease reason (it names the lock
  file), builds the new image, stops `loom-<old>-workers`, then starts the new colour; if
  the new colour fails its checks before the swap, the script stops the new workers and
  starts the old ones again. `redeploy.sh --rollback` stops the current colour's workers
  before it starts the previous colour's, so a rollback to an older build runs one
  supervisor too. Without the script, stop the old workers before the new ones start.
- **Compiler-tier entry locks.** A full SCIP pass and a delta also take a
  deployment-wide key per entry (`scip_entry:<org>/<namespace>` in
  `loom._migration_lock`), so two hosts never index one entry at once. The lock file in
  `LOOM_CODE_SCIP_LOCK_DIR` is still taken first, for the host's cron scripts.
- **A Keeper is required.** Every lease and claim is a KeeperMap key, so a server
  without a Keeper is refused at boot, by `loom migrate` and by `loom ch-setup`. The
  bundled stack runs an embedded one; for your own server add
  `deploy/clickhouse-config/keeper.xml` to its `config.d/`, or point it at your Keeper.
- **Removed settings.** `LOOM_SUPERVISOR_LOCK_PATH`, `LOOM_SUPERVISOR_LEASE_RETRY_S`,
  `LOOM_GRAPH_WRITE_MARKS_SHARED`, `LOOM_GRAPH_WRITE_MARKS_DIR`,
  `LOOM_BOOT_MIGRATE_LEASE_SETTLE_S` and `LOOM_TASK_LEASE_BACKEND`. The supervisor lease,
  the graph-walk marks and every claim are in ClickHouse Keeper. The boot log names a
  `LOOM_*` variable that matches no setting; delete it from the env.

### Rolling replicas from a build before 033

A deployment that replaces replicas one at a time, rather than swapping a colour, runs
the old and new builds together for the length of the rollout. Five things in the old
build do not work beside the new one, so the upgrade from such a build takes two phases.

- **Workers.** The old build elects its worker leader with a host file lock, so an
  old replica with `LOOM_BACKGROUND_WORKERS=auto` runs the workers beside the new
  build's ClickHouse-lease leader.
- **Documents.** An old replica stores uploads in its host's blob directory, which is
  lost when the host is replaced, and answers 410 for a document a new replica stored
  in ClickHouse.
- **Claims.** An old replica keeps an OAuth exchange or a grant change apart in its
  own KeeperMap tables (`loom.oauth_claims`, `loom.grant_claims`), the new build under
  its leases, so across the two builds one code or refresh token can be exchanged
  twice at once, and two changes of one grant can interleave, until the last old
  replica stops.
- **Signing keys.** The new build refuses to start while `LOOM_JWT_SECRETS_JSON` is
  set, so the env change has to ship with the new image.
- **Dashboard.** The old build serves the dashboard at `/me` with its assets under
  `/me/assets/`, redirects `/` to `/me`, and publishes no bundle to
  `loom.frontend_assets`; the new build serves it at `/`, its assets under `/assets/`,
  and redirects `/me` to `/`. Across the two builds a page's assets 404 and `/` and
  `/me` redirect back and forth, so the dashboard does not load for most of the rollout.
  For this one upgrade, swap a colour (`redeploy.sh`) or make the load balancer sticky
  until the last old replica stops. The API and MCP are not affected. From this build
  onward a page of either build loads from any replica.

1. **Phase 1, old image.** Roll the old image with `LOOM_BACKGROUND_WORKERS=off` on
   every replica. Nothing else changes. The old build cannot see the new build's lease,
   so an old replica left on `auto` would run its workers beside the new leader for all
   of phase 2. The cost: no background workers run from the end of this phase until
   the first new replica holds the lease (extraction, consolidation and the code index
   catch up then).
2. **Phase 2, new image.** If the old build used a key map, first import it with the
   new image's CLI, the old variables still set (`loom signing-key import`, above). Roll
   the new image with `LOOM_BACKGROUND_WORKERS=auto` and the old key-map variables
   removed. The first new replica up takes the lease and runs the workers.
3. **After the last old replica stops,** run `loom blobs import <dir> --apply` against
   each old host's blob directory, while those directories still exist. Until then a
   document uploaded to an old replica has no original.

## Rollback

Redeploy the previous image; with the pair, `./redeploy.sh --rollback`. There is no
schema step: the previous build reads and writes the newer schema. Its version writes to
`loom.memory` and the other `ReplacingMergeTree` memory tables keep the columns it does
not know, if it is this build or later; an older build resets them to their defaults
([rolling upgrades](../architecture/rolling-upgrades.md)).

There are no down-migrations. To return to an earlier schema, restore a backup taken
before the upgrade ([backup and recovery](./backup-and-recovery.md)).

A build before `033_clickhouse_only_state` cannot serve an original that `loom blobs
import` moved into ClickHouse (its `blob_path` is a bare sha256), nor a token signed by a
key `loom signing-key rotate` added. Roll back before either, or keep the old blob
directory and the old key map until the new build has run for good. Every document
uploaded through the new build keeps its original in ClickHouse too, so after a rollback
to such a build its download answers 410; the original is not lost, and it serves again
once the new build is back.

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

| Across an upgrade                           | What holds                                                                          | Enforced by                               |
| ------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------- |
| The previous build against the new schema   | keeps reading and writing every table it knew, so a rollback needs no schema step   | `tests/unit/test_migrations_additive.py`  |
| An applied migration file                   | never edited or deleted; a correction is a new file                                 | the ledger checksum; `--dry-run --strict` |
| A client sending a field the server dropped | served; the field is named in `ignored_args`                                        | `server/transport/arg_check.py`           |
| The previous and new build serving together | each reads what the other wrote; runs and verdicts survive, new columns from 033 on | `make test-mixed-version`                 |

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
