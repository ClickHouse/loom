-- Optional provider push setup. Older builds do not read it; new setup waits for
-- every live build to understand retirement and erasure of this registry.
CREATE TABLE IF NOT EXISTS loom.knowledge_source_push
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `name` String,
    `source_id` String,
    `provider` LowCardinality(String),
    `generation` String,
    `fingerprint` String,
    `provider_id` String DEFAULT '',
    `status` LowCardinality(String) DEFAULT 'pending',
    `secret_enc` String DEFAULT '',
    `agent` LowCardinality(String) DEFAULT '-',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `actor` String DEFAULT '',
    `source_class` LowCardinality(String) DEFAULT 'derived',
    `updated_at` DateTime64(3) DEFAULT now64(3),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, source_id)
SETTINGS non_replicated_deduplication_window = 1000;
