-- Connected registrations are keyed by canonical sign-in subject, not end-user user_id.
-- Old builds know no connection and see connected sources with kind/credentials empty.
CREATE TABLE IF NOT EXISTS loom.connected_accounts
(
    org LowCardinality(String),
    subject String,
    kind LowCardinality(String),
    connection_id String,
    account_id String DEFAULT '',
    workspace_id String DEFAULT '',
    account_email_enc String DEFAULT '',
    credential_enc String DEFAULT '',
    scopes Array(String),
    active Bool DEFAULT false,
    refresh_id String DEFAULT '',
    connected_at Nullable(DateTime64(3, 'UTC')),
    disconnected_at Nullable(DateTime64(3, 'UTC')),
    version UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, subject, kind)
SETTINGS non_replicated_deduplication_window = 1000;

ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS credential_mode LowCardinality(String) DEFAULT 'shared';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS owner_subject String DEFAULT '';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS connection_id String DEFAULT '';
