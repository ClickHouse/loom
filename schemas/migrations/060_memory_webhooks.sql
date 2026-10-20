-- Memory webhooks (server/memory/app/webhooks.py). A namespace admin registers a URL,
-- the name of the environment variable holding its signing secret (never the secret)
-- and the event types it wants; `webhook.remove` writes `is_deleted = 1`.
CREATE TABLE IF NOT EXISTS loom.memory_webhooks
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `webhook_id` String,
    `url` String,
    `secret_ref` String,
    `events` Array(LowCardinality(String)),
    `created_by` String DEFAULT '',
    `created_at` DateTime64(3) DEFAULT now64(3),
    `is_deleted` UInt8 DEFAULT 0,
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, webhook_id)
SETTINGS non_replicated_deduplication_window = 1000;

-- One row per (event, webhook), written once by the write path and never rewritten:
-- ids and the identity tuple, never a value, tags or evidence. The key and user id
-- name a memory row, so erasure deletes the rows (erasure.store.steps_ledgers), and
-- nothing the delivery worker writes can bring one back. Sorted by webhook so a pass
-- reads only the webhooks it can send to and an admin only the namespace's events;
-- a queue, partitioned by day, so whole days expire 3 days after their events.
CREATE TABLE IF NOT EXISTS loom.memory_webhook_outbox
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `webhook_id` String,
    `event_id` String,
    `event_type` LowCardinality(String),
    `agent` String,
    `user_id` String,
    `session_id` String,
    `memory_key` String,
    `memory_id` String,
    `memory_version` UInt64,
    `actor` String,
    `request_id` String,
    `erasure_id` String DEFAULT '',
    `occurred_at` DateTime64(3)
)
ENGINE = MergeTree
PARTITION BY toDate(occurred_at)
ORDER BY (org, namespace, webhook_id, occurred_at, event_id)
TTL toDateTime(occurred_at) + toIntervalDay(3)
SETTINGS non_replicated_deduplication_window = 1000, ttl_only_drop_parts = 1;

-- The delivery worker's record of each event: one row per lease and per attempt,
-- appended, the newest `version` of an event winning. Event ids, counts and HTTP
-- statuses only, no key or identity.
CREATE TABLE IF NOT EXISTS loom.memory_webhook_attempts
(
    `event_id` String,
    `status` LowCardinality(String),
    `attempts` UInt16,
    `next_attempt_at` DateTime64(3),
    `last_status` UInt16 DEFAULT 0,
    `last_error` String DEFAULT '',
    `recorded_at` DateTime64(3) DEFAULT now64(3),
    `version` UInt64
)
ENGINE = MergeTree
PARTITION BY toDate(recorded_at)
ORDER BY (event_id, version)
TTL toDateTime(recorded_at) + toIntervalDay(3)
SETTINGS non_replicated_deduplication_window = 1000, ttl_only_drop_parts = 1;
