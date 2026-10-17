-- requires-setting: memory_temporal_enabled
-- Applied only while LOOM_MEMORY_TEMPORAL_ENABLED is on (dormant otherwise: neither
-- applied nor pending); turned on later, the next migrate applies it.
--
-- Bi-temporal memory: "which assertions applied at valid time V, according to
-- what loom had recorded at knowledge time K".
--
-- memory_temporal_commits is the authority. One row is one complete change to
-- one logical memory: the assertion ids it retires and the full assertions it
-- adds. The publisher allocates commit_seq per namespace under one lock, so the
-- sequence is the publication order and recorded_at rises with it. A retry
-- under the same idempotency key and body is answered with the commit it made.
-- Rows are never updated, except
-- that memory.redact rewrites a redacted value in place: payload_sha256 stays the
-- hash at commit time.
CREATE TABLE IF NOT EXISTS loom.memory_temporal_commits
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `scope` LowCardinality(String) DEFAULT 'private',
    `agent` LowCardinality(String),
    `user_id` String,
    `session_id` String,
    `memory_key` String,
    `memory_id` UUID,
    `commit_seq` UInt64,
    `commit_id` UUID,
    `parent_commit_id` Nullable(UUID),
    `publisher_id` String,
    `recorded_at` DateTime64(6, 'UTC'),
    `request_id` String DEFAULT '',
    `idempotency_key` String DEFAULT '',
    -- The request body hash the idempotency key was sent with: a retry with the
    -- same one replays this commit; another body under the key is refused.
    `request_sha256` String DEFAULT '',
    -- Which entry of its request this commit is the outcome of (memory.bulk_set's
    -- entry index), so a retry answers each entry as it was answered; -1 otherwise.
    `request_slot` Int32 DEFAULT -1,
    -- On a request's first commit: how it answered each entry that made no commit (a
    -- result or none, the lists naming it), JSON, so a retry answers them the same.
    `request_outcomes` String DEFAULT '',
    `operation` LowCardinality(String),
    `change_kind` LowCardinality(String),
    `payload_schema` UInt16,
    `payload_sha256` FixedString(64),
    `actor` String DEFAULT '',
    `source_class` LowCardinality(String) DEFAULT '',
    `source_resource_id` String DEFAULT '',
    `erasure_generation` UInt64 DEFAULT 0,
    `retired_assertion_ids` Array(UUID),
    `assertions` String,
    -- Stamped by the server on insert, never by the publisher: which of two
    -- commits at one sequence landed first, the one a caller may have been told of.
    `inserted_at` DateTime64(6, 'UTC') DEFAULT now64(6),
    -- The latest expires_at among the assertions it added or retired: retention
    -- keeps the commit at least that long, so it outlives every assertion it bore
    -- and a retraction is kept as long as what it retracts.
    `retain_until` Nullable(DateTime64(6, 'UTC')),
    -- The loom.memory version its row was written at: the next row of the key is
    -- stamped above it, so the current row and the journal agree on the latest.
    `row_version` UInt64 DEFAULT 0,
    -- A restatement's: the served assertion its row shows (it adds none of its own).
    `restated_assertion_id` Nullable(UUID) DEFAULT NULL,
    INDEX key_bf memory_key TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idem_bf idempotency_key TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX memory_id_bf memory_id TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX recorded_minmax recorded_at TYPE minmax GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(recorded_at)
ORDER BY (org, namespace, commit_seq, commit_id)
-- A retried insert of the same batch (one dedup token) lands once on a single node too.
SETTINGS index_granularity = 1024, non_replicated_deduplication_window = 1000;

