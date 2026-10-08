-- The migrator's own tables: the ledger of applied files (filename +
-- sha256 of the file at apply time) and the lease that serialises appliers
-- across containers.
CREATE TABLE IF NOT EXISTS loom._migrations
(
    `filename` String,
    `checksum` FixedString(64),
    `applied_at` DateTime DEFAULT now(),
    `applied_by` String DEFAULT ''
)
ENGINE = ReplacingMergeTree(applied_at)
ORDER BY filename;

CREATE TABLE IF NOT EXISTS loom._migration_lease
(
    `name` LowCardinality(String),
    `holder` String,
    `acquired_at` DateTime64(3) DEFAULT now64(3),
    `renewed_at` DateTime64(3) DEFAULT now64(3),
    `ttl_s` UInt32 DEFAULT 300,
    `released` Bool DEFAULT false,
    `build_commit` LowCardinality(String) DEFAULT '',
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (name, holder)
TTL toDateTime(renewed_at) + toIntervalDay(7);
