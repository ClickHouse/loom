-- Change counts every grant, group or collection-tree change moves
-- (server/core/grant_cache.py). A change inserts `changes = 1` for its org and
-- kind; each process polls `sum(changes)` per (org, kind) and drops what that
-- kind of change makes stale when it moved. A sum only grows, whatever the
-- writers' clocks or the merges do. A build that predates this table never
-- reads it: its changes reach this build's caches through the TTL.
CREATE TABLE IF NOT EXISTS loom.acl_versions
(
    `org` LowCardinality(String),
    `kind` LowCardinality(String),
    `changes` UInt64 DEFAULT 1,
    `updated_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = SummingMergeTree(changes)
ORDER BY (org, kind)
SETTINGS non_replicated_deduplication_window = 1000;
