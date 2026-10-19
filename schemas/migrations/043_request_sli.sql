-- The SLI columns of a request's ledger row (server/observability/sli.py): the
-- transport that served it, its SLO class, whether a failure was the caller's
-- (`client`) or the server's (`server`), and the latency budget it was held to
-- (budgets.effective_budget_ms, 0 = none). '' and 0 on rows written before them.
-- `ingested_at` is when ClickHouse took the row, never sent by a writer: the
-- rollup below resumes from it rather than from `ts`, so a row whose flush was
-- delayed still lands in its own minute.
ALTER TABLE loom.agent_events
    ADD COLUMN IF NOT EXISTS `transport` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `slo_class` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `status_class` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `budget_ms` UInt32 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS `ingested_at` DateTime64(3) DEFAULT now64(3),
    ADD INDEX IF NOT EXISTS ingested_at_idx ingested_at TYPE minmax GRANULARITY 1;

-- Telemetry Loom derives from its own tables lives outside `loom`.
CREATE DATABASE IF NOT EXISTS loom_telemetry;

-- Requests per minute, SLO class, operation and tenant, for SLO dashboards and
-- alert rules to read, so an evaluation scans minutes rather than the ledger.
-- `rate_limited` is among `client_errors`; `slow` counts `ok` requests over
-- their budget; `latency` holds the quantile state of the `ok` requests (a
-- success the server answered unverified is a server error, not among them);
-- `ingested_hwm` is set on the refresh's sentinel row only (see
-- below), where the next refresh resumes.
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
    `ingested_hwm` SimpleAggregateFunction(max, DateTime64(3)) DEFAULT toDateTime64(0, 3)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (slo_class, minute, operation, transport, traffic_class, org)
TTL minute + toIntervalDay(400)
SETTINGS ttl_only_drop_parts = 1, non_replicated_deduplication_window = 1000;

-- Folds the ledger in once a minute. Each row is folded once, by when
-- ClickHouse took it (`ingested_at`), into the minute of its `ts`, so a row
-- whose flush was delayed still counts.
--  * `folded` is where the last refresh stopped, kept in a sentinel row: every
--    key '' but the day and every count 0, so a day's sentinels merge into one
--    row, reading them is a range read on the sort key, and a reader's sums
--    are unchanged by them. (`HAVING`: an aggregate with no GROUP BY returns
--    a row even when WHERE keeps none.)
--  * A refresh folds up to `fold_to`: at most an hour past the first row not
--    folded yet, so catching up after a stall is many small inserts (an hour
--    stays one insert block up to about 17k active operation, transport,
--    traffic class and org combinations a minute) rather than one large one
--    that could commit partly; with nothing left to fold, straight to the
--    margin. Never later than 70 s ago, longer than a ledger insert may run
--    (LOOM_CH_TIMEOUT_WRITE_S, 60 s), so a slow insert is not passed over
--    before it commits.
-- Rows with a `ts` over 31 days old, past every SLO window, are left out,
-- which keeps the read to the newest partitions. Rows with no SLO class (the
-- `/v1/me/*` routes, and MCP calls that name no registered op, such as
-- initialize) are left out.
CREATE MATERIALIZED VIEW IF NOT EXISTS loom_telemetry.sli_minute_refresh
REFRESH EVERY 1 MINUTE APPEND TO loom_telemetry.sli_minute
DEFINER = CURRENT_USER SQL SECURITY DEFINER
AS WITH
    greatest(
        (SELECT max(ingested_hwm) FROM loom_telemetry.sli_minute
         WHERE slo_class = '' AND minute >= toStartOfMinute(now() - toIntervalDay(31))),
        now64(3) - toIntervalDay(31)) AS folded,
    (SELECT min(ingested_at) FROM loom.agent_events
     WHERE kind = 'request' AND slo_class != '' AND ts >= now64(3) - toIntervalDay(31)
       AND ingested_at > folded) AS first_unfolded,
    least(if(first_unfolded > folded, first_unfolded + toIntervalHour(1), now64(3)),
          now64(3) - toIntervalSecond(70)) AS fold_to
SELECT
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
    countIf(status_class = 'ok' AND budget_ms > 0 AND latency_ms > budget_ms) AS slow,
    quantilesTDigestStateIf(0.5, 0.95, 0.99)(latency_ms, status_class = 'ok') AS latency,
    toDateTime64(0, 3) AS ingested_hwm
FROM loom.agent_events
WHERE kind = 'request' AND slo_class != '' AND ts >= now64(3) - toIntervalDay(31)
  AND ingested_at > folded AND ingested_at <= fold_to
GROUP BY minute, slo_class, operation, transport, traffic_class, org
UNION ALL
SELECT
    toStartOfDay(now()) AS minute,
    '' AS slo_class,
    '' AS operation,
    '' AS transport,
    '' AS traffic_class,
    '' AS org,
    0 AS requests,
    0 AS server_errors,
    0 AS client_errors,
    0 AS rate_limited,
    0 AS slow,
    quantilesTDigestStateIf(0.5, 0.95, 0.99)(toUInt32(0), false) AS latency,
    fold_to AS ingested_hwm
HAVING fold_to > folded
-- One insert stream, so the sentinel and the rows it vouches for are squashed
-- into the same parts rather than written by separate threads, in blocks cut
-- by rows alone (a busy minute's latency states must not split an hour by
-- bytes); and no block deduplication, which on a replicated table could drop
-- a previous-month fold identical to an earlier one (it has no sentinel).
SETTINGS max_insert_threads = 1, min_insert_block_size_bytes = 0, insert_deduplicate = 0;
