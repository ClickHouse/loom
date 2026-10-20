-- memory.delete_version's checks after its tombstone (server/memory/app/handlers.py):
-- one row per delete, written before the tombstone with `done = 0` and again with
-- `done = 1` once the check has run. A plain write takes no key lock, so one that
-- committed between the delete's version check and its tombstone is found only by that
-- read; when it fails, the row stays `done = 0` and the delete_version_check worker
-- runs the check later, so the write is never left under the tombstone. `checked` is
-- the version the delete removed, `tombstone` its tombstone's. The identity and key
-- name a memory row, so erasure clears the rows (erasure.store.steps_ledgers).
CREATE TABLE IF NOT EXISTS loom.delete_version_checks
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `agent` String,
    `user_id` String,
    `session_id` String,
    `memory_key` String,
    `checked` UInt64,
    `tombstone` UInt64,
    `done` UInt8 DEFAULT 0,
    `created_at` DateTime DEFAULT now(),
    `updated_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (org, namespace, agent, user_id, session_id, memory_key, tombstone)
TTL created_at + toIntervalDay(30)
SETTINGS non_replicated_deduplication_window = 1000;
