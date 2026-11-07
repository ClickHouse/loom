# Schema reference

Loom keeps all of its state in one ClickHouse database, `loom`. The shape of
that database is defined by the numbered migrations in `schemas/migrations/`;
`schemas/schema.sql` is the result of applying all of them, dumped from a live
server. The table at the end of this page is generated from `schema.sql`.

## Where the schema comes from

| File                           | What it is                                                                                                                                                                  |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schemas/migrations/NNN_*.sql` | The journal. Applied in numeric order by `loom migrate` (and at boot with `LOOM_BOOT_MIGRATE=true`), each recorded with its SHA-256 in the `loom._migrations` ledger.       |
| `schemas/schema.sql`           | The canonical current state: every `loom.*` table and materialized view as ClickHouse prints it after a full replay. Read this to understand the shape; never hand-edit it. |
| `scripts/ops/dump_schema.py`   | Writes `schema.sql` (`make schema-dump`) and compares it with a live server (`make schema-check`, run in CI after a replay).                                                |

A migration change regenerates the dump in the same pull request:

```bash
docker exec loom-server loom migrate          # or: make migrate
make schema-dump                              # rewrite schemas/schema.sql
python scripts/codegen/docs_reference.py      # regenerate this page's table
```

The dump is taken against a fresh ClickHouse of the version CI pins
(`dev/ci_start_clickhouse.sh`); another server version can print the same
schema differently. Internal tables (`_migrations`, the migration lock and
lease, names starting with `_` or `.`) are left out of the dump, and so of this
page. How to write a migration (additive only, never edit a merged file) is in
[Migrations](../development/migrations.md).

## Reading schema.sql

Each object is one `CREATE … IF NOT EXISTS loom.<name>` statement under a
`-- loom.<name>` comment, tables first and then materialized views, each group
sorted by name. A table statement lists its columns, then its skip indexes
(`INDEX … TYPE …`) and projections, then the clauses the table below extracts:

- **`ENGINE`** decides what a read sees. `MergeTree` tables are append-only
  logs. `ReplacingMergeTree(version)` tables keep the latest row per sort key
  and must be read with `FINAL` (or an equivalent `argMax`) to see one row per
  key before merges run. `AggregatingMergeTree` and `SummingMergeTree` hold
  roll-ups that materialized views feed. One `KeeperMap` table (`task_leases`)
  holds task-inbox leases, so a claim is one Keeper write rather than a
  MergeTree part.
- **`ORDER BY`** is the sort and primary key. For tenant data it starts with
  `(org, namespace, …)`, so every tenant-scoped read prunes to one namespace's
  granules first; the key that follows is the row's identity within the
  namespace (`memory_key`, `run_id`, `path`, `pr_number`).
- **`PARTITION BY`** is most often a month of a timestamp (`toYYYYMM(ts)`,
  `toYYYYMM(created_at)`), which bounds merges and lets TTL drop whole parts;
  some registry-style tables partition by `org` or `(org, namespace)`, and many
  small tables are not partitioned.
- **`TTL`** is the schema-declared expiry. Most tables declare none: Loom keeps
  memory, knowledge and every other agent-written row until its writer deletes
  it. The TTLs the schema does declare sit on caches, traces, queues and
  operational logs (`embedding_cache`, `code_search_trace`, `events`,
  `idempotency`, …). Operators can add retention and
  tiering for the managed tables (`LOOM_RETENTION_<TABLE>_DAYS`,
  `LOOM_TIERING_<TABLE>_WARM_DAYS` for `audit_log`, `agent_events`, `memory`,
  `memory_embeddings`, `conversations`, `chunks`, `documents`) and per namespace (`retention_days`,
  `warm_days` on the namespace row); the leader applies those at boot as one
  `ALTER TABLE … MODIFY TTL` per managed table (`server/storage/retention_overrides.py`),
  and the `retention_reconcile` worker re-applies them on a timer, so a
  deployment's live TTL can differ from this column. See
  [Retention and erasure](../features/memory/retention-and-erasure.md).
- **Indexes** counts the skip indexes by type: `bloom_filter` and `set` for
  equality filters on ids, tags and statuses, `minmax` for time ranges,
  `ngrambf_v1` for substring search on keys, `text` for full-text search.
  Vector search indexes added by hand are not part of the dump.

A materialized view either writes to a target table on every insert into its
source (`TO loom.<target>`, "on insert" below), or is refreshable and recomputes
on a schedule (`REFRESH EVERY …`).

## Column conventions

These hold across the schema; a new table follows them.

- **The identity tuple.** Tenant rows carry `org`, `namespace` and `agent`
  (`LowCardinality(String)`), then `user_id` and `session_id`, plain `String`
  columns that hold the sentinel `'-'` when unset rather than `NULL`, so they
  can sit in a sort key. The
  transport fills them from the request; the first three are required. See
  [Identities and namespaces](../concepts/identities-and-namespaces.md).
- **Server-stamped provenance.** On the tables that carry them, `actor`
  (`human:<subject>` or `process:<subject>`, from the token) and
  `source_class` (`asserted`, `extracted`, `observed`, `derived`, `imported`,
  `external`) are set by the server on every write
  (`server/memory/app/provenance.py`, `server/memory/domain/provenance.py`). A caller cannot name its actor; it may
  only downgrade `source_class` to `external`, `imported` or `derived`.
- **Version columns.** A `ReplacingMergeTree(version)` table's `version` is a
  `UInt64` the writer increases on each new row for a key; other replacing
  tables version by a timestamp (`created_at`, `updated_at`, `seen_at`, …) or
  keep the last inserted row. A new version row
  replaces the whole row under `FINAL`, so every write path carries every
  persisted column forward; for `loom.memory` and `loom.shared` that is the
  row builders in `server/memory/store/rows.py`. A column a writer
  omits reads as its default for every reader.
- **Soft deletion.** Rows are tombstoned, not removed: `tombstoned_at` and a
  `status` enum (`active`, `conflicted`, `tombstoned`, `duplicate`,
  `superseded`, `rejected`, `proposed` on `loom.memory`) say whether a row is
  live. Readers filter on status; apart from an operator-set TTL, rows are
  physically removed only through erasure.
- **Content hashes.** `content_sha256` (`FixedString(64)`) is the hash of a
  row's content. It backs the `if_match_sha256` precondition on writes, the
  attestation columns (`verified_by`, `verified_at`, `verified_sha256`: a
  verification holds only while the hash it was made on still matches), and
  deduplication. The audit log stores hashes, not content.
- **History is a mirror, not the replacing table.** Merges drop superseded
  versions from a `ReplacingMergeTree`, so history lives in an append-only
  mirror: `loom.memory_versions`, filled by `memory_versions_mv` on every insert
  into `loom.memory`. A column added to `loom.memory` is added to the mirror and
  the view in the same migration.
- **Time.** `created_at` and `updated_at` are server time; `valid_at`,
  `valid_to` and `invalid_at` are the fact's own validity (valid time, not
  transaction time).
- **Derived columns** are `MATERIALIZED` (`value_preview`, `value_len` on
  `loom.memory`), computed at insert and never written by the application.

## Which table holds what

[ClickHouse](../architecture/clickhouse.md) covers which plane writes and reads each table
family, why the tables are shaped as they are, and how Loom uses ClickHouse (FINAL, skip
indexes, vector indexes, merges and the settings it depends on).

## How the table is generated

`scripts/codegen/docs_reference.py` (`render_schema`) parses each `CREATE`
statement in `schemas/schema.sql`: the `ENGINE`, `ORDER BY`, `PARTITION BY` and
`TTL` clauses, the types of the skip indexes and the number of projections,
and, for each materialized view, its target, the `loom` tables it reads and
whether it is refreshable. `make lint` fails when the page is stale.

## Tables and views

<!-- BEGIN GENERATED: schema -->
<!-- Generated by `python scripts/codegen/docs_reference.py`; edit the source, not this region. -->
<!-- prettier-ignore-start -->

181 tables and 19 materialized views in the `loom` and `loom_telemetry` databases; a `loom_telemetry` object keeps its database prefix.

#### Tables

| Table | Engine | ORDER BY | PARTITION BY | TTL | Indexes |
| --- | --- | --- | --- | --- | --- |
| `acl_versions` | `SummingMergeTree(changes)` | `(org, kind)` |  |  |  |
| `agent_cost_rollup_daily` | `AggregatingMergeTree` | `(org, namespace, agent, day, operation)` | `toYYYYMM(day)` |  |  |
| `agent_events` | `MergeTree` | `(org, namespace, agent, ts)` | `toYYYYMM(ts)` |  | 2 skip index (bloom_filter, minmax) |
| `agent_run_claims` | `KeeperMap('/loom/agent_run_claims')` |  |  |  |  |
| `agent_runs` | `ReplacingMergeTree(version)` | `(org, namespace, run_id)` |  |  | 1 skip index (bloom_filter) |
| `agent_turn_usage` | `ReplacingMergeTree(version)` | `(org, namespace, session_id, message_id)` | `toYYYYMM(turn_at)` |  |  |
| `audit_checkpoints` | `MergeTree` | `window_start` |  |  |  |
| `audit_log` | `MergeTree` | `(org, namespace, event_type, ts)` | `toYYYYMM(ts)` |  | 1 skip index (bloom_filter) |
| `auth_attempts` | `MergeTree` | `(kind, key_sha256, at)` |  | `toDateTime(at) + toIntervalDay(1)` |  |
| `blacklist` | `ReplacingMergeTree(created_at)` | `(org, namespace, pattern_id)` |  |  |  |
| `blacklist_version` | `ReplacingMergeTree(updated_at)` | `singleton` |  |  |  |
| `blob_uploads` | `MergeTree` | `(org, user_id, namespace, sha256)` |  |  |  |
| `carry_acks` | `ReplacingMergeTree(at)` | `process` |  | `toDateTime(at) + toIntervalDay(1)` |  |
| `categories` | `ReplacingMergeTree(version)` | `(org, namespace, name)` |  |  |  |
| `chunk_supersessions` | `ReplacingMergeTree(created_at)` | `(org, namespace, embedding_model, source_uri, doc_version)` |  |  |  |
| `chunk_term_freq` | `AggregatingMergeTree` | `(org, namespace, term)` | `toYYYYMM(df_added_at)` |  |  |
| `chunks` | `MergeTree` | `(org, namespace, embedding_model, source_uri, chunk_idx)` | `toYYYYMM(created_at)` |  | 5 skip index (bloom_filter, text) |
| `code_answers` | `ReplacingMergeTree(version)` | `(org, namespace, embedding_model, question_id)` |  |  |  |
| `code_backport_gaps` | `ReplacingMergeTree(version)` | `(org, code_namespace, branch_namespace, pr_number)` |  |  |  |
| `code_call_sites_unresolved` | `ReplacingMergeTree(version)` | `(org, namespace, callee_name, path, line, from_symbol_id)` |  |  | 1 skip index (bloom_filter); 1 projection |
| `code_caller_guards` | `ReplacingMergeTree(version)` | `(org, namespace, from_symbol_id, path)` |  |  | 1 skip index (bloom_filter) |
| `code_chunks` | `ReplacingMergeTree(version)` | `(org, namespace, embedding_model, path, chunk_id)` | `(org, namespace)` |  | 4 skip index (bloom_filter, text) |
| `code_chunks_prefix` | `ReplacingMergeTree(version)` | `(org, namespace, embedding_model, path, chunk_id)` |  |  |  |
| `code_context_packs` | `ReplacingMergeTree(version)` | `(org, namespace, pr_number)` |  |  |  |
| `code_coverage_files` | `ReplacingMergeTree(version)` | `(org, namespace, path)` |  |  |  |
| `code_coverage_watches` | `ReplacingMergeTree(version)` | `(org, namespace, commit_sha, inbox)` |  |  |  |
| `code_doc_stats` | `ReplacingMergeTree(version)` | `(org, namespace, chunk_id)` |  |  |  |
| `code_edges` | `ReplacingMergeTree(version)` | `(org, namespace, tier, to_symbol_id, edge_kind, from_symbol_id, path, line)` |  |  | 2 skip index (bloom_filter); 2 projection |
| `code_files` | `ReplacingMergeTree(version)` | `(org, namespace, path)` |  |  |  |
| `code_gh_items` | `ReplacingMergeTree(version)` | `(org, namespace, number)` |  |  |  |
| `code_index_state` | `ReplacingMergeTree(version)` | `(org, namespace, state_key)` |  |  |  |
| `code_pr_history` | `ReplacingMergeTree(version)` | `(org, namespace, pr_number)` |  |  | 3 skip index (bloom_filter, minmax) |
| `code_pr_reviews` | `ReplacingMergeTree(version)` | `(org, namespace, path, pr_number, comment_id)` |  |  |  |
| `code_repos` | `ReplacingMergeTree(version)` | `(org, namespace)` |  |  |  |
| `code_review_briefs` | `ReplacingMergeTree(version)` | `(org, namespace, pr_number, merge_sha)` |  |  |  |
| `code_scip_batches` | `ReplacingMergeTree(version)` | `(org, namespace, batch_id)` | `toYYYYMM(created_at)` | `created_at + toIntervalDay(30)` |  |
| `code_scip_parts` | `MergeTree` | `(org, namespace, batch_id, seq)` | `toYYYYMM(created_at)` | `created_at + toIntervalDay(7)` |  |
| `code_search_memo` | `ReplacingMergeTree(created_at)` | `(org, namespace, key)` |  | `created_at + toIntervalMinute(60)` |  |
| `code_search_outcome` | `MergeTree` | `(org, namespace, trace_run_id, ts)` | `toYYYYMM(ts)` | `ts + toIntervalDay(180)` |  |
| `code_search_trace` | `MergeTree` | `(org, namespace, ts)` | `toYYYYMM(ts)` | `ts + toIntervalDay(180)` |  |
| `code_summaries` | `ReplacingMergeTree(version)` | `(org, namespace, embedding_model, scope_type, scope_path)` |  |  |  |
| `code_symbol_ids` | `ReplacingMergeTree` | `(org, namespace, symbol_id, tier, qualified_name)` |  |  |  |
| `code_symbol_names` | `ReplacingMergeTree` | `(org, namespace, name, tier, qualified_name, symbol_id)` |  |  |  |
| `code_symbol_stats` | `ReplacingMergeTree(version)` | `(org, namespace, symbol_id)` |  |  |  |
| `code_symbols` | `ReplacingMergeTree(version)` | `(org, namespace, tier, qualified_name, symbol_id)` |  |  | 4 skip index (bloom_filter, ngrambf_v1) |
| `code_term_df` | `MergeTree` | `(org, namespace, term)` |  |  |  |
| `code_term_freq` | `AggregatingMergeTree` | `(org, namespace, term)` |  |  |  |
| `code_test_facets` | `ReplacingMergeTree(version)` | `(org, namespace, path, facet_kind, name, ordinal)` |  |  | 2 skip index (bloom_filter, set) |
| `code_test_map` | `ReplacingMergeTree(version)` | `(org, namespace, src_path, method, test_path)` |  |  | 1 skip index (bloom_filter) |
| `code_unit_risk` | `ReplacingMergeTree(version)` | `(org, namespace, unit_id)` |  |  |  |
| `code_unit_signal` | `SummingMergeTree((positive, negative))` | `(org, namespace, unit_id)` |  |  |  |
| `collections` | `ReplacingMergeTree(version)` | `(org, namespace)` |  |  |  |
| `conflict_allowlist` | `ReplacingMergeTree(allowed_at)` | `(org, namespace, memory_key_a, memory_key_b)` |  |  |  |
| `consolidation_runs` | `MergeTree` | `(org, namespace, ran_at, run_id)` | `toYYYYMM(ran_at)` | `ran_at + toIntervalDay(180)` |  |
| `context_blobs` | `ReplacingMergeTree(version)` | `(org, namespace, blob_key)` | `toYYYYMM(expires_at)` | `expires_at + toIntervalDay(1)` |  |
| `conversation_embeddings` | `ReplacingMergeTree(ts)` | `(org, namespace, embedding_model, thread_id, msg_id)` | `toYYYYMM(ts)` |  |  |
| `conversation_threads` | `ReplacingMergeTree(updated_at)` | `(org, namespace, thread_id)` | `toYYYYMM(created_at)` |  |  |
| `conversations` | `MergeTree` | `(org, namespace, thread_id, ts, msg_id)` | `toYYYYMM(ts)` |  |  |
| `dataset_rows` | `MergeTree` | `(org, namespace, dataset_id, snapshot_id, row_id)` |  |  |  |
| `datasets` | `ReplacingMergeTree(version)` | `(org, namespace, source_uri, dataset_id, doc_version)` |  |  |  |
| `delete_version_checks` | `ReplacingMergeTree(updated_at)` | `(org, namespace, agent, user_id, session_id, memory_key, tombstone)` |  | `created_at + toIntervalDay(30)` |  |
| `deployment_config` | `ReplacingMergeTree(version)` | `name` |  |  |  |
| `derived_facts` | `MergeTree` | `(org, namespace, subject, predicate, category)` | `toYYYYMM(created_at)` |  |  |
| `document_blobs` | `ReplacingMergeTree(created_at)` | `(org, namespace, sha256)` |  |  |  |
| `documents` | `ReplacingMergeTree(version)` | `(org, namespace, source_uri)` | `toYYYYMM(ingested_at)` |  | 1 skip index (bloom_filter) |
| `embedder_calibration` | `ReplacingMergeTree(version)` | `embedder_model` |  |  |  |
| `embedding_cache` | `ReplacingMergeTree(created_at)` | `(model, key)` |  | `created_at + toIntervalDay(30)` |  |
| `entity_canonical_map` | `ReplacingMergeTree(version)` | `(org, namespace, mention_text)` | `org` |  | 1 skip index (bloom_filter) |
| `entity_communities` | `ReplacingMergeTree(version)` | `(org, namespace, level, entity)` |  |  |  |
| `entity_degree` | `SummingMergeTree` | `(org, namespace, entity)` |  |  |  |
| `entity_edge_memories` | `ReplacingMergeTree(seen_at)` | `(org, namespace, entity_a, entity_b, memory_key)` | `toYYYYMM(seen_at)` |  |  |
| `entity_edge_rows` | `ReplacingMergeTree` | `(org, namespace, memory_id, user_id, content_sha256, entity_a, entity_b)` |  |  | 1 skip index (bloom_filter) |
| `entity_edges` | `MergeTree` | `(org, namespace, entity_a, entity_b)` | `toYYYYMM(last_seen)` |  | 3 skip index (minmax) |
| `entity_summaries` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, entity, embedding_model)` | `toYYYYMM(created_at)` |  |  |
| `entity_triple_memories` | `ReplacingMergeTree(seen_at)` | `(org, namespace, subject, relation, object, memory_key)` | `toYYYYMM(seen_at)` |  |  |
| `entity_triple_rows` | `ReplacingMergeTree` | `(org, namespace, memory_id, user_id, content_sha256, subject, relation, object, inferred)` |  |  | 1 skip index (bloom_filter) |
| `entity_triples` | `MergeTree` | `(org, namespace, subject, relation, object)` | `toYYYYMM(last_seen)` |  | 3 skip index (minmax) |
| `entity_vectors` | `ReplacingMergeTree(created_at)` | `(org, namespace, agent, user_id, entity_text, memory_key)` | `toYYYYMM(created_at)` |  |  |
| `erasure_requests` | `ReplacingMergeTree(version)` | `id` |  | `requested_at + toIntervalDay(90)` |  |
| `erasure_reversals` | `ReplacingMergeTree(version)` | `(org, erasure_id, memory_id)` |  | `restored_at + toIntervalYear(5)` |  |
| `events` | `MergeTree` | `(org, namespace, kind, ts)` | `toYYYYMM(ts)` | `toDateTime(ts) + toIntervalDay(30)` |  |
| `extraction_policies` | `ReplacingMergeTree(version)` | `(org, namespace)` |  |  |  |
| `frontend_assets` | `ReplacingMergeTree(published_at)` | `path` |  | `published_at + toIntervalDay(30)` |  |
| `grant_claims` | `KeeperMap('/loom/grant_claims')` |  |  |  |  |
| `graph_projections` | `KeeperMap('/loom/graph_projections')` |  |  |  |  |
| `idempotency` | `ReplacingMergeTree(created_at)` | `idempotency_key` |  | `expires_at` |  |
| `instances` | `KeeperMap('/loom/instances')` |  |  |  |  |
| `keyring_readers` | `MergeTree` | `(seen_at, process)` |  | `toDateTime(seen_at) + toIntervalDay(1)` |  |
| `knowledge_ingest_cancels` | `MergeTree` | `(org, namespace, ingestion_id)` |  | `toDateTime(requested_at) + toIntervalDay(30)` |  |
| `knowledge_ingestions` | `ReplacingMergeTree(version)` | `(org, namespace, ingestion_id)` |  | `toDateTime(updated_at) + toIntervalDay(30)` |  |
| `knowledge_sources` | `ReplacingMergeTree(version)` | `(org, namespace, name)` |  |  |  |
| `knowledge_uploads` | `MergeTree` | `(org, namespace, upload_id)` |  | `toDateTime(expires_at) + toIntervalDay(7)` |  |
| `learnings_queue` | `ReplacingMergeTree(version)` | `(org, namespace, candidate_id)` | `toYYYYMM(proposed_at)` |  |  |
| `learnings_supporters` | `MergeTree` | `(org, namespace, candidate_id, user_id, session_id)` |  |  |  |
| `llm_response_cache` | `ReplacingMergeTree(created_at)` | `(model, key)` |  | `created_at + toIntervalDay(7)` |  |
| `machine_identities` | `ReplacingMergeTree(version)` | `name` |  |  |  |
| `memory` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, session_id, memory_key)` | `toYYYYMM(created_at)` |  | 11 skip index (bloom_filter, minmax, ngrambf_v1, set) |
| `memory_ask_precedents` | `ReplacingMergeTree(version)` | `(org, namespace, precedent_key)` |  | `created_at + toIntervalDay(30)` |  |
| `memory_belief_sources` | `ReplacingMergeTree(created_at)` | `(org, namespace, memory_key, evidence_id)` |  |  |  |
| `memory_categories` | `ReplacingMergeTree(created_at)` | `(org, namespace, memory_key, category_id)` |  |  |  |
| `memory_chunk_sources` | `ReplacingMergeTree(created_at)` | `(org, namespace, session_id, chunk_index, memory_key, evidence_id)` | `toYYYYMM(created_at)` |  |  |
| `memory_chunks` | `ReplacingMergeTree(created_at)` | `(org, namespace, session_id, chunk_index, content_sha256)` | `toYYYYMM(created_at)` |  |  |
| `memory_code_link_queue` | `MergeTree` | `queued_at` | `toDate(queued_at)` | `toDateTime(queued_at) + toIntervalDay(7)` |  |
| `memory_code_links` | `ReplacingMergeTree(version)` | `(org, code_namespace, path, qualified_name, namespace, memory_id)` |  |  | 4 skip index (bloom_filter) |
| `memory_doc_stats` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key)` |  |  |  |
| `memory_drops` | `MergeTree` | `(org, namespace, stage, reason, ts)` |  | `ts + toIntervalDay(30)` |  |
| `memory_embeddings` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key)` | `toYYYYMM(created_at)` |  | 11 skip index (bloom_filter, minmax, ngrambf_v1, set) |
| `memory_evidence` | `ReplacingMergeTree(created_at)` | `(org, namespace, agent, user_id, session_id, observed_at, evidence_id)` | `toYYYYMM(created_at)` |  |  |
| `memory_exposure_breadth_hourly` | `AggregatingMergeTree` | `(org, namespace, actor, agent, hour)` | `toYYYYMM(hour)` | `hour + toIntervalDay(180)` |  |
| `memory_exposures` | `MergeTree` | `(org, namespace, actor, ts)` | `toYYYYMM(ts)` | `ts + toIntervalDay(180)` |  |
| `memory_extraction_queue` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, session_id, queue_id)` | `toYYYYMM(enqueued_at)` | `enqueued_at + toIntervalDay(30)` |  |
| `memory_projection_failures` | `MergeTree` | `(org, namespace, projection, created_at, failure_id)` |  | `coalesce(resolved_at, created_at) + toIntervalDay(30) WHERE status = 'resolved'` |  |
| `memory_record_conflicts` | `ReplacingMergeTree(version)` | `(org, namespace, schema_name, memory_key, field_name, incoming_record_id)` |  |  |  |
| `memory_record_fields` | `ReplacingMergeTree(version)` | `(org, namespace, schema_name, field_name, record_id)` |  |  | 1 skip index (bloom_filter) |
| `memory_record_versions` | `ReplacingMergeTree` | `(org, namespace, schema_name, memory_key, record_id, version)` | `toYYYYMM(created_at)` |  | 1 skip index (bloom_filter) |
| `memory_records` | `ReplacingMergeTree(version)` | `(org, namespace, schema_name, memory_key, record_id)` |  |  | 1 skip index (bloom_filter) |
| `memory_reinforcement` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, memory_key)` |  |  |  |
| `memory_schema_fields` | `ReplacingMergeTree(version)` | `(org, namespace, schema_id, field_name)` |  |  |  |
| `memory_schemas` | `ReplacingMergeTree(version)` | `(org, namespace, name)` |  |  |  |
| `memory_served_daily` | `SummingMergeTree` | `(org, namespace, memory_id, agent, day)` | `toYYYYMM(day)` |  |  |
| `memory_term_freq` | `AggregatingMergeTree` | `(org, namespace, term)` | `toYYYYMM(df_added_at)` |  |  |
| `memory_usage_events` | `MergeTree` | `(org, namespace, memory_id, ts)` | `toYYYYMM(ts)` |  |  |
| `memory_usage_totals` | `SummingMergeTree` | `(org, namespace, memory_id)` |  |  |  |
| `memory_versions` | `ReplacingMergeTree` | `(org, namespace, agent, user_id, session_id, memory_key, version)` | `toYYYYMM(created_at)` |  | 1 skip index (bloom_filter) |
| `memory_webhook_attempts` | `MergeTree` | `(event_id, version)` | `toDate(recorded_at)` | `toDateTime(recorded_at) + toIntervalDay(3)` |  |
| `memory_webhook_outbox` | `MergeTree` | `(org, namespace, webhook_id, occurred_at, event_id)` | `toDate(occurred_at)` | `toDateTime(occurred_at) + toIntervalDay(3)` |  |
| `memory_webhooks` | `ReplacingMergeTree(version)` | `(org, namespace, webhook_id)` |  |  |  |
| `namespace_calibration_result` | `ReplacingMergeTree(version)` | `(org, namespace)` |  |  |  |
| `namespace_grants` | `ReplacingMergeTree(version)` | `(org, namespace, subject)` | `org` |  |  |
| `namespace_guides` | `MergeTree` | `(org, namespace, revision)` |  |  |  |
| `namespace_ontology` | `ReplacingMergeTree(version)` | `(org, namespace, kind, name)` | `org` |  |  |
| `namespace_policies` | `ReplacingMergeTree(version)` | `(org, namespace, policy_id)` |  |  |  |
| `namespace_snapshots` | `MergeTree` | `(org, source_namespace, created_at)` | `org` |  |  |
| `namespace_types` | `ReplacingMergeTree(updated_at)` | `(org, namespace, memory_type)` | `org` |  |  |
| `namespace_visits` | `ReplacingMergeTree(version)` | `(org, subject, namespace)` | `org` |  |  |
| `namespace_write_keys` | `KeeperMap('/loom/namespace_write_keys')` |  |  |  |  |
| `namespace_write_marks` | `AggregatingMergeTree` | `(org, namespace, plane)` |  |  |  |
| `namespaces` | `ReplacingMergeTree(updated_at)` | `(org, namespace)` | `org` |  |  |
| `oauth_claims` | `KeeperMap('/loom/oauth_claims')` |  |  |  |  |
| `oauth_signin_state` | `KeeperMap('/loom/oauth_signin_state', 500000)` |  |  |  |  |
| `oauth_state` | `ReplacingMergeTree(version)` | `(record_type, id)` | `record_type` | `created_at + toIntervalDay(90)` |  |
| `operator_edges` | `ReplacingMergeTree(created_at)` | `(org, namespace, from_key, to_key, relation)` | `toYYYYMM(created_at)` |  | 1 skip index (bloom_filter) |
| `org_model_calls` | `ReplacingMergeTree(version)` | `org` |  |  |  |
| `policy_decisions` | `MergeTree` | `(org, namespace, ts)` | `toYYYYMM(ts)` | `toDateTime(ts) + toIntervalDay(90)` |  |
| `principal_groups` | `ReplacingMergeTree(version)` | `(org, group_id)` |  |  |  |
| `procedure_runs` | `MergeTree` | `(org, namespace, memory_key, ts)` | `toYYYYMM(ts)` |  |  |
| `profile_schemas` | `ReplacingMergeTree(version)` | `(org, namespace)` |  |  |  |
| `provider_configuration` | `ReplacingMergeTree(version)` | `config_id` |  |  |  |
| `qa_fail_hours` | `ReplacingMergeTree(version)` | `(org, qa_namespace, hour)` |  |  |  |
| `qa_item_embeddings` | `ReplacingMergeTree(version)` | `(org, namespace, kind, number)` |  |  |  |
| `qa_link_candidates` | `ReplacingMergeTree(version)` | `(org, qa_namespace, issue_number, pr_number)` |  |  |  |
| `qa_rollup_days` | `ReplacingMergeTree(version)` | `(org, qa_namespace, day)` |  |  |  |
| `qa_sources` | `ReplacingMergeTree(version)` | `(org, name)` |  |  |  |
| `qa_test_daily` | `ReplacingMergeTree(version)` | `(org, qa_namespace, test_name, day)` |  |  |  |
| `query_nodes` | `AggregatingMergeTree` | `(org, namespace, query_hash)` |  |  |  |
| `revoked_tokens` | `ReplacingMergeTree(version)` | `jti` |  | `toDateTime(expires_at) + toIntervalDay(10)` |  |
| `runtime_overrides` | `ReplacingMergeTree(version)` | `(kind, key)` |  |  |  |
| `scim_users` | `ReplacingMergeTree(version)` | `id` |  |  |  |
| `search_trace_log` | `MergeTree` | `(org, namespace, ts)` | `toYYYYMM(ts)` | `ts + toIntervalDay(180)` |  |
| `service_accounts` | `ReplacingMergeTree(version)` | `(org, name)` | `org` |  |  |
| `session_brief_revisions` | `MergeTree` | `(org, namespace, brief_key, revision, version)` |  |  |  |
| `session_briefs` | `ReplacingMergeTree(version)` | `(org, namespace, brief_key)` |  |  |  |
| `session_outcomes_by_user` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, session_id)` |  |  |  |
| `session_summaries` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, session_id, embedding_model)` | `toYYYYMM(created_at)` |  |  |
| `shared` | `ReplacingMergeTree(version)` | `(org, namespace, memory_key)` | `(org, namespace)` |  |  |
| `signing_keys` | `ReplacingMergeTree(version)` | `kid` |  |  |  |
| `sso_links` | `ReplacingMergeTree(version)` | `(email, org, namespace)` |  |  |  |
| `storage_authority` | `ReplacingMergeTree(epoch)` | `tuple()` |  |  |  |
| `subject_last_seen` | `AggregatingMergeTree` | `(org, subject)` |  |  |  |
| `task_claims` | `MergeTree` | `(task_id, fence, claim_ts, claim_id)` |  |  |  |
| `task_events` | `MergeTree` | `(org, inbox, seq)` | `toYYYYMM(ts)` |  | 1 skip index (bloom_filter) |
| `task_inboxes` | `ReplacingMergeTree(version)` | `(org, inbox)` |  |  |  |
| `task_leases` | `KeeperMap('/loom/task_leases')` |  |  |  |  |
| `task_poster_policies` | `ReplacingMergeTree(version)` | `(org, inbox, poster, kind)` |  |  |  |
| `task_webhook_cursor` | `ReplacingMergeTree(version)` | `(org, inbox)` |  |  |  |
| `tasks` | `ReplacingMergeTree(version)` | `(org, inbox, task_id)` |  |  |  |
| `usage_acted_credits` | `ReplacingMergeTree(credited_at)` | `pair_sha256` |  | `first_write + toIntervalDay(3)` |  |
| `user_state` | `ReplacingMergeTree(version)` | `(org, subject)` | `org` |  |  |
| `worker_status` | `ReplacingMergeTree(version)` | `(worker, instance_label, pass_started)` |  | `toDateTime(pass_started) + toIntervalDay(30)` |  |
| `working_memory` | `ReplacingMergeTree(version)` | `(org, namespace, agent, user_id, session_id, run_id, block_key)` | `(org, namespace)` |  |  |
| `loom_telemetry.sli_minute` | `AggregatingMergeTree` | `(slo_class, minute, operation, transport, traffic_class, org)` | `toYYYYMM(ingested_hwm)` | `minute + toIntervalDay(400)` |  |

#### Materialized views

| View | Writes to | Reads from | Runs |
| --- | --- | --- | --- |
| `agent_cost_rollup_refresh` | `agent_cost_rollup_daily` | `agent_events` | refreshable, every 1 minute |
| `chunk_context_term_freq_mv` | `chunk_term_freq` | `chunks` | on insert |
| `chunk_term_freq_mv` | `chunk_term_freq` | `chunks` | on insert |
| `code_doc_stats_mv` | `code_doc_stats` | `code_chunks` | on insert |
| `code_symbol_ids_mv` | `code_symbol_ids` | `code_symbols` | on insert |
| `code_symbol_names_mv` | `code_symbol_names` | `code_symbols` | on insert |
| `code_term_df_refresh` | `code_term_df` | `code_doc_stats`, `code_term_freq` | refreshable, every 1 hour |
| `code_term_freq_mv` | `code_term_freq` | `code_chunks` | on insert |
| `entity_degree_mv` | `entity_degree` | `entity_edges` | on insert |
| `memory_code_link_queue_mv` | `memory_code_link_queue` | `memory` | on insert |
| `memory_doc_stats_refresh` | `memory_doc_stats` | `memory_embeddings` | refreshable, every 1 minute |
| `memory_exposure_breadth_hourly_refresh` | `memory_exposure_breadth_hourly` | `memory_exposures` | refreshable, every 1 minute |
| `memory_record_versions_mv` | `memory_record_versions` | `memory_records` | on insert |
| `memory_term_freq_refresh` | `memory_term_freq` | `memory_embeddings` | refreshable, every 1 minute |
| `memory_usage_totals_mv` | `memory_usage_totals` | `memory_usage_events` | on insert |
| `memory_versions_mv` | `memory_versions` | `memory` | on insert |
| `session_brief_revisions_mv` | `session_brief_revisions` | `session_briefs` | on insert |
| `subject_last_seen_refresh` | `subject_last_seen` | `agent_events` | refreshable, every 1 minute |
| `loom_telemetry.sli_minute_refresh` | `loom_telemetry.sli_minute` | `agent_events` | refreshable, every 1 minute |

<!-- prettier-ignore-end -->
<!-- END GENERATED: schema -->

## Related

- [ClickHouse](../architecture/clickhouse.md): table families and their owners, engines,
  indexes and query patterns.
- [Migrations](../development/migrations.md): writing a migration.
- [Adding a table](../development/adding-a-table.md).
- [Upgrades](../operations/upgrades.md): how migrations run on a deployment.
