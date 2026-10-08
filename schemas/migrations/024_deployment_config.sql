-- The main Loom's published settings, for read-replica Looms on the same service:
-- one JSON row, secrets never included. A read-replica Loom takes each value for a
-- setting its own env leaves unset.
CREATE TABLE IF NOT EXISTS loom.deployment_config
(
    `name` LowCardinality(String),
    `value` String CODEC(ZSTD(3)),
    `published_by` String DEFAULT '',
    `build_commit` String DEFAULT '',
    `published_at` DateTime64(3) DEFAULT now64(3),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY name;
