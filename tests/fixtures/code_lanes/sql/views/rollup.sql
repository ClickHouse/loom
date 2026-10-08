-- A ClickHouse table, the materialized view that feeds its rollup, a plain
-- view over the rollup and a CTE reading both.
CREATE TABLE events
(
    ts DateTime,
    user_id UInt64,
    kind LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (user_id, ts);

CREATE TABLE events_daily
(
    day Date,
    kind LowCardinality(String),
    n UInt64
)
ENGINE = SummingMergeTree
ORDER BY (day, kind);

CREATE MATERIALIZED VIEW events_daily_mv TO events_daily AS
SELECT toDate(ts) AS day, kind, count() AS n
FROM events
GROUP BY day, kind;

CREATE VIEW events_recent AS
SELECT day, kind, n FROM events_daily WHERE day >= today() - 7;

WITH top AS (SELECT kind, sum(n) AS total FROM events_recent GROUP BY kind)
SELECT e.kind, count() AS raw, any(top.total) AS rolled
FROM events AS e
INNER JOIN top ON top.kind = e.kind
GROUP BY e.kind;
