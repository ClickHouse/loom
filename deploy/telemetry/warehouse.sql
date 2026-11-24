-- Run on the analytics ClickHouse service.
CREATE DATABASE IF NOT EXISTS loom_product_analytics;

CREATE TABLE IF NOT EXISTS loom_product_analytics.adoption_reports
(
    installation_id String,
    window_start DateTime('UTC'),
    window_end DateTime('UTC'),
    day Date,
    schema_version UInt8,
    loom_version LowCardinality(String),
    clickhouse_version LowCardinality(String),
    total_namespaces UInt64,
    requests UInt64,
    successful_requests UInt64,
    server_errors UInt64,
    active_orgs UInt64,
    active_namespaces UInt64,
    active_agents UInt64,
    active_sessions UInt64,
    memory_requests UInt64,
    code_requests UInt64,
    knowledge_requests UInt64,
    conversation_requests UInt64,
    tracker_requests UInt64,
    task_requests UInt64,
    mcp_requests UInt64,
    http_requests UInt64,
    llm_calls UInt64,
    input_tokens UInt64,
    output_tokens UInt64,
    embedding_tokens UInt64,
    user_domains Array(Tuple(domain String, user_count UInt64, successful_requests UInt64)),
    received_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(day)
ORDER BY (installation_id, window_start)
TTL received_at + INTERVAL 400 DAY
SETTINGS non_replicated_deduplication_window = 10000;

-- Deduplicate windows even when a retry lands after the insert deduplication window.
CREATE VIEW IF NOT EXISTS loom_product_analytics.loom_adoption_windows AS
SELECT * EXCEPT (received_at) FROM loom_product_analytics.adoption_reports FINAL;

CREATE TABLE IF NOT EXISTS loom_product_analytics.adoption_enrollments
(
    installation_id String,
    company_id String,
    company_name String,
    is_internal UInt8,
    managed_stage UInt8,
    recorded_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(recorded_at)
ORDER BY installation_id;

CREATE VIEW IF NOT EXISTS loom_product_analytics.loom_adoption_enrollments AS
SELECT * EXCEPT (recorded_at) FROM loom_product_analytics.adoption_enrollments FINAL;

CREATE TABLE IF NOT EXISTS loom_product_analytics.oss_metrics
(
    day Date,
    source LowCardinality(String),
    artifact String,
    metric LowCardinality(String),
    value UInt64,
    recorded_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(recorded_at)
ORDER BY (day, source, artifact, metric);

CREATE VIEW IF NOT EXISTS loom_product_analytics.loom_oss_metrics_daily AS
SELECT * EXCEPT (recorded_at) FROM loom_product_analytics.oss_metrics FINAL;

-- Roll up the two reporting windows per day. Distinct identifier counts cannot be added.
CREATE VIEW IF NOT EXISTS loom_product_analytics.loom_adoption_daily AS
SELECT
    installation_id,
    day,
    argMax(loom_version, window_start) AS loom_version,
    argMax(clickhouse_version, window_start) AS clickhouse_version,
    sum(requests) AS requests,
    sum(successful_requests) AS successful_requests,
    sum(server_errors) AS server_errors,
    max(active_namespaces) AS active_namespaces,
    max(active_agents) AS active_agents,
    max(active_sessions) AS active_sessions,
    sum(memory_requests) AS memory_requests,
    sum(code_requests) AS code_requests,
    sum(knowledge_requests) AS knowledge_requests,
    sum(conversation_requests) AS conversation_requests,
    sum(tracker_requests) AS tracker_requests,
    sum(task_requests) AS task_requests,
    sum(mcp_requests) AS mcp_requests,
    sum(http_requests) AS http_requests,
    sum(llm_calls) AS llm_calls,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens,
    sum(embedding_tokens) AS embedding_tokens
FROM loom_product_analytics.loom_adoption_windows
GROUP BY installation_id, day;

-- Create this view once in the warehouse to exclude registered internal and test installations.
CREATE VIEW IF NOT EXISTS loom_product_analytics.loom_external_adoption_daily AS
SELECT *
FROM loom_product_analytics.loom_adoption_daily
WHERE installation_id NOT IN (
    SELECT installation_id FROM loom_product_analytics.loom_adoption_enrollments WHERE is_internal = 1
);

CREATE VIEW IF NOT EXISTS loom_product_analytics.loom_adoption_domain_daily AS
SELECT
    installation_id,
    day,
    counts.domain AS domain,
    sum(counts.successful_requests) AS requests,
    argMax(counts.user_count, window_start) AS accounts
FROM loom_product_analytics.loom_adoption_windows
ARRAY JOIN user_domains AS counts
WHERE installation_id NOT IN (
    SELECT installation_id FROM loom_product_analytics.loom_adoption_enrollments WHERE is_internal = 1
)
GROUP BY installation_id, day, domain;

-- Domain attribution is available only to analysts with warehouse access.
CREATE VIEW IF NOT EXISTS loom_product_analytics.loom_adoption_top_domains_30d
SQL SECURITY DEFINER AS
SELECT domain, sum(instance_requests) AS successful_requests, countIf(instance_requests > 0) AS installations,
       sum(latest_accounts) AS accounts
FROM (
    SELECT domain, installation_id, sum(requests) AS instance_requests,
           argMax(accounts, day) AS latest_accounts
    FROM loom_product_analytics.loom_adoption_domain_daily
    WHERE day >= toDate(now('UTC')) - 30 AND day < toDate(now('UTC'))
    GROUP BY domain, installation_id
)
GROUP BY domain
HAVING successful_requests > 0
ORDER BY successful_requests DESC, domain
LIMIT 50;

-- The public website reads these aggregate views with a separate read-only user.
CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_totals
SQL SECURITY DEFINER AS
WITH toDate(now('UTC')) AS today_utc
SELECT
    range_days,
    if(daily.day >= today_utc - range_days, 'current', 'previous') AS comparison,
    uniqExact(daily.day) AS days_reported,
    max(daily.day) AS last_reported,
    uniqExact(daily.installation_id) AS reporting,
    uniqExactIf(daily.installation_id, daily.successful_requests > 0) AS active,
    uniqExactIf(enrolled.company_id, enrolled.company_id != '' AND daily.successful_requests > 0)
        AS verified_companies,
    sum(daily.successful_requests) AS operations,
    sum(daily.requests) AS requests,
    sum(daily.server_errors) AS server_errors,
    quantileExactOrNullIf(0.5)(daily.active_namespaces, daily.successful_requests > 0) AS median_namespaces,
    quantileExactOrNullIf(0.5)(daily.active_agents, daily.successful_requests > 0) AS median_agents,
    quantileExactOrNullIf(0.5)(daily.active_sessions, daily.successful_requests > 0) AS median_sessions,
    sum(daily.memory_requests) AS memory_requests,
    sum(daily.code_requests) AS code_requests,
    sum(daily.knowledge_requests) AS knowledge_requests,
    sum(daily.conversation_requests) AS conversation_requests,
    sum(daily.tracker_requests) AS tracker_requests,
    sum(daily.task_requests) AS task_requests,
    sum(daily.mcp_requests) AS mcp_requests,
    sum(daily.http_requests) AS http_requests,
    sum(daily.llm_calls) AS llm_calls,
    sum(daily.input_tokens) AS input_tokens,
    sum(daily.output_tokens) AS output_tokens,
    sum(daily.embedding_tokens) AS embedding_tokens
FROM loom_product_analytics.loom_external_adoption_daily AS daily
LEFT JOIN loom_product_analytics.loom_adoption_enrollments AS enrolled USING (installation_id)
ARRAY JOIN [30, 90, 365] AS range_days
WHERE daily.day >= today_utc - 2 * range_days AND daily.day < today_utc
GROUP BY range_days, comparison;

CREATE VIEW IF NOT EXISTS loom_product_analytics.public_adoption_versions
SQL SECURITY DEFINER AS
SELECT loom_version, clickhouse_version, count() AS installations
FROM (
    SELECT installation_id,
           argMax(loom_version, day) AS loom_version,
           argMax(clickhouse_version, day) AS clickhouse_version
    FROM loom_product_analytics.loom_external_adoption_daily
    WHERE day >= toDate(now('UTC')) - 30 AND day < toDate(now('UTC'))
    GROUP BY installation_id
)
GROUP BY loom_version, clickhouse_version;

CREATE VIEW IF NOT EXISTS loom_product_analytics.public_adoption_retention
SQL SECURITY DEFINER AS
WITH
    toStartOfWeek(toDate(now('UTC')), 1) AS current_week,
    activity AS (
        SELECT installation_id, toStartOfWeek(day, 1) AS week
        FROM loom_product_analytics.loom_external_adoption_daily
        WHERE day < current_week AND successful_requests > 0
        GROUP BY installation_id, week
    ),
    first_seen AS (
        SELECT installation_id, min(week) AS first_week FROM activity GROUP BY installation_id
    ),
    observations AS (
        SELECT installation_id, first_week, age, first_week + toIntervalWeek(age) AS observed_week
        FROM first_seen
        ARRAY JOIN range(1, 5) AS age
        WHERE first_week >= current_week - INTERVAL 13 WEEK AND observed_week < current_week
    )
SELECT observations.age AS age, count() AS eligible, countIf(activity.week IS NOT NULL) AS active
FROM observations
LEFT JOIN activity
    ON observations.installation_id = activity.installation_id
    AND observations.observed_week = activity.week
GROUP BY observations.age
SETTINGS join_use_nulls = 1;

CREATE OR REPLACE VIEW loom_product_analytics.public_oss_adoption
SQL SECURITY DEFINER AS
SELECT day, source, artifact, metric, value
FROM loom_product_analytics.loom_oss_metrics_daily
WHERE day >= toDate(now('UTC')) - 730 AND day < toDate(now('UTC'))
  AND (metric = 'sdk_downloads' OR day >= toDate(now('UTC')) - 400)
  AND (source, artifact, metric) IN (
      ('github', 'ClickHouse/loom', 'stars'),
      ('github', 'ClickHouse/loom', 'forks'),
      ('github', 'ClickHouse/loom', 'release_asset_downloads_total'),
      ('pypi', 'clickhouse-loom', 'sdk_downloads'),
      ('npm', '@clickhouse/loom', 'sdk_downloads'),
      ('ghcr', 'ghcr.io/clickhouse/loom', 'container_pulls_total')
  );

CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_activity
SQL SECURITY DEFINER AS
WITH
    toDate(now('UTC')) AS today_utc,
    (SELECT minOrNull(day)
     FROM loom_product_analytics.loom_external_adoption_daily WHERE day < today_utc) AS first_report
SELECT
    grain,
    multiIf(grain = 'day', day, grain = 'week', toStartOfWeek(day, 1), toStartOfMonth(day)) AS period,
    uniqExact(installation_id) AS reporting,
    uniqExactIf(installation_id, successful_requests > 0) AS active,
    sum(successful_requests) AS operations,
    uniqExact(day) AS days_reported,
    multiIf(grain = 'day', addDays(period, 1), grain = 'week', addWeeks(period, 1), addMonths(period, 1)) AS period_end,
    period >= first_report AS forecast_eligible
FROM loom_product_analytics.loom_external_adoption_daily
ARRAY JOIN ['day', 'week', 'month'] AS grain
WHERE day >= today_utc - 365 AND day < today_utc
GROUP BY grain, period
HAVING period >= greatest(first_report, today_utc - 365) OR period_end > today_utc;

CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_lifecycle
SQL SECURITY DEFINER AS
WITH
    toDate(now('UTC')) AS today_utc,
    entity_activity AS (
        SELECT 'deployment' AS entity, installation_id AS identity, day, successful_requests AS requests
        FROM loom_product_analytics.loom_external_adoption_daily
        UNION ALL
        SELECT 'domain' AS entity, domain AS identity, day, sum(requests) AS requests
        FROM loom_product_analytics.loom_adoption_domain_daily
        GROUP BY domain, day
    ),
    active_periods AS (
        SELECT
            entity, identity, grain, min_requests,
            if(grain = 'week', toStartOfWeek(day, 1), toStartOfMonth(day)) AS period
        FROM entity_activity
        ARRAY JOIN ['week', 'month'] AS grain
        ARRAY JOIN [0, 50000, 1000000] AS min_requests
        WHERE day < today_utc
        GROUP BY entity, identity, grain, min_requests, period
        HAVING sum(requests) > min_requests
           AND if(grain = 'week', addWeeks(period, 1), addMonths(period, 1)) <= today_utc
    ),
    transitions AS (
        SELECT
            entity, grain, min_requests, period,
            lagInFrame(period) OVER ordered AS previous,
            leadInFrame(period) OVER ordered AS following,
            if(grain = 'week', addWeeks(period, -1), addMonths(period, -1)) AS previous_period,
            if(grain = 'week', addWeeks(period, 1), addMonths(period, 1)) AS next_period
        FROM active_periods
        WINDOW ordered AS (
            PARTITION BY entity, identity, grain, min_requests ORDER BY period
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        )
    )
SELECT
    entity, grain, min_requests,
    if(state = 'inactive', next_period, period) AS bucket,
    countIf(state = 'new') AS new,
    countIf(state = 'retained') AS retained,
    countIf(state = 'reactivated') AS reactivated,
    countIf(state = 'inactive') AS inactive
FROM transitions
ARRAY JOIN arrayConcat(
    [multiIf(previous = toDate(0), 'new', previous = previous_period, 'retained', 'reactivated')],
    if(following != next_period, ['inactive'], [])
) AS state
WHERE bucket >= today_utc - 365
  AND if(grain = 'week', addWeeks(bucket, 1), addMonths(bucket, 1)) <= today_utc
GROUP BY entity, grain, min_requests, bucket;
