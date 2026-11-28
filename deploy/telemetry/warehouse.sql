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
    active_accounts Nullable(UInt64),
    serving_processes Nullable(UInt64),
    daily_successful_requests Nullable(UInt64),
    domain_activity Array(Tuple(domain String, active_accounts UInt64, successful_requests UInt64, serving_processes Nullable(UInt64))),
    received_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(day)
ORDER BY (installation_id, window_start)
TTL received_at + INTERVAL 400 DAY
SETTINGS non_replicated_deduplication_window = 10000;

ALTER TABLE loom_product_analytics.adoption_reports
    ADD COLUMN IF NOT EXISTS active_accounts Nullable(UInt64),
    ADD COLUMN IF NOT EXISTS serving_processes Nullable(UInt64),
    ADD COLUMN IF NOT EXISTS daily_successful_requests Nullable(UInt64),
    ADD COLUMN IF NOT EXISTS domain_activity Array(Tuple(domain String, active_accounts UInt64, successful_requests UInt64, serving_processes Nullable(UInt64)));

-- Deduplicate windows even when a retry lands after the insert deduplication window.
CREATE OR REPLACE VIEW loom_product_analytics.loom_adoption_windows AS
SELECT * EXCEPT (received_at) FROM loom_product_analytics.adoption_reports FINAL;

CREATE TABLE IF NOT EXISTS loom_product_analytics.adoption_domain_snapshots
(
    schema_version UInt8,
    installation_id String,
    day Date,
    observed_at DateTime('UTC'),
    loom_version LowCardinality(String),
    clickhouse_version LowCardinality(String),
    domain_activity Array(Tuple(domain String, active_accounts UInt64, successful_requests UInt64, serving_processes Nullable(UInt64)))
)
ENGINE = ReplacingMergeTree(observed_at)
ORDER BY (day, installation_id)
TTL observed_at + INTERVAL 2 DAY
SETTINGS non_replicated_deduplication_window = 10000;

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

CREATE OR REPLACE VIEW loom_product_analytics.loom_completed_adoption_windows AS
SELECT *
FROM loom_product_analytics.loom_adoption_windows
WHERE window_end <= now('UTC')
    AND loom_version != '0.0.0-smoke'
    AND clickhouse_version != '0.0.0-smoke';

CREATE OR REPLACE VIEW loom_product_analytics.loom_external_adoption_windows AS
SELECT *
FROM loom_product_analytics.loom_completed_adoption_windows
WHERE installation_id NOT IN (
    SELECT installation_id FROM loom_product_analytics.loom_adoption_enrollments WHERE is_internal = 1
);

-- Roll up the two reporting windows per day. Distinct identifier counts cannot be added.
CREATE OR REPLACE VIEW loom_product_analytics.loom_adoption_daily AS
WITH
    schema_version = 3 AND window_end = toDateTime(day + 1, 'UTC') AS complete_day,
    sum(report.successful_requests) AS window_successes,
    greatest(window_successes, ifNull(maxOrNullIf(daily_successful_requests, complete_day), 0)) AS daily_successes
SELECT
    installation_id,
    day,
    argMax(loom_version, window_start) AS loom_version,
    argMax(clickhouse_version, window_start) AS clickhouse_version,
    sum(report.requests) + toUInt64(daily_successes - window_successes) AS requests,
    daily_successes AS successful_requests,
    sum(server_errors) AS server_errors,
    max(total_namespaces) AS total_namespaces,
    max(active_orgs) AS active_orgs,
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
    sum(embedding_tokens) AS embedding_tokens,
    groupBitOr(toUInt8(if(window_start = toDateTime(day, 'UTC'), 1, 2))) AS report_windows,
    maxOrNullIf(active_accounts, complete_day) AS active_accounts,
    maxOrNullIf(serving_processes, complete_day) AS serving_processes
FROM loom_product_analytics.loom_completed_adoption_windows AS report
GROUP BY installation_id, day;

CREATE OR REPLACE VIEW loom_product_analytics.loom_external_adoption_daily AS
SELECT *
FROM loom_product_analytics.loom_adoption_daily
WHERE installation_id NOT IN (
    SELECT installation_id FROM loom_product_analytics.loom_adoption_enrollments WHERE is_internal = 1
);

