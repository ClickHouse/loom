-- One counter per org that every grant change moves (server/core/grant_cache.py).
-- A change inserts `changes = 1` for its org; each process polls `sum(changes)`
-- per org and drops the org's cached grants when it moved. A sum only grows,
-- whatever the writers' clocks or the merges do. A build that predates this
-- table never reads it: its grant changes reach this build's caches through
-- the TTL.
CREATE TABLE IF NOT EXISTS loom.acl_versions
(
    `org` LowCardinality(String),
    `changes` UInt64 DEFAULT 1,
    `updated_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = SummingMergeTree(changes)
ORDER BY org
SETTINGS non_replicated_deduplication_window = 1000;
