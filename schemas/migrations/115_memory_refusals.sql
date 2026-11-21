-- Durable consent fingerprints contain no removed plaintext and have no TTL.
CREATE TABLE IF NOT EXISTS loom.memory_refusals
(
    org String,
    namespace String,
    agent String DEFAULT '-',
    user_id String DEFAULT '-',
    session_id String DEFAULT '-',
    scope_kind LowCardinality(String) DEFAULT 'identity',
    scope_value String,
    actor String,
    source_class LowCardinality(String) DEFAULT 'derived',
    kind LowCardinality(String),
    units UInt32,
    digest FixedString(64),
    created_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (org, scope_kind, scope_value, user_id, kind, units, digest)
SETTINGS non_replicated_deduplication_window = 1000;

-- A numeric version alone cannot distinguish independently written full states.
ALTER TABLE loom.delete_version_checks
    ADD COLUMN IF NOT EXISTS checked_state_sha256 String DEFAULT '';
