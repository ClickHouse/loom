# Migrations

This page holds the rules for writing a migration. How a deployment applies them, and the upgrade and rollback steps, are in [upgrades](../operations/upgrades.md#how-migrations-run). [`schemas/README.md`](../../schemas/README.md) maps the `schemas/` directory and lists the operator tools.

## The journal and the ledger

- The schema is a journal of numbered SQL files in `schemas/migrations/`, applied in
  numeric-prefix order by `server/core/schema/migrate.py`. `000_migrations.sql` creates the ledger;
  `016_baseline.sql` is the starting schema; every later file is a delta.
- Each applied file is recorded in `loom._migrations` with the SHA-256 of its contents.
  A re-run applies only new files. A changed file is **checksum drift** and a file the
  repository no longer has is a **ledger-only row**; `loom migrate --dry-run --strict`
  (a CI step and the deploy preflight) fails on either. A file renamed with its text
  unchanged is not ledger-only: its SHA-256 names it, and it is recorded under the new
  name without running again.
- Every file addresses `loom.<table>` and `loom_telemetry.<table>`. The runner maps them
  to `LOOM_CH_DATABASE` and `LOOM_CH_TELEMETRY_DATABASE` as it sends each statement
  (`server/core/ch_names.py`), so the checksum is over the file as written. A database
  is compared by value as `currentDatabase()` or a bound name, never a literal `'loom'`
  (`tests/unit/test_ch_names.py`). A file that changes the key or TTL of a table whose
  name starts with `_`, or renames or exchanges one, also updates its row in [storage
  layout](../architecture/storage-layout.md) and the reshape guard of
  `tests/unit/test_storage_layout_rows.py`.

## Numbering

A file is named `NNN_description.sql`. The runner orders by the integer prefix, not the
name (`server/core/schema/journal.py`, `migration_order`), so `1030_x.sql` runs after `220_y.sql`. Take the next free number, checking `main`
and open pull requests, and keep it: two files with one number still apply, but in the
lexical order of their suffixes, which is not the order anyone intended, and a rebase
that renames one re-applies nothing. `tests/unit/test_migration_numeric_order.py` fails on a
shared number.

## Rules for writing a migration

- **Never edit or delete a merged migration.** Deployments' ledgers hold its hash. A
  correction is a new file.
- **Additive only.** Add tables, columns, indexes, projections and views; drop a skip
  index or a projection no code under `server/`, `scripts/`, `bench/` or `dev/` names (the old build loses an
  optimisation, never a column); change codecs, defaults or table settings; widen types;
  extend an enum. Never drop or rename a table or a column, and never exchange tables.
  `tests/unit/test_migrations_additive.py` enforces this. Of views and tables it allows
  only `DROP VIEW IF EXISTS` of an insert-time materialized view and
  `DROP TABLE IF EXISTS` of a superseded table, each when no file under `server/`,
  `scripts/`, `bench/` or `dev/` names it: neither a serving build nor an operator tool
  (they run against the production service) reads it. Narrowing a type is a review question, since a regex
  cannot tell it from a widening.
- **Retire a column in two releases.** First ship a release whose code no longer reads or
  writes it; drop it only once no serving build names it.
- **Every writer carries a new column.** Adding a column to `loom.memory` or
  `loom.shared` means updating the row builders in
  `server/memory/store/rows.py` (`build_memory_version_row`,
  `build_embedding_row`) and every projection that feeds them in the same change:
  these are `ReplacingMergeTree(version)` tables read with `FINAL`, and a version row
  that omits a column silently zeros it for every reader. A new `loom.memory` column must
  also be added to `loom.memory_versions` and to the `memory_versions_mv` view's `SELECT`
  in the same migration, or the history stops recording it
  (`tests/unit/test_memory_versions_schema.py` compares the column lists).
- **A table that can hold memory content** also needs an erasure saga step and a
  residue-census row (`server/erasure/store/census.py`, `_CENSUS`);
  `tests/unit/test_every_table_is_classified.py` fails on a new table that is in no list
  ([adding a table](adding-a-table.md)).
- **Engine-portable DDL.** Declare vector columns `Array(BFloat16) CODEC(NONE)`; where
  the `Quantized` codec can be on the runner creates them coded
  ([gated settings](../architecture/clickhouse.md#gated-clickhouse-settings)). Use plain
  `MergeTree` engine names, which ClickHouse Cloud runs as `SharedMergeTree`.

**Why additive.** The build that was serving before a migration ran must still read and
write every table it knew afterwards. That makes a blue/green rollback a proxy swap
instead of a schema rollback, and lets the old colour keep serving while the new one
applies migrations at boot. The price is paid here: two releases to retire a column, and
rebuilds instead of `ALTER` for key changes.

## Runner directives

A migration file can carry comment directives the runner reads:

| Directive                           | Effect                                                                                    |
| ----------------------------------- | ----------------------------------------------------------------------------------------- |
| `-- loom: tolerate-error <code>`    | the file survives one named server error (for example a setting only some servers accept) |
| `-- loom: rebuild-if-empty <table>` | declares a target sort or partition key for `<table>` (below)                             |
| `-- loom: baseline covers N`        | the file replaces every file numbered N or lower that the repository no longer carries    |

**Baselines.** A baseline is numbered N, after the newest file it replaces. A fresh
database runs it. A database whose ledger holds file N records it without running and
keeps the replaced rows, which strict mode reports as superseded rather than as errors. So
a squash needs no hand edit of any ledger, and a build from before it still finds all of
its own files applied. A database whose ledger holds some replaced files but not file N
stopped partway: `loom migrate` refuses it (`stranded`) and changes nothing, since running
the baseline would skip the files it lacks and recording it would claim their schema.
Apply the missing files with a build from before the squash, then migrate again. Squash at
a release, not per change; the squash builds the baseline from `schemas/schema.sql`
(the dump body without the objects the files it keeps create) and proves it on a fresh
server, on a fully migrated ledger and on a partway one.

The runner also adapts statements at apply time without changing the files or their
checksums: it drops the experimental NLP flag where the server forbids it, creates vector
columns with the `Quantized` codec where it can be on, dropping a general codec chained
after it where `allow_suspicious_codecs` cannot be (`server/core/schema/vector_codec_ddl.py`),
creates every view under the user running the migration
(`DEFINER = CURRENT_USER`: the dump names its own user, which another server may not have), and waits for every mutation a file issues before
recording it (`LOOM_MIGRATE_MUTATION_WAIT_S`).

## Changing a sort key or a partition key

ClickHouse changes neither in place, so a migration declares the target and the table is
rebuilt, never altered:

```sql
-- loom: rebuild-if-empty code_test_facets
CREATE TABLE loom.code_test_facets ( ...every column, unchanged... ) ... ORDER BY (...);
```

That statement is the only copy of the target DDL. The runner (`server/core/schema/table_rebuild.py`)
compares the live table's `sorting_key` and `partition_key` with it: a match records the
file, an empty table (a fresh install) is exchanged for a twin with the target DDL on the
spot, and a table with rows fails the boot with a message naming the tool. A boot never
copies rows. On a deployment with data, the operator runs the tool first, one table at a
time:

```bash
python -P -m scripts.ops.table_rebuild --table code_test_facets --dry-run
python -P -m scripts.ops.table_rebuild --table code_test_facets --yes
```

Every changing action (`--yes` alone, `--rollback`, `--drop-pre-rebuild`) prints the
ClickHouse it talks to and its plan, and changes nothing without `--yes`. The tool copies
every row (not FINAL) into `loom.<t>_rebuild` in bounded batches, copies the parts written
meanwhile, checks the row count under FINAL and the newest version of a sample of keys,
swaps with `EXCHANGE TABLES`, and keeps the old table as `loom.<t>_pre_rebuild` for
`--rollback` until `--drop-pre-rebuild`. It refuses to swap, keeping the twin, when an
`ALTER` changed the live table during the copy: exit 6 for one ClickHouse runs as a
mutation (dropping, renaming or retyping a column, dropping an index, a TTL), exit 3 for
one that only moves its metadata time (an index, a setting, a column's default or codec).
An added column fails the copy (exit 1, no swap), or the run exits 3 after the last verify
pass. A rebuild does not carry an `ALTER` over: restate it in the target statement, then
rerun with `--restart`. It also refuses a table no migration declares.
`loom migrate --dry-run --strict` fails while a declared table still holds rows in the old
layout, so the deploy preflight catches a skipped step.

The target keeps the table's columns (so the swap is additive), and its sort key keeps
the old key's columns, reordered: that key is also what a `ReplacingMergeTree` merges on.
A key with other columns merges other rows and is listed with its reason in
`tests/unit/test_migrations_additive.py`. Name the rebuild in the release notes as an
operator step ([release process](./release-process.md#release-notes)).

## Postgres migrations

`schemas/pg_migrations/NNN_*.sql` follow the same rules: numbered, additive, never
edited once merged, and every `CREATE`, `ALTER` or `DROP` of a table names
`loom.<table>`, except a rename's new name, where Postgres takes no schema
(`tests/unit/test_storage_layout_rows.py`). A boot applies them under a Postgres
advisory lock, each file in one transaction with its ledger row. A migration waits at
most 5 s for a table lock (`lock_timeout`, then retried), so an `ALTER` never queues
every query of the table behind a long transaction. A build serves a ledger that holds a
later build's files, with a warning naming them; a ledger file it does not carry
numbered at or below its own last file, or one whose checksum changed, refuses the boot.

Every file runs in one transaction, so a migration builds no index on a large table: the
build would hold off the table's writes, every commit answering 503, until it ends. Such
an index needs `CREATE INDEX CONCURRENTLY` outside a transaction, and a boot waiting on
the advisory lock deadlocks with it (a waiting session holds a snapshot the build waits
out): a change that adds such a file also takes the lock by retrying
`pg_try_advisory_lock`.

## Schema dump and check

`schemas/schema.sql` is the dumped result of applying every migration: read it to
understand the current shape, never hand-edit it. After a migration change, regenerate it
against a fresh ClickHouse of the release CI tests (`clickhouse/clickhouse-server:latest`):

```bash
bash dev/ci_start_clickhouse.sh     # that image with Loom's server config
make migrate
make schema-dump                    # rewrites schemas/schema.sql only when the dump differs
make schema-check                   # exit 1 with the head of the diff; writes nothing
```

A dump that fails leaves the file as it was. Another ClickHouse version, a server where the
`Quantized` codec or its `LZ4` chain cannot be on, or a database holding rebuild twins dumps
differently, which is why the pinned container matters. Internal tables (`_migrations`, names with a leading `_` or `.`)
are left out. CI's `schema-check` job replays every migration on a fresh server, runs the
strict dry run, then `make schema-check`.

CI follows `latest`, so a new ClickHouse release that prints DDL differently turns
`schema-check` red with no Loom change. Read the diff the job prints: when it is only
formatting, run the four commands above (they pull the new release) and commit
`schemas/schema.sql` on its own. A change in meaning (a type, a default, an engine
setting) is a ClickHouse behaviour change to look into before dumping over it;
`LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D` runs the commands above on
the last release that dumped cleanly, to compare.

## Checklist

1. Take the next free number; write the file additively.
2. Update the row builders, `loom.memory_versions`, the erasure census and the analyzer
   cases if the change touches them.
3. `make migrate` against a local ClickHouse, then `make schema-dump` and commit
   `schemas/schema.sql` with the migration.
4. `make test-unit` (the additive, numbering and schema-classification tests) and the
   integration suite for the paths that read the new shape.
5. Note any operator step (a rebuild, a backfill) for the release notes.

## Related

- [Upgrades](../operations/upgrades.md): how the runner applies files on a deployment
- [ClickHouse](../architecture/clickhouse.md)
- [Schema reference](../reference/schema.md)
