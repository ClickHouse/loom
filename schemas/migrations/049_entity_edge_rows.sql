-- The memory row (`memory_id`, writer `user_id`) and value (`content_sha256`) that named
-- each entity edge's pair. Reads by row use the sort key; the "still holds" lookup by
-- pair uses `pair_skip`. See docs/architecture/memory-architecture.md, "Rows that hold facts".
CREATE TABLE IF NOT EXISTS loom.entity_edge_rows
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `memory_id` UUID,
    `user_id` String,
    `content_sha256` FixedString(64),
    `entity_a` LowCardinality(String),
    `entity_b` LowCardinality(String),
    INDEX pair_skip (entity_a, entity_b) TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree
ORDER BY (org, namespace, memory_id, user_id, content_sha256, entity_a, entity_b)
-- As 032 sets on every MergeTree table: a retried insert keeps its dedup token.
SETTINGS non_replicated_deduplication_window = 1000;
