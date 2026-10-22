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
  `embedding_model` differs from the configured embedder. Loom never re-embeds on its
  own, so run it after changing `LOOM_EMBEDDING_MODEL` (and `LOOM_EMBEDDING_DIM`). Scope
  with `--org` / `--namespace`; `--dry-run` counts the rows; `--delete-old` removes the
  old-model rows afterwards; `--key-only` rebuilds only the key-axis vector of each live
  row. `--in-database <named collection>` runs the embedding inside ClickHouse with
  `aiEmbed` (ClickHouse 26.7 or later) as one `INSERT … SELECT` per table instead of a
  Python loop. It is refused while a customer or identity header is set or the inference
  gateway is required: ClickHouse's own calls name no customer or identity and skip the
  gateway's host check. The Python path reads `--batch-size` rows at a time in sort-key
  order and prints a `resume:` token after each page; `--resume <token>` continues an
  interrupted run there. The Python path never sends a conversation turn whose text reads as
  high to the embedder: the row stays on the old model, which search does not read,
  until `--delete-old` removes it. A memory row's vector follows its sensitivity label.
  `--in-database` cannot check the text and re-embeds such a turn too; search still holds
  it back ([sensitivity](../architecture/security.md#sensitivity)).
- **A model of another width.** On ClickHouse 26.8 or later a fresh install codes its
  vector columns (`Quantized`) where `enable_quantized_codec` can be on
  ([ClickHouse](../architecture/clickhouse.md#gated-clickhouse-settings)), and a coded
  column takes vectors of the width it was created with only. Change to a model of
  another width (a multiple of 8) in Settings → Models & keys, which
  rebuilds the vector tables at the new width; `loom reembed` refuses a width the tables
  do not take, and editing `LOOM_EMBEDDING_DIM` alone makes every vector write answer 503
  `CODED_WIDTH_MISMATCH` while `/v1/health` reports `coded_width_mismatch`.
- **`loom rechunk --org ORG --namespace NS`** re-runs every live document through
  `knowledge.upsert` from its stored content, so documents pick up the current chunking
  and vectors (`--source-uri` for one document). Dry run until `--apply`.
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

The saga checkpoints each step in `loom.erasure_requests` (`ReplacingMergeTree`, rows
kept 90 days). After its steps it runs a residue census over every table the read paths
touch; any row still in scope fails the saga, and the final row carries `residue_count`,
`residue` and `verified_at`. A backend that cannot confirm deletion fails the saga
closed; the `erasure_recovery` worker retries it idempotently until `max_attempts`, then
leaves it `failed`. `purge: true` hard-deletes instead of tombstoning (for throwaway
namespaces; no restore window). `external: false` skips the external backends and is
accepted only for `namespace_list`. What erasure promises, and what it does not, is in
[retention and erasure](../features/memory/retention-and-erasure.md).

```sql
SELECT id, scope_type, scope_value, status, current_step, attempts, failure_reason, residue_count
FROM loom.erasure_requests FINAL
WHERE status != 'completed' ORDER BY requested_at DESC LIMIT 20;
```

`loom erasure-re-verify --org ORG` prints each failed saga's last reason and a fresh
census of its scope, read-only (`--id` for one saga). `--resume` re-runs failed sagas
from their checkpoint, past the recovery worker's attempts budget; a step that owns a
table the last census found rows in runs again even if the checkpoint lists it as done.
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

A few operational tables carry their own fixed TTL because they are diagnostics, not
memory: `loom.memory_drops` (30 days), `loom.worker_status` (30 days) and
`loom.erasure_requests` (90 days). The [schema reference](../reference/schema.md) lists
every table's TTL.

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
`--rollback`. Both print their plan and change nothing without `--yes`; the procedure is
in [migrations](../development/migrations.md#changing-a-sort-key-or-a-partition-key).

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
