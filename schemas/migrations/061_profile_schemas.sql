-- The shape of each user's structured profile in a namespace
-- (`profile_schema.set`, docs/features/memory/working-memory-and-profiles.md).
--
-- One row per change, ReplacingMergeTree(version) keyed on (org, namespace),
-- so the newest wins. schema_json is the admin's JSON Schema as they wrote it,
-- key order kept, and schema_sha256 a hash of its parsed properties, which each
-- built profile row carries as a tag: a profile built under another hash is
-- stale. tombstoned_at clears the schema; the profiles already built stay in
-- loom.memory.
CREATE TABLE IF NOT EXISTS loom.profile_schemas
(
    `org` LowCardinality(String),
    `namespace` String,
    `schema_json` String,
    `schema_sha256` String,
    `updated_by` String DEFAULT '',
    `updated_at` DateTime64(3, 'UTC'),
    `tombstoned_at` Nullable(DateTime64(3, 'UTC')) DEFAULT NULL,
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace)
SETTINGS non_replicated_deduplication_window = 1000;
