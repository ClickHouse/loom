-- The memory row (`memory_id`, writer `user_id`) and value (`content_sha256`) that stated
-- each triple. Reads by row use the sort key; the "still holds" lookup by triple uses
-- `triple_skip`. See docs/architecture/memory-architecture.md, "Rows that hold facts".
CREATE TABLE IF NOT EXISTS loom.entity_triple_rows
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `memory_id` UUID,
    `user_id` String,
    `content_sha256` FixedString(64),
    `subject` LowCardinality(String),
    `relation` LowCardinality(String),
    `object` LowCardinality(String),
    `inferred` Bool,
    INDEX triple_skip (subject, relation, object) TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree
ORDER BY (org, namespace, memory_id, user_id, content_sha256, subject, relation, object, inferred)
-- As 032 sets on every MergeTree table: a retried insert keeps its dedup token.
SETTINGS non_replicated_deduplication_window = 1000;
