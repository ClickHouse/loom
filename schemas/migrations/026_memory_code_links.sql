-- A memory linked to the code it is about. One row per (memory, symbol or file), kept
-- against the repository's trunk code namespace; the link worker writes a new version
-- as the code changes. `qualified_name` '' is a file link, '*' a memory waiting for its
-- repository's first index. `body_hash` is the definition's fingerprint when the memory
-- was written, `checked_hash` at the last check, `anchored_*` where it was
-- (`anchored_hash` its fingerprint there and `anchored_name` its name, kept across
-- renames); `inputs_hash` covers what the links were made from (text, files,
-- repository, user); `complete` = 0 when the memory named more than the link cap;
-- `policy_due` marks a memory whose policy has yet to run on its latest change;
-- `suggestion` is the corrected claim `revise` proposes for the code whose fingerprint
-- is `judged_hash`; `carried` marks a link its renamed class carried (what surrounds
-- the code changed) until a revalidation; `fingerprint_version` the rules the hashes
-- were computed with.
CREATE TABLE IF NOT EXISTS loom.memory_code_links
(
    `org` LowCardinality(String),
    `code_namespace` LowCardinality(String),
    `path` String,
    `qualified_name` String,
    `namespace` LowCardinality(String),
    `memory_id` UUID,
    `memory_key` String,
    `agent` LowCardinality(String) DEFAULT '',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `inputs_hash` String DEFAULT '',
    `complete` UInt8 DEFAULT 1,
    `body_hash` String DEFAULT '',
    `checked_hash` String DEFAULT '',
    `start_line` UInt32 DEFAULT 0,
    `end_line` UInt32 DEFAULT 0,
    `state` Enum8('fresh' = 1, 'moved' = 2, 'changed' = 3, 'missing' = 4, 'pending' = 5, 'unresolved' = 6),
    `checked_commit` String DEFAULT '',
    `checked_path` String DEFAULT '',
    `anchored_commit` String DEFAULT '',
    `anchored_path` String DEFAULT '',
    `anchored_hash` String DEFAULT '',
    `anchored_name` String DEFAULT '',
    `baseline_commit` String DEFAULT '',
    `baseline_path` String DEFAULT '',
    `baseline_name` String DEFAULT '',
    `changed_since` String DEFAULT '',
    `former` Array(Tuple(
        path String, name String, hash String,
        baseline_commit String, baseline_path String, baseline_name String)) DEFAULT [],
    `policy_due` UInt8 DEFAULT 0,
    `suggestion` String DEFAULT '',
    `judged_hash` String DEFAULT '',
    `carried` UInt8 DEFAULT 0,
    `fingerprint_version` LowCardinality(String) DEFAULT '',
    `checked_at` DateTime DEFAULT now(),
    `tombstoned_at` Nullable(DateTime) DEFAULT NULL,
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9))),
    INDEX idx_memory_code_links_memory_id memory_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_memory_code_links_memory_key memory_key TYPE bloom_filter GRANULARITY 1,
    INDEX idx_memory_code_links_former_paths former.path TYPE bloom_filter GRANULARITY 1,
    INDEX idx_memory_code_links_anchored_path anchored_path TYPE bloom_filter GRANULARITY 1
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, code_namespace, path, qualified_name, namespace, memory_id)
SETTINGS index_granularity = 1024;

-- Every memory write in the order it was inserted: `queued_at` is stamped when the
-- insert runs, so a write that took its version early and landed late is still read
-- after the ones before it. The link worker's input; a worker further behind than the
-- TTL relinks from loom.memory itself.
CREATE TABLE IF NOT EXISTS loom.memory_code_link_queue
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `agent` LowCardinality(String),
    `user_id` String,
    `session_id` String,
    `memory_key` String,
    `version` UInt64,
    `queued_at` DateTime64(6) DEFAULT now64(6)
)
ENGINE = MergeTree
PARTITION BY toDate(queued_at)
ORDER BY queued_at
TTL toDateTime(queued_at) + INTERVAL 7 DAY
SETTINGS ttl_only_drop_parts = 1;

-- Every write is queued, touches and pins included: one that drops a memory's `repo:`
-- tag must reach the worker to drop its links. A queued row is its identity alone; the
-- worker skips a memory whose links already match it after one read of its head.
CREATE MATERIALIZED VIEW IF NOT EXISTS loom.memory_code_link_queue_mv TO loom.memory_code_link_queue
AS SELECT org, namespace, agent, user_id, session_id, memory_key, version
FROM loom.memory;

-- What Loom does to a namespace's code-linked memories when their code changes:
-- '' / `retire_removed` (retire a memory whose code is gone), `flag` (only mark the
-- links), `revise` (also re-check a changed claim with the LLM and suggest a fix).
ALTER TABLE loom.namespaces
    ADD COLUMN IF NOT EXISTS `code_change_policy` LowCardinality(String) DEFAULT '';
