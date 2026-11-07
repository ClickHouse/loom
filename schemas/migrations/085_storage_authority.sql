-- The memory authority this deployment records at its first boot: clickhouse or
-- postgres, with an epoch (1: switching authority needs a migration this build does
-- not provide) and the deployment id its Postgres also records. A replica configured
-- for another authority refuses to start.
CREATE TABLE IF NOT EXISTS loom.storage_authority
(
    authority LowCardinality(String),
    epoch UInt64,
    deployment_id String,
    recorded_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(epoch)
ORDER BY tuple()
SETTINGS non_replicated_deduplication_window = 1000;
