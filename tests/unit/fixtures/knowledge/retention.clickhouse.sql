-- Raw events are kept for 30 days.
CREATE TABLE analytics.events
(
    id UInt64,
    ts DateTime,
    payload String
)
ENGINE = MergeTree
ORDER BY (id, ts)
TTL ts + INTERVAL 30 DAY;

/* Daily rollup; kept for a year. */
CREATE MATERIALIZED VIEW analytics.events_daily
ENGINE = SummingMergeTree ORDER BY day
TTL day + INTERVAL 365 DAY
AS SELECT toDate(ts) AS day, count() AS n FROM analytics.events GROUP BY day;

ALTER TABLE analytics.events MODIFY TTL ts + INTERVAL 45 DAY;

SELECT 'a; b' AS s, n FROM analytics.events_daily WHERE day > today() - 7;