-- Complete-day reports can cover the same requests as earlier half-day reports.
CREATE OR REPLACE VIEW loom_product_analytics.loom_adoption_domain_daily AS
WITH schema_version = 3 AND window_end = toDateTime(day + 1, 'UTC') AND daily_successful_requests IS NOT NULL AS complete_day
SELECT
    installation_id,
    day,
    counts.domain AS domain,
    greatest(
        sumIf(counts.successful_requests, schema_version IN (1, 2)),
        maxIf(counts.successful_requests, complete_day)
    ) AS requests,
    argMax(counts.user_count, window_start) AS accounts,
    greatest(
        sumIf(counts.successful_requests, schema_version = 2),
        maxIf(counts.successful_requests, complete_day)
    ) AS disclosed_requests,
    groupBitOrIf(
        toUInt8(multiIf(complete_day, 3, window_start = toDateTime(day, 'UTC'), 1, 2)),
        schema_version = 2 OR complete_day
    ) AS disclosed_windows,
    countIf(schema_version = 2 OR complete_day) > 0 AS disclosure_reports
FROM loom_product_analytics.loom_external_adoption_windows
LEFT ARRAY JOIN user_domains AS counts
GROUP BY installation_id, day, domain;

-- Attributed request and account counts stay in the restricted domain view.
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

-- A completed day takes precedence over its partial snapshots.
CREATE OR REPLACE VIEW loom_product_analytics.loom_adoption_live_domains AS
SELECT day, installation_id, loom_version, domain_activity
FROM loom_product_analytics.adoption_domain_snapshots FINAL
WHERE day BETWEEN toDate(now('UTC')) - 1 AND toDate(now('UTC'))
    AND observed_at BETWEEN now('UTC') - INTERVAL 2 HOUR AND now('UTC')
    AND loom_version != '0.0.0-smoke'
    AND clickhouse_version != '0.0.0-smoke'
    AND installation_id NOT IN (
        SELECT installation_id FROM loom_product_analytics.loom_adoption_enrollments WHERE is_internal = 1
    )
    AND (installation_id, day) NOT IN (
        SELECT installation_id, day FROM loom_product_analytics.loom_external_adoption_windows
        WHERE schema_version = 3 AND window_end = toDateTime(day + 1, 'UTC')
            AND day >= toDate(now('UTC')) - 1
    );

-- Only reports covered by the public domain-sharing notice supply names.
CREATE OR REPLACE VIEW loom_product_analytics.loom_adoption_disclosed_domains AS
WITH toDate(now('UTC')) AS today_utc
SELECT
    domain,
    sumIf(disclosed_requests, day >= today_utc - 30) AS successful_requests_30d,
    sum(disclosed_requests) AS successful_requests_90d
FROM (
    SELECT day, installation_id, domain, max(attributed_requests) AS disclosed_requests
    FROM (
        SELECT day, installation_id, domain, disclosed_requests AS attributed_requests
        FROM loom_product_analytics.loom_adoption_domain_daily
        WHERE day BETWEEN today_utc - 90 AND today_utc AND disclosed_requests > 0
        UNION ALL
        SELECT day, installation_id, counts.domain, counts.successful_requests
        FROM loom_product_analytics.loom_adoption_live_domains
        ARRAY JOIN domain_activity AS counts
    )
    GROUP BY day, installation_id, domain
)
GROUP BY domain;

CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_domains
SQL SECURITY DEFINER AS
SELECT domain FROM loom_product_analytics.loom_adoption_disclosed_domains;

-- Publish names in activity order. The attributed counts stay in the warehouse.
CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_domain_directories
SQL SECURITY DEFINER AS
SELECT directory.1 AS range_days, directory.2 AS domains
FROM (
    SELECT
        arrayMap(entry -> entry.2, arraySort(groupArrayIf(
            (-toInt128(successful_requests_30d), domain), successful_requests_30d > 0
        ))) AS domains_30d,
        arrayMap(entry -> entry.2, arraySort(groupArray((-toInt128(successful_requests_90d), domain)))) AS domains_90d
    FROM loom_product_analytics.loom_adoption_disclosed_domains
)
ARRAY JOIN [(30, domains_30d), (90, domains_90d)] AS directory;

