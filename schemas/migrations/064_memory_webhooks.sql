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

-- The outbox the write path fills and the memory_webhook_deliver worker drains: one
-- row per (event, webhook), written beside the memory write, then a new version per
-- lease and per attempt. It holds ids and the identity tuple, never a value, tags or
-- evidence; the key and user id name a memory row, so erasure clears the rows
-- (erasure.store.steps_ledgers). A queue: rows expire 30 days after the event.
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
    `occurred_at` DateTime64(3),
    `status` LowCardinality(String) DEFAULT 'pending',
    `attempts` UInt16 DEFAULT 0,
    `next_try_at` DateTime64(3),
    `lease_until` DateTime64(3) DEFAULT toDateTime64(0, 3),
    `last_status` UInt16 DEFAULT 0,
    `last_error` String DEFAULT '',
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, webhook_id, event_id)
TTL toDateTime(occurred_at) + toIntervalDay(30)
SETTINGS non_replicated_deduplication_window = 1000;
