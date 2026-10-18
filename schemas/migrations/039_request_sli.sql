-- The SLI columns of a request's ledger row (server/observability/sli.py): the
-- transport that served it, its op class, whether a failure was the caller's
-- (`client`) or the server's (`server`), and the latency budget it was held to
-- (budgets.effective_budget_ms, 0 = none). '' and 0 on rows written before them.
ALTER TABLE loom.agent_events
    ADD COLUMN IF NOT EXISTS `transport` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `op_class` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `status_class` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `budget_ms` UInt32 DEFAULT 0;

-- Telemetry Loom derives from its own tables lives outside `loom`.
CREATE DATABASE IF NOT EXISTS loom_telemetry;

-- Requests per minute, op and tenant: the table the SLO alert rules and tiles
-- read, so an evaluation scans minutes rather than the ledger. `slow` counts
-- successful requests over their budget; `latency` holds the quantile state
-- of successful requests.
CREATE TABLE IF NOT EXISTS loom_telemetry.sli_minute
(
    `minute` DateTime,
    `op_class` LowCardinality(String),
    `operation` LowCardinality(String),
    `transport` LowCardinality(String),
    `traffic_class` LowCardinality(String),
    `org` LowCardinality(String),
    `requests` SimpleAggregateFunction(sum, UInt64),
    `server_errors` SimpleAggregateFunction(sum, UInt64),
    `client_errors` SimpleAggregateFunction(sum, UInt64),
    `rate_limited` SimpleAggregateFunction(sum, UInt64),
    `slow` SimpleAggregateFunction(sum, UInt64),
    `latency` AggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), UInt32)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (op_class, minute, operation, transport, traffic_class, org)
TTL minute + toIntervalDay(400)
SETTINGS non_replicated_deduplication_window = 1000;

-- Rows from a build that does not write the SLI columns have no transport and
-- are left out, so a rolling upgrade thins the sample instead of skewing it.
CREATE MATERIALIZED VIEW IF NOT EXISTS loom_telemetry.sli_minute_mv
TO loom_telemetry.sli_minute
AS SELECT
    toStartOfMinute(ts) AS minute,
    op_class,
    operation,
    transport,
    traffic_class,
    org,
    count() AS requests,
    countIf(status_class = 'server') AS server_errors,
    countIf(status_class = 'client') AS client_errors,
    countIf(error_code = 'RATE_LIMITED') AS rate_limited,
    countIf(success AND budget_ms > 0 AND latency_ms > budget_ms) AS slow,
    quantilesTDigestStateIf(0.5, 0.95, 0.99)(latency_ms, success) AS latency
FROM loom.agent_events
WHERE kind = 'request' AND transport != ''
GROUP BY minute, op_class, operation, transport, traffic_class, org;
