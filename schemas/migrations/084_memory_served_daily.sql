-- loom.memory_served_daily: how often an agent read returned a memory, per memory, agent
-- and day. An agent read op (memory.search, search.query, context.pack, memory.stack,
-- memory.ask, procedure.match, memory.for_file) inserts one row per key its answer
-- returned; the SummingMergeTree collapses them, so a pack on every turn adds one row per
-- (memory, agent, day). Nothing ranks on it and the usage fold never reads it: the recall
-- credit that ranks stays in loom.memory_usage_events. Counts and ids only, no content.
CREATE TABLE IF NOT EXISTS loom.memory_served_daily
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `memory_id` UUID,
    `agent` LowCardinality(String),
    `day` Date,
    `served` UInt64,
    `last_at` SimpleAggregateFunction(max, DateTime)
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (org, namespace, memory_id, agent, day)
SETTINGS non_replicated_deduplication_window = 1000;
