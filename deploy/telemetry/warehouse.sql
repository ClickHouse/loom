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
    user_domains Array(Tuple(domain String, user_count UInt64)),
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
