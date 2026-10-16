-- Which memory row stated each entity edge, as `entity_triple_rows` links triples.
-- `entity_edge_memories` links a pair to a memory_key; this links it to the one row
-- (`memory_id`) and to the value that row held when it named the pair
-- (`content_sha256`, as on `loom.memory`), since a revision keeps the row's id. When
-- a dropped fact's edge would end, it stays open while a live row's current value
-- has a link to the pair. `user_id` is the row's writer, so a user's erasure finds
-- its links after the row itself is gone. The skip index serves the "still holds"
-- lookup, which asks by pair.
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
