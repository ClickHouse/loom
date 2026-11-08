-- loom.analysis_secrets: the one row of insights.query's analyst secrets, the key its row
-- policies check a scope's signature with and the analyst user's password, sealed together
-- as one value with server/core/secrets_at_rest.py, so a reseal rewrites both at once. The
-- first boot makes them under the analysis lease; `loom secrets reseal` rewrites the row at
-- a higher version when the signing secret is replaced, so the key itself never changes.
-- No content.
CREATE TABLE IF NOT EXISTS loom.analysis_secrets
(
    `id` UInt8 DEFAULT 0,
    `sealed` String,
    `version` UInt64,
    `created_at` DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(version)
ORDER BY id
SETTINGS non_replicated_deduplication_window = 1000;

-- loom.analysis_canary: one constant row in a reserved scope (org '', namespace ''),
-- under a policy written last with the same key as every other. insights.query's probe
-- reads it signed: one row back shows the policies accept the key this replica signs
-- with, which a read of real data cannot tell from an empty scope. No content.
CREATE TABLE IF NOT EXISTS loom.analysis_canary
(
    `id` UInt8 DEFAULT 0,
    `org` String DEFAULT '',
    `namespace` String DEFAULT ''
)
ENGINE = ReplacingMergeTree
ORDER BY id
SETTINGS non_replicated_deduplication_window = 1000;
