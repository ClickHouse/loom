-- The SLI columns of a request's ledger row (server/observability/sli.py): the
-- transport that served it, its SLO class, whether a failure was the caller's
-- (`client`) or the server's (`server`), and the latency budget it was held to
-- (budgets.effective_budget_ms, 0 = none). '' and 0 on rows written before them.
ALTER TABLE loom.agent_events
    ADD COLUMN IF NOT EXISTS `transport` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `slo_class` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `status_class` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `budget_ms` UInt32 DEFAULT 0;

-- Telemetry Loom derives from its own tables lives outside `loom`.
CREATE DATABASE IF NOT EXISTS loom_telemetry;

-- Requests per minute, SLO class, operation and tenant, for SLO dashboards and
-- alert rules to read, so an evaluation scans minutes rather than the ledger.
-- `rate_limited` is among `client_errors`; `slow` counts successful requests
-- over their budget; `latency` holds the quantile state of successful
-- requests; `ts_hwm` is the newest ledger row folded in, where the next
-- refresh resumes.
CREATE TABLE IF NOT EXISTS loom_telemetry.sli_minute
(
    `minute` DateTime,
    `slo_class` LowCardinality(String),
    `operation` LowCardinality(String),
    `transport` LowCardinality(String),
    `traffic_class` LowCardinality(String),
    `org` LowCardinality(String),
    `requests` SimpleAggregateFunction(sum, UInt64),
    `server_errors` SimpleAggregateFunction(sum, UInt64),
    `client_errors` SimpleAggregateFunction(sum, UInt64),
    `rate_limited` SimpleAggregateFunction(sum, UInt64),
    `slow` SimpleAggregateFunction(sum, UInt64),
    `latency` AggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), UInt32),
    `ts_hwm` SimpleAggregateFunction(max, DateTime64(3)) DEFAULT toDateTime64(0, 3)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (slo_class, minute, operation, transport, traffic_class, org)
TTL minute + toIntervalDay(400)
SETTINGS ttl_only_drop_parts = 1, non_replicated_deduplication_window = 1000;

-- Folds the ledger in once a minute, rows at least 10 s old, so a ledger
-- insert never waits on it. The first refresh starts 31 days back, enough for
-- every SLO window without reading the whole ledger; the high-water mark is
-- read from those 31 days too, since an older one loses to that floor. Rows
-- with no SLO class (the `/v1/me/*` routes, MCP's initialize) are left out.
CREATE MATERIALIZED VIEW IF NOT EXISTS loom_telemetry.sli_minute_refresh
REFRESH EVERY 1 MINUTE APPEND TO loom_telemetry.sli_minute
DEFINER = CURRENT_USER SQL SECURITY DEFINER
AS SELECT
    toStartOfMinute(ts) AS minute,
    slo_class,
    operation,
    transport,
    traffic_class,
    org,
    count() AS requests,
    countIf(status_class = 'server') AS server_errors,
    countIf(status_class = 'client') AS client_errors,
    countIf(error_code = 'RATE_LIMITED') AS rate_limited,
    countIf(success AND budget_ms > 0 AND latency_ms > budget_ms) AS slow,
    quantilesTDigestStateIf(0.5, 0.95, 0.99)(latency_ms, success) AS latency,
    max(ts) AS ts_hwm
FROM loom.agent_events
WHERE kind = 'request' AND slo_class != ''
  AND ts > greatest(
      (SELECT max(ts_hwm) FROM loom_telemetry.sli_minute
       WHERE minute >= toStartOfMinute(now() - toIntervalDay(31))),
      now64(3) - toIntervalDay(31))
  AND ts <= now64(3) - toIntervalSecond(10)
GROUP BY minute, slo_class, operation, transport, traffic_class, org;
