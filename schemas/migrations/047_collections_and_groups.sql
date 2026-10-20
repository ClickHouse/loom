-- Knowledge sharing (docs/architecture/sharing.md).
--
-- A collection is a namespace whose access comes only from grants, inherited
-- from its parent. Its namespace row in loom.namespaces has kind 'collection';
-- this table holds the tree. One row per collection, rewritten with a higher
-- version on a move; `deleted` retires it. A user's personal collection sits in
-- no tree and has no row here: its name says whose it is.
CREATE TABLE IF NOT EXISTS loom.collections
(
    `org` LowCardinality(String),
    `namespace` String,
    `parent` String DEFAULT '',
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

-- Who wrote each document and what it is, for provenance, for erasure by user
-- and for a search to tell an upload from what an agent derived or fetched:
-- the agent, the end user, the session (a chat, a run) it was written in, the
-- token's principal and the content's class. Access never reads these. Rows
-- written before them read '-' and ''. The chunks carry what search returns
-- and filters on, so a hit needs no join.
ALTER TABLE loom.documents
    ADD COLUMN IF NOT EXISTS `agent` LowCardinality(String) DEFAULT '-',
    ADD COLUMN IF NOT EXISTS `user_id` String DEFAULT '-',
    ADD COLUMN IF NOT EXISTS `session_id` String DEFAULT '-',
    ADD COLUMN IF NOT EXISTS `actor` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `source_class` LowCardinality(String) DEFAULT '';

ALTER TABLE loom.chunks
    ADD COLUMN IF NOT EXISTS `agent` LowCardinality(String) DEFAULT '-',
    ADD COLUMN IF NOT EXISTS `user_id` String DEFAULT '-',
    ADD COLUMN IF NOT EXISTS `session_id` String DEFAULT '-',
    ADD COLUMN IF NOT EXISTS `source_class` LowCardinality(String) DEFAULT '';

-- Whether a saga purges, so a resumed one keeps purging: a user erasure may
-- (a namespace_list one also carries it in its scope_value). Rows written
-- before it read false, as those sagas did not purge.
ALTER TABLE loom.erasure_requests
    ADD COLUMN IF NOT EXISTS `purge` Bool DEFAULT false;
