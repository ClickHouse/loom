# Maintenance

Every command here is a `loom` subcommand; run it inside the container that has the
deployment's environment (`docker exec loom-server loom …`, or `loom-workers` in a split
deployment). The [CLI reference](../reference/cli.md) lists every flag. The code index
has its own runbook: [code index operations](./code-index-operations.md).

## Before you run a write

Most commands below are **dry runs by default**. Read the output, scope with `--org` and
`--namespace`, then pass the write flag (`--apply`, or `--erase` for an erasure; the tables
name it and the exceptions). A command that submits a mutation (`ALTER … UPDATE`) returns
before ClickHouse applies it; watch `system.mutations`.

## Re-embed and re-extract

- **`loom reembed`** re-embeds memory, entity and conversation vectors whose
  `embedding_model` differs from the configured embedder. Loom never re-embeds on its own,
  so run it after changing `LOOM_EMBEDDING_MODEL` (and `LOOM_EMBEDDING_DIM`). Scope with
  `--org` / `--namespace`; `--dry-run` counts the rows; `--delete-old` removes the old-model
  rows afterwards; `--key-only` rebuilds only the key-axis vector of each live row, except
  an import key (`import:` and a digest), which has none: a row without one is skipped, and
  one an older build embedded is cleared. `--in-database <named collection>` runs the
  embedding inside ClickHouse with `aiEmbed` (ClickHouse 26.7 or later) as one
  `INSERT … SELECT` per table instead of a Python loop. It is refused while a customer or
  identity header is set or the inference gateway is required: ClickHouse's own calls name no
  customer or identity and skip the gateway's host check. The Python path reads
  `--batch-size` rows at a time in sort-key order and prints a `resume:` token after each
  page; `--resume <token>` continues an interrupted run there. The Python path never sends a
  conversation turn whose text reads as high to the embedder: the row stays on the old
  model, which search does not read, until `--delete-old` removes it. A memory row's vector
  follows its sensitivity label. `--in-database` cannot check the text and re-embeds such a
  turn too; search still holds it back
  ([sensitivity](../architecture/security.md#sensitivity)).
  - Before each page's insert the Python path reads the page's rows again by key. A row
    deleted, tombstoned, or given other text or another agent or user since the page was
    read is skipped. Every other row is written with its other columns as that second read
    found them.
  - When a skipped row is an old-model row still stored with other text or identity,
    `--delete-old` keeps its org's old-model rows for a later run, which re-embeds all of
    that org's old-model rows (scope it with `--org`). The `resume:` token carries the
    orgs kept so far, and the run names them before it deletes.
  - A deletion, redaction or erasure that lands between that read and the insert, or whose
    mutation has not reached the row yet, can leave the row live with its new vector. Run
    such a redaction again once the run has finished, and delete such a conversation turn
    or thread again; the orphan-vector sweep tombstones a deleted memory's leftover
    vector, and an erasure's verify step finds the row and retries. `--in-database` reads
    each table once, as its statement starts, so run it while nothing redacts or erases.
- **A model of another width.** On ClickHouse 26.8 or later a fresh install codes its
  vector columns (`Quantized`) where `enable_quantized_codec` can be on
  ([ClickHouse](../architecture/clickhouse.md#gated-clickhouse-settings)), and a coded
  column takes vectors of the width it was created with only. Change to a model of
  another width (a multiple of 8) in Settings → Models & keys, which
  rebuilds the vector tables at the new width; `loom reembed` refuses a width the tables
  do not take, and editing `LOOM_EMBEDDING_DIM` alone makes every vector write answer 503
  `CODED_WIDTH_MISMATCH` while `/v1/health` reports `coded_width_mismatch`.
- **`loom rechunk --org ORG --namespace NS`** re-runs every live document through
  `knowledge.upsert` from its stored content, so documents pick up the current chunking,
  chunk context and vectors (`--source-uri` for one document). Dry run until `--apply`.
- **`loom lexical-stats-rebuild --org ORG --namespace NS`** recomputes the BM25 document
  frequencies from live rows. The term-frequency views see only inserts, so supersession,
  tombstones and bulk deletes leave stale vocabulary behind until this runs
  (`--no-memory` / `--no-knowledge` skip a side). It writes immediately.
- **`loom reextract --org ORG --namespace SRC --into DST`** replays a namespace's stored
  `set_from_messages` transcripts through a running server's current extractor into
  another namespace (`--token` is a writer token for `DST`, else `$LOOM_API_KEY`, else
  `~/.loom/token`). The source is only read, so you can compare the two namespaces before
  switching readers. `--dry-run` lists the sessions; `--limit-sessions N` replays a
  sample.
- **`loom recalibrate`** re-probes the embedder and recomputes the adaptive similarity
  thresholds immediately; the `recalibration` worker otherwise does it weekly.

Re-embedding is manual because a new vector space moves every similarity score. Running
it by hand keeps the switch, the cost and the re-baselining of search quality in one
operator step.

## Backfills and repairs

| Command                          | What it does                                                                                                                                              | Write flag |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `loom integrity-repair`          | prints the integrity gauges and the rows behind them; re-emits `dim0_with_vector` rows so search sees them (drift and `valid_to` rows are listed only)    | `--apply`  |
| `loom sensitivity-sweep`         | counts live unlabelled rows the sensitivity detector would label; writing labels them and tombstones a `high` row's embedding rows                        | `--apply`  |
| `loom reconcile-status`          | re-emits `memory_embeddings` rows whose status diverged from `loom.memory`                                                                                | `--apply`  |
| `loom demote-activity-narration` | finds extracted facts that narrate tool activity with no durable outcome and demotes or labels them through `memory.apply`; never deletes                 | `--apply`  |
| `loom namespaces-adopt`          | gives namespaces with live memory and no registry row an owner row                                                                                        | `--apply`  |
| `loom namespaces-gc`             | finds idle benchmark namespaces by prefix (or runs one `namespace_gc` pass with `--worker-pass`); submits their erasure                                   | `--erase`  |
| `loom audit-namespace`           | reports how a namespace is used (exact-key reads vs searches, oversized or double-encoded values, version churn); read-only; exit 1 on a critical finding | read-only  |

The integrity gauges these repairs answer are listed in
[monitoring](./monitoring.md#integrity-invariants). Reworking a namespace whose rows
have the wrong shape (documents stored as memories, leases stored as facts) is a guided
process in [optimize a namespace](../guides/optimize-namespace.md).

## Memory ingest drops

Every point where the ingest pipeline discards, truncates or gives up on a piece of data
records a drop (`server/memory/app/drops.py`, `record_drop`). Drops are buffered in
process, flushed to `loom.memory_drops` (kept 30 days), and mirrored to the counter
`loom.memory.drops{stage, reason}`. Recording a drop never raises and never blocks the
write that caused it.

```bash
docker exec loom-server loom drops --namespace NS [--org ORG] [--hours 24]
```

prints the counts by `stage` and `reason` for the window, with the last time each pair
was seen; `SELECT * FROM loom.memory_drops WHERE namespace = '…' ORDER BY ts DESC`
shows the items (`item` holds a key or field name, never used as a metric label).
Stages include `extract.parse`, `extract.judge`, `extract.truncate`, `write`,
`entity.extract`, `consolidate.merge`, `records.field`, `chunk.split` and
`conflict.deferred`.

An extraction that fails outright is not a drop: `set_from_messages` answers 502
`EXTRACTION_FAILED`, and partial failures come back in the response's
`pipeline_errors`.

## Entity graph maintenance

The writers of `loom.entity_edges` filter lexical junk (pronouns, interjections, light
verbs, contraction fragments, two-character tokens) out of the graph.

A namespace whose graph outlived its memories is listed by
`loom erasure-re-verify --org ORG --orphan-graph` and purged with `--erase`.

## Erasure

| Call                                                                            | Effect                                                                                                                                     | Use for                                                |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------ |
| `memory.clear` (`ADMIN`)                                                        | tombstones the memories of one `user_id` or `agent` in a namespace; `confirm_subject` must repeat the scope value                          | cleanup, test resets; reversible with `memory.restore` |
| `erasure.request` with `scope_type`: `user_id`, `namespace` or `namespace_list` | a resumable saga across every Loom table that can hold the subject's content, then the configured external backends (Langfuse, ClickStack) | a person's right to erasure; retiring namespaces       |

The saga checkpoints each step in `loom.erasure_requests` (`ReplacingMergeTree`, rows read
for 89 days, dropped at 90). After its steps it runs a residue census over every table the
read paths touch; any row still in scope fails the saga, and the final row carries
`residue_count`, `residue`, `verified_at` and `census`. A backend that cannot confirm
deletion fails the saga closed; the `erasure_recovery` worker retries it idempotently until
`max_attempts`, then leaves it `failed`. `purge: true` hard-deletes instead of tombstoning
(for throwaway namespaces; no restore window). `external: false` skips the external backends
and is accepted only for `namespace_list`. What erasure promises, and what it does not, is
in [retention and erasure](../features/memory/retention-and-erasure.md).

```sql
SELECT id, scope_type, scope_value, status, current_step, attempts, failure_reason, residue_count
FROM loom.erasure_requests FINAL
WHERE status != 'completed' ORDER BY requested_at DESC LIMIT 20;
```

`loom erasure-re-verify --org ORG` prints each failed saga's last reason and a fresh
census of its scope, read-only (`--id` for one saga), a user saga's with the `purge` it
was requested with. `--resume` re-runs failed sagas from their checkpoint, the same
way, past the recovery worker's attempts budget; a step that owns a table the last
census found rows in runs again even if the checkpoint lists it as done.
`--skip-external` (with `--resume`, failed `namespace_list` sagas only; refused with
`--completed-since`) finishes the Loom tables when an external backend keeps failing.
`--completed-since <ISO timestamp>` takes the completed sagas whose census ran at or
after that time instead of the failed ones (UTC unless the timestamp names an offset):
after a rollout, the erasures the previous build ran. For those it counts, and with
`--resume` deletes, only the orphan row links in the saga's scope (rows of
`entity_triple_rows` and `entity_edge_rows` whose memory has no live row and no row
written since the saga's census); it runs no saga step again, so data written after the
erasure stays, even once `memory.delete` has tombstoned it. If a saga cannot be
recovered, issue the same `erasure.request` again; the operation is idempotent.

### ClickStack erasure index

The ClickStack leg deletes OTel rows matched by attribute value. The ClickStack schema
gives every OTel table a bloom-filter index on its attribute maps, and
`schemas/otel/clickstack_otel_tables.sql` creates them with it. An `otel_metrics_*`
table created without `mapValues(Attributes)` (by another installer) makes each erasure scan it in full. `loom doctor` and `/v1/health` report the
missing index as `otel_erasure_index`. `CREATE TABLE IF NOT EXISTS` never adds it to an
existing table, so add it yourself, once per table:

```sql
ALTER TABLE default.otel_metrics_histogram
  ADD INDEX IF NOT EXISTS idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1;
ALTER TABLE default.otel_metrics_histogram MATERIALIZE INDEX idx_attr_value;
-- repeat for otel_metrics_sum and otel_metrics_gauge (and the other metrics tables if populated)
```

On ClickHouse Cloud, a `MATERIALIZE INDEX` issued immediately after its `ADD INDEX` can
build nothing. Check `data_compressed_bytes` in `system.data_skipping_indices` for the
table, and re-issue the `MATERIALIZE` if it reads 0.

## Retention

Loom keeps every row until its writer deletes it. Nothing is removed by age by default;
the retention gauges (`loom.retention.*`) only count candidates.

- **Per table.** `LOOM_RETENTION_<TABLE>_DAYS=N` for `audit_log`, `agent_events`,
  `memory`, `memory_embeddings`, `conversations`, `chunks` and `documents`.
- **Per namespace.** `retention_days` in the namespace's registration
  (`namespace.register`); the managed tables' TTL becomes one `multiIf` over every
  namespace that sets it.
- The workers' leader applies the `MODIFY TTL` at boot. After that only the
  `retention_reconcile` worker writes table TTLs, so a policy change reaches the tables
  within `LOOM_RETENTION_RECONCILE_INTERVAL_SECONDS` and a burst of registrations does
  not rewrite a table once each.

Hot/warm tiering uses a ClickHouse storage policy: `LOOM_TIERING_<TABLE>_WARM_DAYS=W`
plus `LOOM_TIERING_WARM_VOLUME_NAME`, the name of a volume in your `storage_policies`.

A few operational tables carry their own fixed TTL because they are diagnostics, not memory:
`loom.memory_drops` (30 days), `loom.worker_status` (30 days) and `loom.erasure_requests`
(90 days; its reads stop at 89). The [schema reference](../reference/schema.md) lists every
table's TTL.

## Compaction and OPTIMIZE

`loom.memory` and `loom.shared` are `ReplacingMergeTree(version)` tables: every update,
tombstone and restore writes a new version, and background merges collapse them. Readers
use `FINAL`, so uncollapsed versions cost read time, not correctness. A large
`count() / uniqExact(memory_key)` ratio on `loom.memory` means many uncollapsed
versions; after a large erasure, let merges catch up or run
`OPTIMIZE TABLE loom.memory FINAL` off-peak.

The code index's vector tables have a command that measures before it merges:

```bash
docker exec loom-workers loom code compact                     # measure copies per key
docker exec loom-workers loom code compact --table all --apply # OPTIMIZE … FINAL where the ratio is reached
```

It runs `OPTIMIZE … FINAL` on a code vector table (`code_chunks_prefix` by default,
`code_chunks`, `all`, or a table in `LOOM_CODE_VECTOR_TABLES`) once its physical copies
per key reach `--min-ratio` (default `LOOM_CODE_COMPACT_MIN_RATIO`, 1.5), and waits for
the merge (`--timeout-s`). `code_chunks` is the largest table in a code-heavy deployment;
compact it in an announced window. Bulk passes compact on their own after
`LOOM_CODE_COMPACT_AFTER_BULK_ROWS` rows.

**Changing a table's sort key, partition key or vector codec** is a rebuild, never an
`ALTER`: `scripts/ops/table_rebuild.py` and `scripts/ops/vector_codec_rebuild.py` copy
into a twin table, check it, swap with `EXCHANGE TABLES` and keep the old table for
`--rollback`. Both print their plan and change nothing without `--yes`. The sort-key and
partition-key procedure is in [migrations](../development/migrations.md#changing-a-sort-key-or-a-partition-key),
the vector-codec one in [recommended settings](deployment.md#recommended-clickhouse-settings).

`vector_codec_rebuild.py` needs a maintenance window with every writer and erasure
stopped and all in-flight writes drained. It never repairs the live table. Before
copying it refuses while a source mutation is still running or was created in the last
minute; the minute covers replica clock skew, and the gate before the swap counts it
too. Rerun a minute after the last mutation finishes. With `--reuse` it counts every
mutation since the reused twin was created, finished ones included: such a mutation
makes the twin stale, and the rerun is `--restart --yes`, which copies again. `--reuse`
also refuses a twin whose named columns, granularity or sorting key differ from the twin
the run would create (another `--method`, `--index-granularity` or `--chain`, or another
tool's twin): the gates read the stored vectors and the codes and would not see it. An empty table is rebuilt too, at the width the Width bullet below
gives (a coded column's own, else `LOOM_EMBEDDING_DIM`); the recall gate has no rows to probe there. Its ClickHouse user needs the
`SYSTEM FLUSH ASYNC INSERT QUEUE` and `SYSTEM SYNC REPLICA` privileges on the table and,
on `ReplicatedMergeTree`, `SELECT` on `system.zookeeper` and `system.replication_queue`
(which `loom ch-setup` does not grant) and `SYSTEM SYNC REPLICA` on the twin `loom.<table>_rebuild`, which
the gates synchronize before each read of it, and on `loom.<table>_pre_rebuild`, the
rollback copy the swap creates, which the checks after the swap and in
`--drop-pre-rebuild` synchronize. The preflight runs the first two once and refuses
(exit 3) when either fails, naming the error; the others can only run once their table
exists, and a refused twin sync fails the gate (exit 5).
`--dry-run` runs the first two as well, so it lands queued rows in the table and, on a
lagging replica, waits for the sync, and writes nothing else. Where a gate fails
(exit 5), stop writes and erasures and rerun with `--restart`, except when the table has
rows but no real vectors to probe: a rerun fails the same way, so drop the twin and skip
the table until it holds embeddings, as the run's `next` says. A width refusal (exit 3)
needs the re-embed below.

Keep writers and erasure stopped until the rebuild finishes or is abandoned and every
`<table>_rebuild` twin and `<table>_pre_rebuild` rollback copy is removed; a successful
swap prints that as its next step, in its log and in the summary line's `next`. Each next
step that names a command carries the run's `--method`, `--chain`,
`--index-granularity`, `--gate-fetch-multiplier`, `--probes`, `--require-patch-free`,
`--cluster`, `--statement-timeout-s` and, when it is not the default, `--abort-file`
(the twin's record lives beside it), so it runs as printed. Use
`--drop-pre-rebuild --yes` to remove the rollback copy after validation or rollback, and
`DROP TABLE loom.<table>_rebuild SYNC` to remove an abandoned twin only after a
`--dry-run` of the table's line prints a next step that starts `not swapped` (a twin
left for inspection, or a leftover never exchanged in). When it names
`RENAME TABLE loom.<table>_rebuild TO loom.<table>_pre_rebuild` or asks to compare the two
tables, do that first and drop nothing: the rename step means the exchange ran without
the rename, `<table>_rebuild` is the old table, and every run of the table's line
refuses with exit 3 (the same line with `--yes`, `--reuse` or `--restart` included), as
does `--drop-pre-rebuild`, until the rename runs. The tool reads
this from every vector column of the two tables, not from the run's flags, so a rerun
with another `--method`, `--chain`, `--index-granularity` or `--columns` refuses too: a
twin not known to be a leftover, under a live table with a coded vector column, may be
the old table. When
each column coded on the live table is plain on the twin, the tool names the rename.
When one is coded on the twin too (a `--require-patch-free` run, or a
rebuild of a coded table), the codecs do not show whether an exchange ran: the tool
refuses, renames and drops nothing, and asks to compare the two tables' rows before
dropping either by hand. A twin whose sorting or partition key differs from the live
table's may be the old table `scripts/ops/table_rebuild.py` exchanged out without its
rename, since that tool shares the twin's name: every line refuses, drops nothing, and
names `python -P -m scripts.ops.table_rebuild --table <table> --dry-run`, whose report
gives the rename. A twin never exchanged in is a leftover, whatever layout the
rerun names: `--restart --yes` copies again, or drop it. The tool knows a twin was never
exchanged in when its UUID, and the live table's, are the ones recorded in
`$LOOM_REBUILD_DIR/<table>_rebuild.uuid` (the abort file's directory) when the tool
created it, on any database engine: any exchange gives the live table's name another
UUID, including a `--reuse` swap run from another directory, which leaves the record
behind. The tool also removes the record just before its own exchange, and refuses the
swap when it cannot. Without a matching record, it reads a twin
whose `system.tables.metadata_modification_time` is later than the live table's as never
exchanged in, which holds on Atomic and Replicated databases, where `EXCHANGE TABLES`
keeps each table's own time, because the swap refuses (exit 3, twin kept) a live table
whose metadata time is later than the twin's: an `ADD INDEX` or `MODIFY SETTING` on the
live table during the gates creates no mutation but would make the old table the newer
one after a half swap. Rerun with `--restart` after such an ALTER. Equal times (the column has one second's precision), and any
other database engine, show nothing, so there the tool treats the twin as possibly the
old table: run the tool from the same `LOOM_REBUILD_DIR` each time.
`--drop-pre-rebuild` first compares the two tables again, after the same drain as the
postcondition, also when both are on the target layout (a `--require-patch-free`
rebuild of a table already on it), and refuses (exit 3) and keeps the copy on any
difference in [what the checks after a swap find](#what-the-checks-after-a-swap-find).
When both are on the target layout, `--rollback` and `--drop-pre-rebuild` take the table
with the later metadata time as the new one, on Atomic and Replicated databases only:
on equal times or any other engine both refuse (exit 3) and keep both tables for a
comparison by hand.
The erasure saga does not cover these copies.

- **Width.** The target width of a plain column is `LOOM_EMBEDDING_DIM`, so run the tool
  with the deployment's env; a coded column states its own. `LOOM_EMBEDDING_DIM` must
  equal the served model's width: writers use the width the server probes from the model
  at boot, and a stale `LOOM_EMBEDDING_DIM` only gets an `embedding dim mismatch` line in
  the server log. The tool refuses (exit 3), `--dry-run` included, while a plain named
  column holds real vectors (not all zeros) of another width: the copy would replace them
  with zero vectors, and a coded column rejects writes of any other width. The message
  names each width and `LOOM_EMBEDDING_DIM`. When every real vector has one other width
  and the server log names it as the model's, set `LOOM_EMBEDDING_DIM` to that width and
  rerun; do not re-embed. Otherwise re-embed to `LOOM_EMBEDDING_DIM` and rerun:
  `loom reembed --delete-old` for `memory_embeddings`, `entity_vectors` and
  `conversation_embeddings`, which re-embeds only rows of another `embedding_model`, so a
  width change with the same model goes through Settings → Models & keys; any other
  table, such as `memory_chunks`, `code_chunks` or `code_summaries`, is re-embedded by
  its own writer.
- **Gates (exit 5).** The consistency gate compares each key's newest version (or, on a
  table without a version column, its row count) and a hash of every column, the
  source's rebuilt vector columns cast and padded as the copy writes them. A key the twin
  lacks, or whose version is older, is copied again key by key, and the gate fails if any
  key still differs. A key only the twin holds fails it: rows were removed from the
  source after the copy, and the swap would bring them back. The recall gate ranks real
  vectors only, since a placeholder's cosine distance is NaN.
- **Rows that share a key and a version.** Under `FINAL` a ReplacingMergeTree keeps the
  last row inserted among a key's rows at its newest version, and the copy's parallel
  `INSERT` can insert them in another order, so the twin can keep a different row however
  often it is copied (two writes of one key in the same second, on a table versioned by a
  `DateTime`, are enough). When a key still differs, the gate counts such keys on the
  source and names them in its message. Run `OPTIMIZE TABLE loom.<table> FINAL` on the
  source inside the window, which leaves one row per key, then rerun with `--restart`.
- **Before the swap.** The tool flushes the source's async insert queue (and, on a
  replicated engine, runs `SYSTEM SYNC REPLICA`, which syncs only the replica that answers
  it; on `ReplicatedMergeTree` with `LIGHTWEIGHT`, which waits for fetches and not for
  merges). For `ReplicatedMergeTree`, connect directly to one replica throughout the run.
  Each later consistency and width read also reads, in the same statement, whether the
  replica answering it has copied the whole replication log of the tables it reads and has
  no entry other than a merge or a mutation left in its queue. A replica that has not
  pulled a log of one entry reports the same zero log pointer as one with an empty log, so
  the run reads the log's entries in `system.zookeeper` and passes the zeros only for a
  log with none; its user needs `SELECT` on `system.zookeeper` and on
  `system.replication_queue`, and a run that cannot read either refuses like a read on a
  replica behind. A read on a replica that
  has not caught up refuses the run in the preflight (exit 3), fails the gate before the
  swap (exit 5) and fails the postcondition after it (exit 7). Every later gate read and
  the last delta use `select_sequential_consistency = 1`. On `SharedMergeTree` this covers
  acknowledged writes even if another replica serves the read. On `ReplicatedMergeTree`
  the setting only covers quorum inserts with `insert_quorum_parallel = 0`; it does not
  replace the direct replica connection. The width and consistency checks fail the gate on
  a delete, a write of another width, a write the delta cannot pick up (a version below
  the twin's highest, or a row under a key the twin holds on a table without a version
  column), and any change to a row's columns that keeps its key and version, the rebuilt
  vectors included, such as a lightweight `UPDATE` or an erasure tombstone, whether or
  not its patch part is still there. Any other insert is copied in and the swap goes
  ahead: the window, not the tool, keeps writers out. The gate also fails on any source
  mutation still running or created since the copy started (with `--reuse`, since the
  twin was created), less a minute for replica clocks.
- **After the swap (exit 7).** The tool runs the consistency check again, the rollback
  copy as the source and the new table as the twin, with the same read settings and
  replica check as the last gate reads, and counts both tables' mutations since the
  copy. On `ReplicatedMergeTree` it first runs `SYSTEM SYNC REPLICA ... LIGHTWEIGHT`
  on the rollback copy, which has its own replication queue: a write that reached
  another replica before the swap belongs to the copy after the rename. `LIGHTWEIGHT`,
  because the swap stops the copy's merges and a full sync would wait on a merge entry.
  A merge changes neither table's rows and passes. Any difference fails the
  postcondition and keeps the rollback copy; the next step it prints is in
  [what the checks after a swap find](#what-the-checks-after-a-swap-find).

`--statement-timeout-s` (default 1800, below 7200, the client's read timeout) caps the
width, consistency, mutation, delta and recall reads, those after the swap and in
`--drop-pre-rebuild` included, and each delta and repair before the swap. The small
reads of a table's row count, columns and catalog row run without it, and the copy and
the rollback's catch-up copy, the reads that find its rows included, run under their own
limit of 7000 seconds. A read that
times out after the swap fails the postcondition (exit 7) with the rollback copy kept,
and one in `--drop-pre-rebuild` keeps the copy. Each width check (the preflight, before the swap, and on the rollback
copy after it) reads every vector of a plain named column (under `FINAL` on a ReplacingMergeTree), and
each consistency check (after the copy or the repair, before the swap, and after it)
reads every row of both tables, vectors included, so set the timeout to cover one full
read of the table.

### What the checks after a swap find

The run after the swap fails its postcondition (exit 7) and `--drop-pre-rebuild`
refuses (exit 3) on each finding below, and both keep the rollback copy. The summary
line's `next` carries the step.

| The checks find                                                                                                                                                                      | Next step printed                                                                                                                                           |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| After a swap, a change only the rollback copy holds: a key only there, a newer version there, an unfinished `ALTER UPDATE` on it, or an `UPDATE` that sets a vector of another width | `--rollback --yes`, which puts the old table back with the change                                                                                           |
| After a swap, a key only the live table holds or a newer version there: a write to the new table after the swap, or a delete in the old table after the last check                   | keep both tables and inspect the difference by hand                                                                                                         |
| A key whose content differs at one version: a lightweight `UPDATE` in either table, merged or not, or an `ALTER UPDATE` that finished before the swap                                | keep both tables and inspect the difference by hand                                                                                                         |
| After a swap, a mutation on the live table since the copy started, such as the lightweight `DELETE` that `loom reembed --delete-old` runs                                            | keep both tables and inspect the difference by hand                                                                                                         |
| The two tables name other columns                                                                                                                                                    | keep both tables and inspect the difference by hand                                                                                                         |
| After a rollback, a change only the new-layout copy holds, or a mutation on it since its creation                                                                                    | keep both tables and inspect the difference by hand                                                                                                         |
| The checks could not run: a failed drain or sync, a read past the timeout, or a replica that has not caught up                                                                       | fix the grant, connect directly to one replica, or raise `--statement-timeout-s`, and rerun the action that refused (`--drop-pre-rebuild`, or `--rollback`) |

`--rollback` copies back only the rows the old table lacks: on a table with a version
column, the versions above the old table's highest, and on `chunks`, which has none, the
keys the old table lacks. A lightweight `UPDATE` or `DELETE` or an erasure tombstone on
the live table keeps its key and version, and a version written below the old table's
highest is never copied back, so `--rollback` would lose such a change. It runs the
checks above before its exchange and refuses (exit 3), exchanging nothing, on every
finding whose next step is not `--rollback`. It starts the
old table's merges again, so an unfinished mutation on the rollback copy applies after
it, and the `--drop-pre-rebuild` that follows shows the keys it touched as differing at
one version. An unfinished `ALTER UPDATE` on the new-layout copy after a rollback does
not show under `FINAL`, which is why a mutation on it counts. A rollback that succeeds
names the next step in its summary: keep writers and erasure stopped and run
`--drop-pre-rebuild --yes`, which checks the two tables before it drops the new-layout
copy. A second `--rollback` refuses (exit 3) and names that step: the live table is no
longer the new layout.
A delete in the live table leaves its keys only in the rollback copy, so the first row
names `--rollback` only while the live table has no mutation since the copy started.

To inspect a difference by hand, keep writers and erasure stopped and list each key the
two tables hold differently, with both sides' version column. `<version>` is the
table's ReplacingMergeTree version column: `version`, or `created_at` on `memory_chunks`
and `entity_vectors` and `ts` on `conversation_embeddings`. `<key>` is the table's sorting key.
`<copy_vectors>` and `<live_vectors>` list only the vector columns the rebuild named
(`--columns`). `<columns>` is every other column, other vector columns among them, each
named as it is on both sides: the tool hashes them as stored.
For the columns the rebuild named, on the new-layout side (the live table after a swap, the copy after a rollback) a column
is named as it is. On the old-layout side it is written as the rebuild writes it,
`if(length(<column>) = <dim>, CAST(<column> AS Array(BFloat16)), arrayResize(CAST([] AS Array(BFloat16)), <dim>, toBFloat16(0)))`,
where `<dim>` is the codec's width (`Quantized(<method>, <dim>, 0)` in the new
layout's `SHOW CREATE TABLE`), so both layouts hash alike:

```sql
SELECT <key>, s.<version> AS copy_version, t.<version> AS live_version,
       s.__present AS in_copy, t.__present AS in_live
FROM (SELECT <key>, <version>, cityHash64(tuple(<columns>, <copy_vectors>)) AS __hash,
             1 AS __present FROM loom.<table>_pre_rebuild FINAL) AS s
FULL OUTER JOIN
     (SELECT <key>, <version>, cityHash64(tuple(<columns>, <live_vectors>)) AS __hash,
             1 AS __present FROM loom.<table> FINAL) AS t USING (<key>)
WHERE s.__hash != t.__hash
SETTINGS join_use_nulls = 0
```

On a table without a version column, drop `FINAL` and `<version>`, and read
`count() AS n, sum(cityHash64(...)) AS __hash ... GROUP BY <key>` on each side. Read the
full rows of each listed key in both tables, and check `system.mutations` for a mutation
that has not applied yet. Then:

- When the live table holds every listed key's change as writers meant it, and any
  mutation the checks counted has applied there, drop the copy with
  `DROP TABLE loom.<table>_pre_rebuild SYNC`.
- Otherwise, inside the window, copy each row only the copy holds into the live table
  (`INSERT INTO loom.<table> (<all columns>) SELECT <all columns> FROM
loom.<table>_pre_rebuild FINAL WHERE (<key>) IN (...)`; at one version the row inserted
  last wins), run the read again until it lists only changes the live table should keep,
  and drop the copy then. `<all columns>` leaves out MATERIALIZED and ALIAS columns,
  which an INSERT refuses. After a swap the live table is coded and refuses an empty
  vector or one of another width, so in the `SELECT` write each column the rebuild named
  as the rebuild writes it:
  `if(length(<column>) = <dim>, CAST(<column> AS Array(BFloat16)), arrayResize(CAST([] AS Array(BFloat16)), <dim>, toBFloat16(0))) AS <column>`.

A hand drop removes the only copy of what that table alone holds, so it comes after the
read, never before it.

## Disk pressure hygiene

On the Loom host:

- **Cap Docker's JSON logs.** A container that logs a stack trace per line can fill a
  disk fast. Set `log-opts` in `/etc/docker/daemon.json` (for example `max-size` 100m ×
  `max-file` 5).
- **Remove build cache and dangling images** with `docker builder prune` and
  `docker image prune`.
- **Do not prune containers.** `docker container prune` removes stopped containers, and
  in a [zero-downtime pair](./deployment.md#zero-downtime-pair) the stopped colour is
  your rollback ([backup and recovery](./backup-and-recovery.md#the-rollback-colour)).
- **Watch the code mirrors.** `var/code_repos` holds one bare clone per indexed
  repository; retiring a repository deletes its mirror when no other entry uses it
  and the mirror lives under `LOOM_CODE_REPOS_ROOT`; a mirror an operator placed
  elsewhere is left alone.
- **Keep probe output small.** On a full disk, commands that write temporary files fail
  too; write diagnostics to a memory-backed path such as `/dev/shm`.

In ClickHouse:

```sql
SELECT table, formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts WHERE active AND database = 'loom'
GROUP BY table ORDER BY sum(bytes_on_disk) DESC LIMIT 10;

SELECT name, formatReadableSize(free_space) FROM system.disks;
```

Let merges collapse versions after large erasures, compact the code vector tables, or
opt into per-table retention. The symptom-first version of this is in
[troubleshooting](./troubleshooting.md#the-host-or-clickhouse-runs-out-of-disk).

## Related

- [Monitoring](./monitoring.md): the gauges that tell you a repair is needed
- [Backup and recovery](./backup-and-recovery.md)
- [Code index operations](./code-index-operations.md)
