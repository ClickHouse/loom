-- State that lived on a Loom host's disk or in its memory, moved into ClickHouse so a
-- new Loom pointed at this database gets every feature back.

-- Uploaded document originals, addressed per namespace by the sha256 of their bytes
-- (`loom.documents.blob_path`). Two documents with the same bytes share one row.
CREATE TABLE IF NOT EXISTS loom.document_blobs
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `sha256` FixedString(64),
    `data` String CODEC(ZSTD(3)),
    `size` UInt64,
    `created_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (org, namespace, sha256)
-- A row is a whole file and every read is one key: 128-row granules keep a point read
-- from decompressing thousands of neighbouring files.
SETTINGS index_granularity = 128, non_replicated_deduplication_window = 1000;

-- Token signing keys beside the root secret (LOOM_JWT_SECRET, kid `default`). The
-- private half is sealed with the root secret; `public_pem` is what JWKS serves.
-- A key signs from `activate_at`, set far enough ahead that every process has
-- loaded it by then; a retired key no longer verifies.
CREATE TABLE IF NOT EXISTS loom.signing_keys
(
    `kid` String,
    `algorithm` LowCardinality(String),
    `sealed_key` String,
    `public_pem` String DEFAULT '',
    `created_at` DateTime64(3) DEFAULT now64(3),
    `activate_at` DateTime64(3),
    `retired_at` Nullable(DateTime64(3)) DEFAULT NULL,
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY kid
SETTINGS non_replicated_deduplication_window = 1000;

-- Operator overrides set at runtime through the admin routes: rate limits
-- (`ratelimit.namespace` / `ratelimit.org`) and latency budgets (`budget.op` /
-- `budget.consumer`). `value` is JSON; `deleted = 1` clears the key.
CREATE TABLE IF NOT EXISTS loom.runtime_overrides
(
    `kind` LowCardinality(String),
    `key` String,
    `value` String,
    `deleted` UInt8 DEFAULT 0,
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (kind, key)
SETTINGS non_replicated_deduplication_window = 1000;

-- Failed sign-ins and sign-ups, counted per kind and key across every Loom process.
-- The key is an HMAC (keyed by the root secret) of the identifier or client
-- address, never the value.
CREATE TABLE IF NOT EXISTS loom.auth_attempts
(
    `kind` LowCardinality(String),
    `key_sha256` FixedString(64),
    `at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (kind, key_sha256, at)
TTL toDateTime(at) + toIntervalDay(1)
SETTINGS non_replicated_deduplication_window = 1000;

-- Each Loom process's keyring refresh interval, recorded hourly, so `loom signing-key
-- rotate` waits out the slowest process before a new key signs.
CREATE TABLE IF NOT EXISTS loom.keyring_readers
(
    `process` String,
    `refresh_seconds` Float64,
    `seen_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (seen_at, process)
TTL toDateTime(seen_at) + toIntervalDay(1)
SETTINGS non_replicated_deduplication_window = 1000;

-- Provider participants heartbeat. One whose `heartbeat_at` is older than its
-- `stale_after_s` is listed as stale (0, a row of a build without heartbeats,
-- never expires). `host_proof` names its host, kernel boot, container, pid
-- namespace, pid and start time, so a later process there can prove it stopped.
ALTER TABLE loom._provider_participants
    ADD COLUMN IF NOT EXISTS `heartbeat_at` DateTime64(3) DEFAULT now64(3),
    ADD COLUMN IF NOT EXISTS `stale_after_s` UInt32 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS `host_proof` String DEFAULT '';
