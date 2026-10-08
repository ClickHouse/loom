-- Operator-owned model configuration and restart-safe rebuild checkpoints.
-- The configuration envelope (including API keys) is encrypted before insertion.
-- Candidate revisions do not become active until their validation/rebuild succeeds.
CREATE TABLE IF NOT EXISTS loom.provider_configuration
(
    `config_id` UUID,
    `parent_id` UUID DEFAULT '00000000-0000-0000-0000-000000000000',
    `state` LowCardinality(String),
    `envelope` String CODEC(ZSTD(3)),
    `progress` String DEFAULT '{}' CODEC(ZSTD(3)),
    `error_code` LowCardinality(String) DEFAULT '',
    `created_at` DateTime64(6) DEFAULT now64(6),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY config_id;