-- Every assertion the journal has added, with the sequence that added it
-- (born_seq) and the one that retired it (retired_seq, NULL while it is
-- served). A read at cutoff C admits born_seq <= C AND (retired_seq IS NULL OR
-- C < retired_seq), so later retirements leave earlier cutoffs intact. A
-- retirement re-emits the whole row with a higher projected_seq.
CREATE TABLE IF NOT EXISTS loom.memory_temporal_assertions
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `scope` LowCardinality(String) DEFAULT 'private',
    `agent` LowCardinality(String),
    `user_id` String,
    `session_id` String,
    `memory_key` String,
    `memory_id` UUID,
    `assertion_id` UUID,
    `parent_assertion_id` Nullable(UUID),
    `origin_assertion_id` UUID,
    `born_seq` UInt64,
    `born_at` DateTime64(6, 'UTC'),
    `retired_seq` Nullable(UInt64),
    `retired_at` Nullable(DateTime64(6, 'UTC')),
    `projected_seq` UInt64,
    `source_version` UInt64 DEFAULT 0,
    `memory_type` LowCardinality(String),
    `value` String,
    `value_sha256` FixedString(64),
    `embed_input_sha256` FixedString(64),
    `temporal_kind` LowCardinality(String) DEFAULT 'state',
    `validity_basis` LowCardinality(String) DEFAULT 'unknown',
    `valid_from` Nullable(DateTime64(6, 'UTC')),
    `valid_to` Nullable(DateTime64(6, 'UTC')),
    `valid_text` String DEFAULT '',
    `valid_precision` LowCardinality(String) DEFAULT '',
    `valid_tz` LowCardinality(String) DEFAULT '',
    `modality` LowCardinality(String) DEFAULT 'asserted',
    `status` LowCardinality(String) DEFAULT 'active',
    `confidence` Float32 DEFAULT 1.,
    `expires_at` Nullable(DateTime64(6, 'UTC')),
    `pinned` UInt8 DEFAULT 0,
    `lock_mode` LowCardinality(String) DEFAULT '',
    `lock_until` Nullable(DateTime64(6, 'UTC')) DEFAULT NULL,
    `lock_reason` String DEFAULT '',
    `locked_by` String DEFAULT '',
    `locked_at` Nullable(DateTime64(6, 'UTC')) DEFAULT NULL,
    `verified_by` String DEFAULT '',
    `verified_at` Nullable(DateTime64(6, 'UTC')),
    `verified_sha256` String DEFAULT '',
    `grounding` LowCardinality(String) DEFAULT '',
    `source_class` LowCardinality(String) DEFAULT '',
    `actor` String DEFAULT '',
    `sensitivity` LowCardinality(String) DEFAULT '',
    `qualifiers` Map(String, String),
    `tags` Array(String),
    `entities` Array(String),
    `evidence_ids` Array(String),
    `derived_from` Array(String),
    INDEX memory_id_bf memory_id TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX born_minmax born_seq TYPE minmax GRANULARITY 1,
    INDEX retired_minmax retired_seq TYPE minmax GRANULARITY 1,
    INDEX valid_from_minmax valid_from TYPE minmax GRANULARITY 4,
    INDEX valid_to_minmax valid_to TYPE minmax GRANULARITY 4
)
ENGINE = ReplacingMergeTree(projected_seq)
PARTITION BY (org)
ORDER BY (org, namespace, agent, user_id, session_id, memory_key, assertion_id)
SETTINGS index_granularity = 256, non_replicated_deduplication_window = 1000;

-- The highest journal sequence a projection has applied with no gap below it.
-- A reader whose cutoff is above it reduces the journal tail itself. The
-- `published` row is the last sequence the publisher allocated: erasing or
-- expiring the newest commits never hands their sequences out again.
CREATE TABLE IF NOT EXISTS loom.memory_temporal_watermarks
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `projection` LowCardinality(String),
    `applied_seq` UInt64,
    -- The `published` row's recording time: the latest the publisher stamped.
    `recorded_at` Nullable(DateTime64(6, 'UTC')) DEFAULT NULL,
    `updated_at` DateTime64(6, 'UTC') DEFAULT now64(6)
)
ENGINE = ReplacingMergeTree(applied_seq)
ORDER BY (org, namespace, projection)
SETTINGS non_replicated_deduplication_window = 1000;

-- Vectors for historical search, keyed by what was embedded: the model and the
-- sha256 of the exact embed text (value and key axis), so a revision whose text
-- did not change reuses its vector and two keys never share one by accident.
CREATE TABLE IF NOT EXISTS loom.memory_temporal_vectors
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `embedding_model` LowCardinality(String),
    `embed_input_sha256` FixedString(64),
    `embedding_dim` UInt16,
    `embedding` Array(BFloat16) CODEC(NONE),
    `created_at` DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (org, namespace, embedding_model, embed_input_sha256)
SETTINGS index_granularity = 256, non_replicated_deduplication_window = 1000;