-- Publish daily counts from complete reports and the latest snapshots.
CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_domain_accounts
SQL SECURITY DEFINER AS
WITH
    toDate(now('UTC')) AS today_utc,
    daily.day >= today_utc - range_days AS in_window
SELECT
    daily.domain AS domain,
    range_days,
    argMaxIf(daily.active_accounts, daily.day, in_window) AS active_accounts,
    tupleElement(argMaxIf(tuple(daily.serving_processes), daily.day, in_window), 1) AS serving_processes,
    maxIf(daily.day, in_window) AS day,
    sumIf(daily.successful_requests, in_window) AS successful_requests,
    uniqExactArrayIf(daily.active_installation_ids, in_window) AS active_installations,
    argMaxOrNullIf(daily.loom_version, daily.day, in_window) AS loom_version,
    minOrNullIf(daily.day, daily.successful_requests > 0) AS first_seen,
    maxOrNullIf(daily.day, daily.successful_requests > 0 AND in_window) AS last_seen
FROM (
    SELECT
        counts.domain AS domain,
        day,
        sum(counts.active_accounts) AS active_accounts,
        sumOrNull(counts.serving_processes) AS serving_processes,
        sum(counts.successful_requests) AS successful_requests,
        groupUniqArrayIf(installation_id, counts.successful_requests > 0) AS active_installation_ids,
        argMaxOrNullIf(loom_version, installation_id, counts.successful_requests > 0) AS loom_version
    FROM (
        SELECT day, installation_id, loom_version,
               arrayFilter(count -> has(arrayMap(item -> item.domain, user_domains), count.domain),
                           domain_activity) AS domain_activity
        FROM loom_product_analytics.loom_external_adoption_windows
        WHERE schema_version = 3 AND day < today_utc AND window_end = toDateTime(day + 1, 'UTC')
        UNION ALL
        SELECT day, installation_id, loom_version, domain_activity
        FROM loom_product_analytics.loom_adoption_live_domains
    )
    ARRAY JOIN domain_activity AS counts
    GROUP BY domain, day
) AS daily
INNER JOIN loom_product_analytics.loom_adoption_disclosed_domains AS disclosed USING (domain)
ARRAY JOIN [30, 90] AS range_days
WHERE range_days = 90 OR disclosed.successful_requests_30d > 0
GROUP BY domain, range_days
HAVING countIf(in_window) > 0;

CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_company_activity
SQL SECURITY DEFINER AS
WITH toDate(now('UTC')) AS today_utc
SELECT
    grain,
    multiIf(grain = 'day', day, grain = 'week', toStartOfWeek(day, 1), toStartOfMonth(day)) AS period,
    multiIf(grain = 'day', addDays(period, 1), grain = 'week', addWeeks(period, 1), addMonths(period, 1)) AS period_end,
    uniqExactIf(domain, disclosed_requests > 0) AS companies,
    uniqExact(day) AS days_reported,
    uniqExactIf(day, day_windows = 3) AS complete_days_reported
FROM (
    SELECT day, domain, disclosed_requests, groupBitOr(disclosed_windows) OVER (PARTITION BY day) AS day_windows
    FROM loom_product_analytics.loom_adoption_domain_daily
    WHERE disclosure_reports AND day >= today_utc - 365 AND day < today_utc
)
ARRAY JOIN ['day', 'week', 'month'] AS grain
GROUP BY grain, period;

