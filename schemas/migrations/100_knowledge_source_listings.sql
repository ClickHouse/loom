-- loom.knowledge_source_listings: when each mirrored source's last complete listing
-- started (docs/architecture/document-sources.md, Every complete listing reads the lists
-- again). `namespace` is the mirror. Written only once every list that listing changed
-- is written, so a row is never newer than the lists it vouches for. Names and times
-- only, no memory content.
CREATE TABLE IF NOT EXISTS loom.knowledge_source_listings
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `source_id` String,
    `name` String,
    `started_at` DateTime64(3),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, source_id)
SETTINGS non_replicated_deduplication_window = 1000;
