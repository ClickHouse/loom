-- Every grant row as it was inserted. loom.namespace_grants keeps only the latest row per
-- (org, namespace, subject), so a write that read the grant before a concurrent raise
-- landed cannot tell it lowered that raise, and a retried lowering cannot tell what it
-- lowered from. After such a write the server reads this log: when the row just before the
-- write's own ranks higher, or a retry finds the grant below one it held, the subject's
-- older tokens are cut. One row per grant write, kept for good like the grants themselves.
CREATE TABLE IF NOT EXISTS loom.namespace_grant_log
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `subject` String,
    `role` LowCardinality(String),
    `status` LowCardinality(String),
    `revoked_at` Nullable(DateTime),
    `version` UInt64
)
ENGINE = MergeTree
PARTITION BY org
ORDER BY (org, namespace, subject, version);

CREATE MATERIALIZED VIEW IF NOT EXISTS loom.namespace_grant_log_mv TO loom.namespace_grant_log
AS SELECT org, namespace, subject, role, status, revoked_at, version
FROM loom.namespace_grants;

-- The grants written before this log: a lowering of one must find the row it lowered.
-- A row the view logged meanwhile is logged twice, which neither read minds.
INSERT INTO loom.namespace_grant_log
SELECT org, namespace, subject, role, status, revoked_at, version
FROM loom.namespace_grants FINAL;
