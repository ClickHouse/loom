-- The operator's extraction policy (server/memory/app/extraction_policy.py,
-- `extraction_policy.set`): rules every model call that turns raw text into
-- memories is given, which a request cannot drop or override.
--
-- namespace = '' is the org-wide policy; a namespace's own policy adds to it.
-- One row per change, the newest version wins; deleted = 1 is a cleared
-- policy. updated_by is the subject that set or cleared it. A build that
-- predates this table never reads it.
CREATE TABLE IF NOT EXISTS loom.extraction_policies
(
    `org` String,
    `namespace` String,
    `policy` String,
    `updated_by` String,
    `updated_at` DateTime64(3, 'UTC'),
    `deleted` UInt8,
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace)
SETTINGS non_replicated_deduplication_window = 1000;