-- Shared memory history: every promotion, supersession and demotion row,
-- kept after the ReplacingMergeTree merges the head.
CREATE TABLE IF NOT EXISTS loom.shared_versions
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `memory_key` String,
    `value` String,
    `memory_type` LowCardinality(String),
    `confidence` Float32,
    `promoted_from` Array(String),
    `promotion_event` UUID,
    `promoted_from_candidate_id` Nullable(UUID) DEFAULT NULL,
    `promoted_by` String,
    `rationale` String DEFAULT '',
    `promoted_at` DateTime DEFAULT now(),
    `reversible_until` DateTime,
    `superseded_by` Nullable(String),
    `tombstoned_at` Nullable(DateTime),
    `version` UInt64
)
ENGINE = ReplacingMergeTree
PARTITION BY (org, namespace)
ORDER BY (org, namespace, memory_key, version)
SETTINGS non_replicated_deduplication_window = 1000;

CREATE MATERIALIZED VIEW IF NOT EXISTS loom.shared_versions_mv TO loom.shared_versions AS
SELECT org, namespace, memory_key, value, memory_type, confidence, promoted_from,
       promotion_event, promoted_from_candidate_id, promoted_by, rationale, promoted_at,
       reversible_until, superseded_by, tombstoned_at, version
FROM loom.shared;

-- Graph rows record when loom learned them (recorded_at) and when an interval
-- close was recorded (closed_at: the close is an in-place update of valid_to).
-- A read known at K admits recorded_at <= K and treats a row closed after K as
-- still open. A row written before this migration reads the epoch (recording
-- time unknown) and is admitted at every K.
ALTER TABLE loom.entity_edges ADD COLUMN IF NOT EXISTS `recorded_at` DateTime DEFAULT toDateTime(0);
ALTER TABLE loom.entity_triples ADD COLUMN IF NOT EXISTS `recorded_at` DateTime DEFAULT toDateTime(0);
ALTER TABLE loom.entity_edges ADD COLUMN IF NOT EXISTS `closed_at` Nullable(DateTime) DEFAULT NULL;
ALTER TABLE loom.entity_triples ADD COLUMN IF NOT EXISTS `closed_at` Nullable(DateTime) DEFAULT NULL;

-- Exposure receipts: the exact revisions a read returned and the cutoff it read at.
ALTER TABLE loom.memory_exposures ADD COLUMN IF NOT EXISTS `returned_ids` Array(String) DEFAULT [];
ALTER TABLE loom.memory_exposures ADD COLUMN IF NOT EXISTS `returned_sha256` Array(String) DEFAULT [];
ALTER TABLE loom.memory_exposures ADD COLUMN IF NOT EXISTS `cutoff_seq` UInt64 DEFAULT 0;
ALTER TABLE loom.memory_exposures ADD COLUMN IF NOT EXISTS `valid_as_of` Nullable(DateTime64(6, 'UTC')) DEFAULT NULL;
ALTER TABLE loom.memory_exposures ADD COLUMN IF NOT EXISTS `known_as_of` Nullable(DateTime64(6, 'UTC')) DEFAULT NULL;

-- The version this build wrote its row at. A build that does not journal (the one
-- serving beside this one during a rolling upgrade) carries the column forward
-- unchanged, so a row whose version differs is a write the journal has not seen:
-- the publisher and the reconciler journal it (adopt it) instead of overwriting it.
ALTER TABLE loom.memory ADD COLUMN IF NOT EXISTS `journal_version` UInt64 DEFAULT 0;
ALTER TABLE loom.memory_versions ADD COLUMN IF NOT EXISTS `journal_version` UInt64 DEFAULT 0;
ALTER TABLE loom.memory_versions_mv MODIFY QUERY
SELECT
    org,
    namespace,
    agent,
    user_id,
    session_id,
    memory_key,
    memory_type,
    value,
    value_json,
    confidence,
    source_trace,
    version,
    tombstoned_at,
    created_at,
    expires_at,
    pinned,
    run_id,
    entities,
    memory_id,
    redacted_at,
    redaction_reason,
    status,
    conflict_with,
    duplicate_of,
    evidence_count,
    recall_count,
    last_recalled_at,
    cross_promoted_from_memory_id,
    cross_promoted_from_namespace,
    tags,
    files,
    concepts,
    superseded_by,
    rejection_reason,
    temporal_anchor,
    temporal_confidence,
    updated_at,
    valid_at,
    valid_to,
    invalid_at,
    char_limit,
    reinforcement_count,
    last_reinforced_at,
    mentioned_at,
    source_resource_id,
    content_sha256,
    verified_by,
    verified_at,
    verified_sha256,
    grounding,
    evidence,
    sensitivity,
    actor,
    source_class,
    lock_mode,
    lock_reason,
    lock_until,
    locked_by,
    locked_at,
    on_expire,
    expire_reason,
    journal_version
FROM loom.memory;
