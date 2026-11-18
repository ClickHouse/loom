-- Fresh membership is evidence only for the source and mirror that observed it.
-- Empty storage admits no group; older builds ignore this table.
CREATE TABLE IF NOT EXISTS loom.knowledge_group_members
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `source_id` String,
    `group` String,
    `name` String,
    `members` Array(String),
    `read_at` DateTime64(3, 'UTC'),
    `deleted` Bool DEFAULT false,
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, source_id, `group`)
SETTINGS non_replicated_deduplication_window = 1000;
