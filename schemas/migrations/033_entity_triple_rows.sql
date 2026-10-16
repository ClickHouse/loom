-- Which memory row stated each triple. `entity_triple_memories` links a triple to
-- a memory_key, which every session's row of that key shares; this links it to the
-- one row (`memory_id`) and to the value that row held when it stated it
-- (`content_sha256`, as on `loom.memory`), since a revision keeps the row's id.
-- When a row retires, the triple plane closes the triples its current value stated
-- and no live row still holds, whatever their subject was canonicalised to ("I live
-- in Paris" is `(user, lives_in, paris)`). `inferred` marks a successor the triple
-- plane wrote for the row rather than one its extractor stated; it stays the row's
-- after a revision while the new value says it in the same words, and is kept until
-- the row is erased. `user_id` is the row's writer, so a user's erasure finds its
-- links after the row itself is gone. The skip index serves the "still holds"
-- lookup, which asks by triple.
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
