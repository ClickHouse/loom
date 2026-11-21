# Attaching the first cell

`loom cell attach` turns a prepared first cell (`loom cell prepare`, mode `recording`,
directory row `attaching`) into a `serving` one: it places every org the cell holds in the
router's directory, raises the cell's mode to `enforced` and creates its org triggers. This
page is the operator's procedure; the design is
[cells](../architecture/cells.md#attaching-a-cell).

## Before attach

Run attach at low load, as one run at a time, and not during a rollout, a provider
transition, a logical backup, a long erasure or a migration: each holds a transaction or a
snapshot long enough for step 4's wait to run out of `--wait`. Steps 2 and 4 run index skip
scans on the Postgres primary (each statement holds vacuum's horizon while it runs) and
read the `org` column of every ClickHouse table, which competes with serving load.

Every item below is a precondition attach checks, or one it cannot check and relies on:

1. Every router replica runs this release; every process and every CLI and CI image of
   the cell runs it too ([upgrades](upgrades.md#upgrade-procedure), steps 16 and 17).
2. `LOOM_CELL` is set on every process of the cell. Every CLI and CI configuration that
   writes for an org has `LOOM_CELL` set without `LOOM_CELL_ROUTER_TOKEN`: in `recording`
   it then refuses only new orgs, so nothing has to change at the mode commit. With the
   token removed, revoke the `INSERT` on `loom.tenancy` and the `UPDATE (since)` on
   `loom.tenancy_mode` the CI role held for recording new orgs.
3. The ClickHouse writer credential is rotated after that rollout
   ([upgrades](upgrades.md#upgrade-procedure), step 18).
4. No fingerprinted setting has changed since prepare, and none changes until attach ends:
   a change gates every process against `loom.cell_self`.
5. `LOOM_SERVES_TRAFFIC=false` is set only on pods no load balancer routes to: attach
   exempts them from the router check.
6. The cell's processes and the attach CLI share one `LOOM_INSTANCE_HEARTBEAT_SECONDS`:
   attach judges which processes are live by its own value.
7. `LOOM_PG_DSN` points straight at Postgres, with no pooler of any mode, for the servers
   and for attach: attach refuses a session that moves between backends or loses the
   tenancy marker, and refuses a server whose heartbeat reports `marked` false.
8. SCIM is off: attach refuses with it on.
9. On a ClickHouse cluster of several hosts, attach's user exists on every host, and the
   cluster has an interserver `<secret>` or a `remote_servers` user equal to attach's.
   One replica that cannot sync blocks attach (`SYSTEM SYNC REPLICA … ON CLUSTER`): an
   availability cost, not a safety one.
10. Only Loom's own roles and superusers hold the rights attach's tenancy-privilege check
    counts (inserting a tenancy row, changing the mode, creating in a schema, triggers on
    `loom` tables, owning them or their trigger functions, `session_replication_role`,
    `ALTER SYSTEM` of `loom.tenancy_checked`, `pg_write_server_files`,
    `pg_execute_server_program`), and no role that holds one, or owns what it counts, owns
    a `SECURITY DEFINER` function another role can execute that does any of that. Attach
    checks the catalog; it cannot read a function's body for intent, or a role that runs
    code as the server's OS user.
11. `postgresql.conf`, `postgresql.auto.conf` and any pending `ALTER SYSTEM` entry, on the
    primary and on every standby, set neither `loom.tenancy_checked` nor
    `session_replication_role`. Attach sees a live entry on the primary (its unmarked
    session reads it), never a standby's files.
12. Postgres has a synchronous standby attached (`synchronous_standby_names` set) and
    `max_prepared_transactions = 0`; the database encoding is UTF8.
13. No org longer than 2048 bytes is in the cell's stores (below). `--dry-run` lists any.

Attach's Postgres role needs the privileges of `pg_read_all_stats`
without `SET ROLE` (grant its membership with `INHERIT TRUE`), `USAGE` on schema
`loom`, `SELECT` on `loom.cell_first`, `loom.tenancy_mode`, `loom.cell_self`,
`loom.authority` and every org table, `UPDATE` on `loom.tenancy_mode`, `INSERT` on
`loom.tenancy`, `SELECT`, `INSERT`, `UPDATE` and `DELETE` on `loom.cell_probe`, `TEMPORARY`
on the database, `EXECUTE` on `pg_logical_emit_message`, and effective privileges of the owner
role of `loom.tenancy` and every guarded table without `SET ROLE` (grant their memberships
with `INHERIT TRUE`). Its ClickHouse user needs the grants the writer
check names (`SELECT` on `system.*` and on the configured databases, `SHOW USERS`,
`SHOW ROLES`, `SHOW ROW POLICIES`, `SHOW TABLES`, `SHOW COLUMNS`,
`SYSTEM FLUSH ASYNC INSERT QUEUE`, and on a cluster `SYSTEM SYNC REPLICA`, `CLUSTER` and
`READ ON REMOTE`), no settings profile or quota capping the rows or bytes it reads, and the
insert into `loom._migration_lock` that writes the probe nonce. The CLI needs write access
to the bucket's probe object and `LOOM_ROUTER_OPERATOR_TOKEN`.

## Run it

```bash
loom cell attach --cell <cell> --dry-run \
  --ch-allow-user <servers' ClickHouse user> --pg-allow-role <servers' Postgres role>
loom cell attach --cell <cell> \
  --ch-allow-user <servers' ClickHouse user> --pg-allow-role <servers' Postgres role>
```

`--dry-run` runs the refusals and step 2's scan, prints the count and a sample of the orgs
per source table, the ones it would skip, and whether the scan fit in half of
`--scan-budget`, and writes nothing; the router's own checks at `attach/begin` (the probe,
the hosts, the upstream) do not run. Name every ClickHouse user, user directory, object and
replica that may write the configured databases with `--ch-allow-user`,
`--ch-allow-user-directory`, `--ch-allow-object` and `--ch-allow-replica`, and every
Postgres role besides attach's own that may make an org held with `--pg-allow-role`; each
list is audited by the router. `--wait` (default 600 s) is the wall time the waits share;
every lock try runs at least once, with a lock wait of at least 100 ms, so `--wait 0` runs
one try of each wait with no retry. The final heartbeat checks get at least two heartbeats
plus the router timeout, with one extension for a process first seen booting; this floor
is not charged to `--wait`. `--scan-budget` (default 7200 s) bounds each full scan, and
step 2 must take at most half of it, since step 4 reads the same tables again.
Each Postgres mode or trigger try reserves cleanup time inside its client deadline.
On interruption its owner closes the session before cancelling transaction work.
A close that cannot complete uses transport abort and reports failure; that abort
does not establish that server locks or an unacknowledged commit have disappeared.
Retry the command to resolve an unknown commit through its durable state checks.

A refusal names what to fix. Rerun attach after fixing it: every step is idempotent, and a
rerun resumes by the directory row and the cell's mode. On a `serving` row it verifies
that the connected authority, configured cell and bucket match the directory registration,
then requires `enforced` mode before printing that it is done.

## The point of no return

Step 3's mode commit is one-way. Before it, a refused attach leaves the cell `recording`,
its tenancy rows and its triggers in place (they pass every row in `recording`), and the
cell can be backed out (the first-cell branch of
[troubleshooting](troubleshooting.md#a-process-with-loom_cell-unset-is-gated)):
`loom cell remove` serves only a first cell that never left `recording`. After it, scoped
principals place their orgs live within seconds, every refusal leaves the cell `enforced`
and `attaching`, and the cell goes forward: fix the cause and rerun attach. Unsetting
`LOOM_CELL` then gates every process, an outage, not a rollback. A row with `placed > 0`
always goes forward.

From `enforced` on, native minting requires the org to be held by this cell, with or
without `--platform-admin`. Admin account routes cannot bring in a new org (`WRONG_CELL`).
An already issued ordinary token scoped to its org places that org on its first request.
A direct call of the router's operator place route, or a live
placement with the cell's credential, cannot read the cell's Postgres and is accepted once
the directory mode is `enforced`; the cell's credential is the servers' alone.

## Orgs attach does not place

- **An org longer than 2048 bytes** refuses step 2, naming it, before any tenancy row is
  written. One first found in step 4 is skipped and named. Remove such an org by hand, in
  this order: its bucket objects, from the
  keys of its rows in `loom.documents`, `loom.document_blobs` and `loom.blob_uploads`, so no
  memory content is left outside erasure; then its rows in every table with an `org` column
  of both ClickHouse databases and of Postgres schema `loom`, the erasure ledger tables
  included; then its `loom.tenancy` row, on a session with `loom.tenancy_checked` set.
- **A name no request can carry** (not UTF-8, a NUL) found in a data table is skipped and
  named; its rows stay outside placement.
- **An org found only in a log table** (`agent_events` and the tables derived from it, the
  code search trace) is never placed: a log row keeps the org an unauthenticated caller
  typed. Once that org is placed in another cell, its log rows here are outside its
  erasure there.

`loom doctor --stray-orgs` names the orgs the cell's stores hold without a tenancy row,
smallest tables first within `--stray-orgs-budget` (default 60 s), every table the budget
left unchecked and every table it could not read. Remove a stray org's rows by the same
procedure as an over-long org's.

## After attach

`loom doctor` runs attach's point-in-time checks again on every run: it warns on a Postgres
role outside the owner roles, the superusers and its own `--pg-allow-role` that can make an
org held or lower the mode (`cell_tenancy_privilege`). Attach exempts the role it runs as,
the operator's; doctor does not exempt the role it runs as, so a CI or monitoring role
holding such a right is named. It warns too on a role or database setting or a subscription
that passes an unchecked writer (`cell_unchecked_writers`). A grant or a writer added after
`serving` passes attach's checks, which ran once; doctor is how it is found.

## Related

- [Cells](../architecture/cells.md)
- [Troubleshooting: cells](troubleshooting.md#cells)
- [Upgrades](upgrades.md)
