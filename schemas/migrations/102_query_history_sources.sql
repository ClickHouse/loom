-- Query-history registrations hold connection metadata and sealed credentials,
-- never query text. They stay empty while an older build can run an erasure.
ALTER TABLE loom.learnings_queue
    ADD COLUMN IF NOT EXISTS source_resource_id String DEFAULT '';

CREATE TABLE IF NOT EXISTS loom.query_history_sources
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `agent` LowCardinality(String) DEFAULT '-',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `name` String,
    `source_id` UUID,
    `host` String,
    `port` UInt16,
    `secure` Bool,
    `user` String,
    `password_enc` String,
    `cluster` String DEFAULT '',
    `databases` Array(String),
    `actor` String,
    `source_class` LowCardinality(String),
    `created_at` DateTime,
    `tombstoned_at` Nullable(DateTime) DEFAULT NULL,
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, name)
SETTINGS non_replicated_deduplication_window = 1000;
