-- Run each SELECT in its own Hex cell against the analytics warehouse.
-- loom_adoption_windows has one validated version-1 report per installation and window.
-- loom_adoption_enrollments has one row per enrolled installation. See docs/features/observability/telemetry.md.

-- Roll up the two reporting windows per day. Distinct identifier counts cannot be added.
CREATE VIEW IF NOT EXISTS loom_adoption_daily AS
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
FROM loom_adoption_windows
GROUP BY installation_id, day;

-- Create this view once in the warehouse to exclude registered internal and test installations.
CREATE VIEW IF NOT EXISTS loom_external_adoption_daily AS
SELECT *
FROM loom_adoption_daily
WHERE installation_id NOT IN (
    SELECT installation_id FROM loom_adoption_enrollments WHERE is_internal = 1
);

-- Reporting coverage, measured adoption, and successful request volume.
SELECT
    day,
    uniqExact(installation_id) AS reporting_installations,
    uniqExactIf(installation_id, daily.successful_requests > 0) AS active_installations,
    sum(daily.requests) AS requests,
    sum(daily.successful_requests) AS successful_requests,
    sum(daily.server_errors) AS server_errors,
    ifNotFinite(quantileIf(0.5)(active_namespaces, daily.successful_requests > 0), 0)
        AS median_active_namespaces,
    ifNotFinite(quantileIf(0.5)(active_agents, daily.successful_requests > 0), 0)
        AS median_active_agents,
    ifNotFinite(quantileIf(0.5)(active_sessions, daily.successful_requests > 0), 0)
        AS median_active_sessions
FROM loom_external_adoption_daily AS daily
GROUP BY day
ORDER BY day;

-- Usage by feature and integration. Feature and transport totals are separate series.
SELECT
    toStartOfWeek(day, 1) AS week,
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
FROM loom_external_adoption_daily
GROUP BY week
ORDER BY week;

-- Distribution of the latest reported versions per installation.
SELECT loom_version, clickhouse_version, count() AS installations
FROM (
    SELECT
        installation_id,
        argMax(loom_version, day) AS loom_version,
        argMax(clickhouse_version, day) AS clickhouse_version
    FROM loom_external_adoption_daily
    WHERE day >= toDate(now('UTC')) - 30
    GROUP BY installation_id
)
GROUP BY loom_version, clickhouse_version
ORDER BY installations DESC, loom_version, clickhouse_version;

-- Cohorts by first successful operation. Include only complete UTC weeks.
WITH
    toStartOfWeek(toDate(now('UTC')), 1) AS current_week,
    weekly AS (
        SELECT
            installation_id,
            toStartOfWeek(day, 1) AS week,
            countIf(successful_requests > 0) AS active_days
        FROM loom_external_adoption_daily
        WHERE day < current_week
        GROUP BY installation_id, week
    ),
    cohorts AS (
        SELECT installation_id, minIf(week, active_days > 0) AS cohort_week
        FROM weekly
        GROUP BY installation_id
        HAVING countIf(active_days > 0) > 0
    ),
    observations AS (
        SELECT
            installation_id,
            cohort_week,
            cohort_week + toIntervalWeek(week_offset) AS observed_week
        FROM cohorts
        ARRAY JOIN range(5) AS week_offset
        WHERE observed_week < current_week
    )
SELECT
    cohort_week,
    dateDiff('week', cohort_week, observed_week) AS weeks_since_activation,
    count() AS cohort_installations,
    countIf(weekly.week IS NOT NULL) AS reporting_installations,
    countIf(weekly.active_days > 0) AS active_installations
FROM observations
LEFT JOIN weekly
    ON observations.installation_id = weekly.installation_id
    AND observations.observed_week = weekly.week
GROUP BY cohort_week, observed_week
ORDER BY cohort_week, observed_week
SETTINGS join_use_nulls = 1;

-- Domains from each installation's latest report. A domain is a lead for verification.
SELECT
    domain_count.domain AS domain,
    count() AS reporting_installations,
    sum(domain_count.user_count) AS reported_accounts
FROM (
    SELECT installation_id, argMax(user_domains, window_start) AS domains
    FROM loom_adoption_windows
    WHERE day >= toDate(now('UTC')) - 30
      AND installation_id NOT IN (
          SELECT installation_id FROM loom_adoption_enrollments WHERE is_internal = 1
      )
    GROUP BY installation_id
)
ARRAY JOIN domains AS domain_count
GROUP BY domain
ORDER BY reporting_installations DESC, reported_accounts DESC, domain;

-- Opted-in companies, their recent usage, and their highest recorded pilot stage.
-- Stage 0 = no request, 1 = requested, 2 = qualified, 3 = evaluating, 4 = committed.
SELECT
    enrollment.company_id,
    any(enrollment.company_name) AS company_name,
    uniqExact(enrollment.installation_id) AS enrolled_installations,
    uniqExactIf(enrollment.installation_id, daily.day IS NOT NULL) AS reporting_installations_30d,
    uniqExactIf(enrollment.installation_id, daily.successful_requests > 0) AS active_installations_30d,
    sum(ifNull(daily.successful_requests, 0)) AS successful_requests_30d,
    max(enrollment.managed_stage) AS managed_stage
FROM loom_adoption_enrollments AS enrollment
LEFT JOIN (
    SELECT installation_id, day, successful_requests
    FROM loom_adoption_daily
    WHERE day >= toDate(now('UTC')) - 30
) AS daily ON enrollment.installation_id = daily.installation_id
WHERE enrollment.company_id != '' AND enrollment.is_internal = 0
GROUP BY enrollment.company_id
ORDER BY successful_requests_30d DESC, enrollment.company_id
SETTINGS join_use_nulls = 1;