-- The public website reads these aggregate views with a separate read-only user.
CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_totals
SQL SECURITY DEFINER AS
WITH toDate(now('UTC')) AS today_utc
SELECT
    range_days,
    arrayJoin(multiIf(
        daily.day = today_utc, ['current'],
        daily.day >= today_utc - range_days, ['current', 'current_complete'],
        ['previous_complete']
    )) AS comparison,
    uniqExactIf(daily.day, daily.day < today_utc) AS days_reported,
    max(daily.day) AS last_reported,
    uniqExact(daily.installation_id) AS reporting,
    uniqExactIf(daily.installation_id, daily.successful_requests > 0) AS active,
    sum(daily.successful_requests) AS operations,
    sum(daily.requests) AS requests,
    sum(daily.server_errors) AS server_errors,
    quantileExactOrNullIf(0.5)(daily.total_namespaces, daily.successful_requests > 0) AS median_total_namespaces,
    quantileExactOrNullIf(0.5)(daily.active_orgs, daily.successful_requests > 0) AS median_active_orgs,
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
ARRAY JOIN [30, 90, 365] AS range_days
WHERE daily.day >= today_utc - 2 * range_days AND daily.day <= today_utc
GROUP BY range_days, comparison;

CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_versions
SQL SECURITY DEFINER AS
-- Keep the saved Query API's sort field during a warehouse rollout.
SELECT loom_version, clickhouse_version, count() AS active_installations, active_installations AS installations
FROM (
    SELECT installation_id,
           argMax(loom_version, day) AS loom_version,
           argMax(clickhouse_version, day) AS clickhouse_version
    FROM loom_product_analytics.loom_external_adoption_daily
    WHERE day >= toDate(now('UTC')) - 30 AND day < toDate(now('UTC')) AND successful_requests > 0
    GROUP BY installation_id
)
GROUP BY loom_version, clickhouse_version;

CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_first_observation
SQL SECURITY DEFINER AS
SELECT day, loom_version, clickhouse_version
FROM loom_product_analytics.loom_external_adoption_windows
ORDER BY window_start, installation_id
LIMIT 1;

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
      ('crates.io', 'clickhouse-loom', 'sdk_downloads'),
      ('ghcr', 'ghcr.io/clickhouse/loom', 'container_pulls_total')
  );

