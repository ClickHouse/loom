-- Server-side ingestion of uploaded files (knowledge.upload, knowledge.ingest).
--
-- loom.knowledge_uploads: one row per verified upload, the scoped identity a
-- caller names in knowledge.ingest. The bytes are an original in
-- loom.document_blobs; the row holds them from the sweep until `expires_at`, and
-- goes a week after that.
CREATE TABLE IF NOT EXISTS loom.knowledge_uploads
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `upload_id` UUID,
    `sha256` FixedString(64),
    `size` UInt64,
    `filename` String DEFAULT '',
    `content_type` LowCardinality(String) DEFAULT '',
    `format` LowCardinality(String) DEFAULT '',
    `agent` LowCardinality(String) DEFAULT '-',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `actor` String DEFAULT '',
    `source_class` LowCardinality(String) DEFAULT '',
    `created_at` DateTime64(3) DEFAULT now64(3),
    `expires_at` DateTime64(3)
)
ENGINE = MergeTree
ORDER BY (org, namespace, upload_id)
TTL toDateTime(expires_at) + INTERVAL 7 DAY
SETTINGS non_replicated_deduplication_window = 1000, index_granularity = 1024;

-- loom.knowledge_ingestions: one job per knowledge.ingest, a version row per
-- state change (pending, processing, ready, partial, failed, cancelled), read
-- with FINAL. A worker takes a job under its lease before it works on it. A job
-- row goes 30 days after its last change: the document it published keeps its
-- revision, and knowledge.ingest_status no longer finds the job.
CREATE TABLE IF NOT EXISTS loom.knowledge_ingestions
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `ingestion_id` UUID,
    `source_uri` String,
    `upload_id` UUID,
    `sha256` FixedString(64),
    `filename` String DEFAULT '',
    `content_type` LowCardinality(String) DEFAULT '',
    `title` String DEFAULT '',
    `description` String DEFAULT '',
    `tags` Array(LowCardinality(String)) DEFAULT [],
    `metadata` String DEFAULT '{}',
    `options` String DEFAULT '{}',
    `expected_revision` Int64 DEFAULT -1,
    `allow_partial` Bool DEFAULT false,
    `state` LowCardinality(String),
    `stage` LowCardinality(String) DEFAULT '',
    `attempts` UInt16 DEFAULT 0,
    `next_attempt_at` DateTime64(3) DEFAULT now64(3),
    `error_code` LowCardinality(String) DEFAULT '',
    `error_message` String DEFAULT '',
    `format` LowCardinality(String) DEFAULT '',
    `coverage` String DEFAULT '',
    `warnings` Array(String) DEFAULT [],
    `artifact_sha256` String DEFAULT '',
    `chunks` UInt32 DEFAULT 0,
    `revision` UInt64 DEFAULT 0,
    `doc_version` UInt64 DEFAULT 0,
    `agent` LowCardinality(String) DEFAULT '-',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `actor` String DEFAULT '',
    `source_class` LowCardinality(String) DEFAULT '',
    `created_at` DateTime64(3) DEFAULT now64(3),
    `updated_at` DateTime64(3) DEFAULT now64(3),
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, ingestion_id)
TTL toDateTime(updated_at) + INTERVAL 30 DAY
SETTINGS non_replicated_deduplication_window = 1000, index_granularity = 1024;

-- loom.knowledge_ingest_cancels: one row per knowledge.ingest_cancel. A cancel
-- is its own row, never a version of the job's: a version written from the
-- caller's read would replace whatever the worker saved after that read. The
-- worker reads it before it publishes and when it takes the job.
CREATE TABLE IF NOT EXISTS loom.knowledge_ingest_cancels
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `ingestion_id` UUID,
    `agent` LowCardinality(String) DEFAULT '-',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `actor` String DEFAULT '',
    `requested_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (org, namespace, ingestion_id)
TTL toDateTime(requested_at) + INTERVAL 30 DAY
SETTINGS non_replicated_deduplication_window = 1000, index_granularity = 1024;

-- A document's revision (1, 2, ... per source_uri; 0 on a row no write has
-- numbered), the format it was parsed as, the extraction artifact
-- its chunks were cut from, the parser and its coverage, and the ingestion that
-- published it. Text writes (knowledge.add, upsert) set `revision` only.
ALTER TABLE loom.documents
    ADD COLUMN IF NOT EXISTS `revision` UInt64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS `format` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `artifact_sha256` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `parser` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `coverage` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `ingestion_id` String DEFAULT '';

-- A chunk's exact lookup keys (SQL object names, TOML key paths) and the
-- sha256 of the text its vector was computed from, so an identical input
-- under the same model reuses the stored vector.
ALTER TABLE loom.chunks
    ADD COLUMN IF NOT EXISTS `keys` Array(String) DEFAULT [],
    ADD COLUMN IF NOT EXISTS `embed_input_sha256` String DEFAULT '';

ALTER TABLE loom.chunks
    ADD INDEX IF NOT EXISTS keys_bloom keys TYPE bloom_filter() GRANULARITY 1;

-- The vector-reuse lookup filters on embed_input_sha256 within a namespace.
ALTER TABLE loom.chunks
    ADD INDEX IF NOT EXISTS embed_input_bloom embed_input_sha256 TYPE bloom_filter() GRANULARITY 1;
