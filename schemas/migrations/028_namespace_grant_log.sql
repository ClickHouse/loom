-- Every grant row as it was inserted. loom.namespace_grants keeps only the latest row per
-- (org, namespace, subject), so a write that read the grant before a concurrent raise
-- landed cannot tell it lowered that raise. After such a write, the server reads this log:
-- when the row just before the write's own ranks higher, the subject's older tokens are
-- cut. The check runs right after the write, so a week of rows is plenty.
CREATE TABLE IF NOT EXISTS loom.namespace_grant_log
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `subject` String,
    `role` LowCardinality(String),
    `status` LowCardinality(String),
    `revoked_at` Nullable(DateTime),
    `version` UInt64,
    `logged_at` DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toDate(logged_at)
ORDER BY (org, namespace, subject, version)
TTL logged_at + INTERVAL 7 DAY
SETTINGS ttl_only_drop_parts = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS loom.namespace_grant_log_mv TO loom.namespace_grant_log
AS SELECT org, namespace, subject, role, status, revoked_at, version
FROM loom.namespace_grants;
