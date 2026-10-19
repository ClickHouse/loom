-- Knowledge sharing (docs/architecture/sharing.md).
--
-- A collection is a namespace whose access comes only from grants, inherited
-- from its parent. Its namespace row in loom.namespaces has kind 'collection'
-- (or 'personal' for a user's own); this table holds the tree. One row per
-- collection, rewritten with a higher version on a move; `deleted` retires it.
CREATE TABLE IF NOT EXISTS loom.collections
(
    `org` LowCardinality(String),
    `namespace` String,
    `parent` String DEFAULT '',
    `personal` Bool DEFAULT false,
    `owner` String DEFAULT '',
    `created_at` DateTime64(3) DEFAULT now64(3),
    `deleted` Bool DEFAULT false,
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace)
SETTINGS non_replicated_deduplication_window = 1000;

-- Groups a SCIM client pushes for an org. `members` are grant principals
-- (`user:<id>`); a grant to `group:<group_id>` reaches every member. One row
-- per group, rewritten whole on every change; `deleted` retires it.
CREATE TABLE IF NOT EXISTS loom.principal_groups
(
    `org` LowCardinality(String),
    `group_id` String,
    `display_name` String DEFAULT '',
    `external_id` String DEFAULT '',
    `members` Array(String),
    `deleted` Bool DEFAULT false,
    `created_at` DateTime64(3) DEFAULT now64(3),
    `updated_at` DateTime64(3) DEFAULT now64(3),
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, group_id)
SETTINGS non_replicated_deduplication_window = 1000;

-- Who wrote each document, for provenance and for erasure by user. Access
-- never reads these. Rows written before them read '-' and ''.
ALTER TABLE loom.documents
    ADD COLUMN IF NOT EXISTS `agent` LowCardinality(String) DEFAULT '-',
    ADD COLUMN IF NOT EXISTS `user_id` String DEFAULT '-',
    ADD COLUMN IF NOT EXISTS `actor` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `source_class` LowCardinality(String) DEFAULT '';

ALTER TABLE loom.chunks
    ADD COLUMN IF NOT EXISTS `user_id` String DEFAULT '-';