-- Count an installation once at its first successful request in retained reports.
CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_growth
SQL SECURITY DEFINER AS
WITH toDate(now('UTC')) AS today_utc
SELECT
    grain,
    period,
    multiIf(grain = 'day', addDays(period, 1), grain = 'week', addWeeks(period, 1), addMonths(period, 1)) AS period_end,
    first_active,
    sum(first_active) OVER (PARTITION BY grain ORDER BY period ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        AS cumulative_active
FROM (
    SELECT
        grain,
        multiIf(grain = 'day', observed_day, grain = 'week', toStartOfWeek(observed_day, 1), toStartOfMonth(observed_day)) AS period,
        countIf(observed_day = first_active_day) AS first_active
    FROM (
        SELECT
            minOrNullIf(day, successful_requests > 0) AS first_active_day,
            groupArray(day) AS report_days
        FROM loom_product_analytics.loom_external_adoption_daily
        WHERE day < today_utc
        GROUP BY installation_id
    )
    ARRAY JOIN report_days AS observed_day
    ARRAY JOIN ['day', 'week', 'month'] AS grain
    GROUP BY grain, period
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
    uniqExactIf(day, day_windows = 3) AS complete_days_reported,
    multiIf(grain = 'day', addDays(period, 1), grain = 'week', addWeeks(period, 1), addMonths(period, 1)) AS period_end,
    period >= first_report AS forecast_eligible,
    sum(requests) AS requests,
    sum(server_errors) AS server_errors,
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
    sum(embedding_tokens) AS embedding_tokens,
    quantileExactOrNullIf(0.5)(total_namespaces, successful_requests > 0) AS median_total_namespaces,
    quantileExactOrNullIf(0.5)(active_orgs, successful_requests > 0) AS median_active_orgs,
    quantileExactOrNullIf(0.5)(active_namespaces, successful_requests > 0) AS median_namespaces,
    quantileExactOrNullIf(0.5)(active_agents, successful_requests > 0) AS median_agents,
    quantileExactOrNullIf(0.5)(active_sessions, successful_requests > 0) AS median_sessions,
    maxOrNull(daily_active_accounts) AS active_accounts,
    uniqExactIf(day, daily_active_accounts IS NOT NULL) AS accounts_days_reported,
    argMaxIf(daily_account_installations, tuple(daily_active_accounts, day), daily_active_accounts IS NOT NULL)
        AS account_reporting_installations,
    maxOrNull(daily_serving_processes) AS serving_processes,
    uniqExactIf(day, daily_serving_processes IS NOT NULL) AS processes_days_reported,
    argMaxIf(daily_process_installations, tuple(daily_serving_processes, day), daily_serving_processes IS NOT NULL)
        AS process_reporting_installations
FROM (
    SELECT
        *,
        groupBitOr(report_windows) OVER (PARTITION BY day) AS day_windows,
        sumOrNull(active_accounts) OVER (PARTITION BY day) AS daily_active_accounts,
        count(active_accounts) OVER (PARTITION BY day) AS daily_account_installations,
        sumOrNull(serving_processes) OVER (PARTITION BY day) AS daily_serving_processes,
        count(serving_processes) OVER (PARTITION BY day) AS daily_process_installations
    FROM loom_product_analytics.loom_external_adoption_daily
    WHERE day >= today_utc - 365 AND day < today_utc
)
ARRAY JOIN ['day', 'week', 'month'] AS grain
GROUP BY grain, period;

CREATE OR REPLACE VIEW loom_product_analytics.public_adoption_lifecycle
SQL SECURITY DEFINER AS
WITH
    toDate(now('UTC')) AS today_utc,
    report_coverage AS (
        SELECT
            day, window_start, schema_version,
            schema_version = 3 AND window_end = toDateTime(day + 1, 'UTC')
                AND daily_successful_requests IS NOT NULL AS complete_day
        FROM loom_product_analytics.loom_external_adoption_windows
        WHERE day >= today_utc - 400 AND day < today_utc
    ),
    complete_periods AS (
        SELECT
            entity, grain,
            if(grain = 'week', toStartOfWeek(day, 1), toStartOfMonth(day)) AS bucket,
            if(grain = 'week', addWeeks(bucket, 1), addMonths(bucket, 1)) AS next_bucket
        FROM report_coverage
        ARRAY JOIN if(schema_version = 2 OR complete_day, ['deployment', 'domain'], ['deployment']) AS entity
        ARRAY JOIN ['week', 'month'] AS grain
        GROUP BY entity, grain, bucket
        HAVING next_bucket <= today_utc
            AND uniqExactArray(if(complete_day, [toDateTime(day, 'UTC'), window_start], [window_start]))
                = 2 * dateDiff('day', bucket, next_bucket)
    ),
    comparable_periods AS (
        SELECT entity, grain, bucket
        FROM (
            SELECT entity, grain, bucket, lagInFrame(next_bucket) OVER (
                PARTITION BY entity, grain ORDER BY bucket
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) AS previous_end
            FROM complete_periods
        )
        WHERE previous_end = bucket AND bucket >= today_utc - 365
    ),
    entity_activity AS (
        SELECT 'deployment' AS entity, installation_id AS identity, day, successful_requests AS requests
        FROM loom_product_analytics.loom_external_adoption_daily
        UNION ALL
        SELECT 'domain' AS entity, domain AS identity, day, sum(disclosed_requests) AS requests
        FROM loom_product_analytics.loom_adoption_domain_daily
        WHERE disclosed_requests > 0
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
        HAVING sum(requests) > if(grain = 'week', intDiv(min_requests * 7, 30), min_requests)
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
    ),
    counts AS (
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
        GROUP BY entity, grain, min_requests, bucket
    )
SELECT periods.entity, periods.grain, periods.min_requests, periods.bucket,
       ifNull(counts.new, 0) AS new,
       ifNull(counts.retained, 0) AS retained,
       ifNull(counts.reactivated, 0) AS reactivated,
       ifNull(counts.inactive, 0) AS inactive,
       toUInt8(1) AS complete_coverage
FROM (
    SELECT grain, bucket, entity, min_requests FROM comparable_periods
    ARRAY JOIN [0, 50000, 1000000] AS min_requests
) AS periods
LEFT JOIN counts USING (entity, grain, min_requests, bucket);
