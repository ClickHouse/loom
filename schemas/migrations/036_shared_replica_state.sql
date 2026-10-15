-- State every replica shares, so a replica added or upgraded at any time serves
-- what the others serve.

-- The dashboard's built assets, published by each replica at boot: during a rolling
-- upgrade a page from one build loads its assets from a replica of another.
CREATE TABLE IF NOT EXISTS loom.frontend_assets
(
    `path` String,
    `sha256` String,
    `body` String CODEC(ZSTD(3)),
    `size` UInt32,
    `published_by` String,
    `published_at` DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(published_at)
ORDER BY path
TTL published_at + INTERVAL 30 DAY
SETTINGS non_replicated_deduplication_window = 1000;

-- The embedder's similarity calibration, probed once and adopted by every replica.
CREATE TABLE IF NOT EXISTS loom.embedder_calibration
(
    `embedder_model` LowCardinality(String),
    `thresholds_json` String,
    `calibration_hash` String,
    `calibrated_by` String,
    `calibrated_at` DateTime DEFAULT now(),
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY embedder_model
SETTINGS non_replicated_deduplication_window = 1000;

-- Write/read pairs the usage fold has already credited as `acted`, so a new leader
-- does not credit them again.
CREATE TABLE IF NOT EXISTS loom.usage_acted_credits
(
    `pair_sha256` FixedString(64),
    `first_write` DateTime,
    `credited_at` DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(credited_at)
ORDER BY pair_sha256
TTL first_write + toIntervalDay(3)
SETTINGS non_replicated_deduplication_window = 1000;
