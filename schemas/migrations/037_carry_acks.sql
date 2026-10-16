-- Per process, the column lists its ReplacingMergeTree version writes carry by
-- (server/core/rmt_carry.py): a fingerprint per carried table. A booting process
-- waits until every live one has the schema it migrated to before it serves.
CREATE TABLE IF NOT EXISTS loom.carry_acks
(
    `process` String,
    `fingerprints` String,
    `at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(at)
ORDER BY process
TTL toDateTime(at) + toIntervalDay(1)
SETTINGS non_replicated_deduplication_window = 1000;
