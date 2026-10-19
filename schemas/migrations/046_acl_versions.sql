-- One watermark per org that every grant change moves (server/core/grant_cache.py).
-- Each process polls `count()` and `sum(version)` per org and drops the org's
-- cached grants when its pair moves, so a revoke reaches every replica within
-- the poll interval instead of the cache TTL. A change inserts a row with a
-- fresh version and never reads first, so two writers never write the same
-- one; merges collapse an org to its newest row, which moves the pair and
-- costs one extra re-read. A build that predates this table never reads it:
-- its grant changes reach this build's caches through the TTL.
CREATE TABLE IF NOT EXISTS loom.acl_versions
(
    `org` LowCardinality(String),
    `version` UInt64,
    `updated_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(version)
ORDER BY org
SETTINGS non_replicated_deduplication_window = 1000;
