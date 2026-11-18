-- Private events have their own physical store; ordinary events retain their schema
-- and values for old builds. No existing rows are moved, erased or replayed.
-- A contaminated ordinary store refuses analyst provisioning until repaired explicitly.
CREATE TABLE IF NOT EXISTS loom.agent_events_private
(
    `ts` DateTime64(3) DEFAULT now64(),
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `agent` LowCardinality(String),
    `user_id` String,
    `session_id` String,
    `run_id` String DEFAULT '',
    `operation` LowCardinality(String),
    `request_id` String,
    `trace_id` String DEFAULT '',
    `latency_ms` UInt32,
    `input_tokens` UInt32 DEFAULT 0,
    `output_tokens` UInt32 DEFAULT 0,
    `embedding_tokens` UInt32 DEFAULT 0,
    `upstream_cost_usd` Decimal(18, 6) DEFAULT 0,
    `bytes_written` UInt64 DEFAULT 0,
    `success` Bool,
    `error_code` LowCardinality(String) DEFAULT '',
    `memory_key` String DEFAULT '',
    `impersonated_by` String DEFAULT '',
    `kind` LowCardinality(String) DEFAULT '',
    `args` Map(LowCardinality(String), String) DEFAULT map(),
    `model` LowCardinality(String) DEFAULT '',
    `llm_calls` UInt16 DEFAULT 0,
    `build_commit` LowCardinality(String) DEFAULT '',
    `instance` LowCardinality(String) DEFAULT '',
    `instance_label` LowCardinality(String) DEFAULT '',
    `consumer` LowCardinality(String) DEFAULT '',
    `response_tokens` UInt32 DEFAULT 0,
    `traffic_class` LowCardinality(String) DEFAULT '',
    `subject` LowCardinality(String) DEFAULT '',
    `transport` LowCardinality(String) DEFAULT '',
    `slo_class` LowCardinality(String) DEFAULT '',
    `status_class` LowCardinality(String) DEFAULT '',
    `budget_ms` UInt32 DEFAULT 0,
    `ingested_at` DateTime64(3) DEFAULT now64(3),
    `mirror_marked` UInt8 DEFAULT 0,
    INDEX run_id_bloom_idx run_id TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX ingested_at_idx ingested_at TYPE minmax GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (org, namespace, agent, ts)
SETTINGS index_granularity = 8192, non_replicated_deduplication_window = 1000;

-- New writes cannot add private rows to storage readable by an old analyst.
-- The application verifies this CHECK on every writer and drains pre-check inserts
-- before its contamination census and before granting ordinary analyst columns.
ALTER TABLE loom.agent_events
    ADD CONSTRAINT IF NOT EXISTS ordinary_events_only
    CHECK mirror_marked = 0 AND NOT match(namespace, '^acl-[0-9a-f]{24}$');

SYSTEM PAUSE VIEW loom.agent_cost_rollup_refresh;
SYSTEM PAUSE VIEW loom.subject_last_seen_refresh;
SYSTEM WAIT VIEW loom.agent_cost_rollup_refresh;
SYSTEM WAIT VIEW loom.subject_last_seen_refresh;

ALTER TABLE loom.agent_cost_rollup_refresh MODIFY QUERY
SELECT
    org,
    namespace,
    agent,
    toDate(ts) AS day,
    operation,
    sumStateIf(upstream_cost_usd, mirror_marked = 0) AS cost_usd_state,
    sumStateIf(toUInt64(input_tokens) + toUInt64(output_tokens), mirror_marked = 0) AS tokens_state,
    sumStateIf(toUInt64(embedding_tokens), mirror_marked = 0) AS embed_tokens_state,
    countStateIf(mirror_marked = 0) AS ops_state,
    sumStateIf(toUInt64(1), mirror_marked = 0 AND success = false) AS errors_state,
    sumStateIf(toUInt64(latency_ms), mirror_marked = 0) AS latency_sum_state,
    quantilesStateIf(0.5, 0.95, 0.99)(latency_ms, mirror_marked = 0) AS latency_quantile_state,
    sumStateIf(upstream_cost_usd, mirror_marked != 0) AS marked_cost_usd_state,
    sumStateIf(toUInt64(input_tokens) + toUInt64(output_tokens), mirror_marked != 0) AS marked_tokens_state,
    sumStateIf(toUInt64(embedding_tokens), mirror_marked != 0) AS marked_embed_tokens_state,
    countStateIf(mirror_marked != 0) AS marked_ops_state,
    max(ts) AS ts_hwm
FROM (SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events UNION ALL SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events_private)
WHERE (ts > (
    SELECT max(ts_hwm)
    FROM loom.agent_cost_rollup_daily
)) AND (ts <= (now64(3) - toIntervalSecond(10)))
GROUP BY
    org,
    namespace,
    agent,
    day,
    operation;

ALTER TABLE loom.subject_last_seen_refresh MODIFY QUERY
SELECT
    org,
    subject,
    maxState(ts) AS last_seen,
    max(ts) AS ts_hwm
FROM (SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events UNION ALL SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events_private)
WHERE (subject != '') AND (ts > (
    SELECT max(ts_hwm)
    FROM loom.subject_last_seen
    WHERE (org = '') AND (subject = '')
)) AND (ts <= (now64(3) - toIntervalSecond(10)))
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
GROUP BY
    org,
    subject
UNION ALL
SELECT
    '' AS org,
    '' AS subject,
    maxState(toDateTime64(0, 3)) AS last_seen,
    max(ts) AS ts_hwm
FROM (SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events UNION ALL SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events_private)
WHERE (ts > (
    SELECT max(ts_hwm)
    FROM loom.subject_last_seen
    WHERE (org = '') AND (subject = '')
)) AND (ts <= (now64(3) - toIntervalSecond(10)))
HAVING count() > 0;

-- Scope and window pins sit in each physical branch, before UNION and masking,
-- so partition and primary-key pruning do not depend on subquery pushdown.
CREATE OR REPLACE VIEW loom.sql_v1_agent_events
DEFINER = CURRENT_USER SQL SECURITY DEFINER
AS
WITH scope AS
(
    SELECT org, namespace FROM loom.sql_access FINAL
    WHERE user_name = currentUser() AND granted
),
restricted AS
(
    SELECT org, namespace FROM loom.namespaces FINAL WHERE classification = 'restricted'
    UNION ALL
    SELECT org, namespace FROM loom.code_repos FINAL WHERE classification = 'restricted'
)
SELECT
    ts, org, namespace, agent, user_id, session_id, run_id, operation, kind, request_id,
    trace_id, latency_ms, success, error_code, status_class, memory_key, input_tokens,
    output_tokens, embedding_tokens, llm_calls, upstream_cost_usd, bytes_written, model,
    consumer, subject, transport
FROM (SELECT raw.ts AS ts, raw.org AS org, raw.namespace AS namespace, raw.agent AS agent, raw.user_id AS user_id, raw.session_id AS session_id, raw.run_id AS run_id, raw.operation AS operation, raw.request_id AS request_id, raw.trace_id AS trace_id, raw.memory_key AS memory_key, raw.impersonated_by AS impersonated_by, raw.kind AS kind, raw.build_commit AS build_commit, raw.instance AS instance, raw.instance_label AS instance_label, raw.consumer AS consumer, raw.traffic_class AS traffic_class, raw.subject AS subject, raw.transport AS transport, raw.slo_class AS slo_class, raw.budget_ms AS budget_ms, raw.ingested_at AS ingested_at, raw.mirror_marked AS mirror_marked, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.latency_ms), 'UInt32') AS latency_ms, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.input_tokens), 'UInt32') AS input_tokens, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.output_tokens), 'UInt32') AS output_tokens, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.embedding_tokens), 'UInt32') AS embedding_tokens, CAST(if(raw.mirror_marked != 0, CAST(0, 'Decimal(18, 6)'), raw.upstream_cost_usd), 'Decimal(18, 6)') AS upstream_cost_usd, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt64'), raw.bytes_written), 'UInt64') AS bytes_written, CAST(if(raw.mirror_marked != 0, CAST(false, 'Bool'), raw.success), 'Bool') AS success, CAST(if(raw.mirror_marked != 0, CAST('', 'LowCardinality(String)'), raw.error_code), 'LowCardinality(String)') AS error_code, CAST(if(raw.mirror_marked != 0, CAST(map(), 'Map(LowCardinality(String), String)'), raw.args), 'Map(LowCardinality(String), String)') AS args, CAST(if(raw.mirror_marked != 0, CAST('', 'LowCardinality(String)'), raw.model), 'LowCardinality(String)') AS model, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt16'), raw.llm_calls), 'UInt16') AS llm_calls, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.response_tokens), 'UInt32') AS response_tokens, CAST(if(raw.mirror_marked != 0, CAST('', 'LowCardinality(String)'), raw.status_class), 'LowCardinality(String)') AS status_class FROM (SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY UNION ALL SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events_private WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY) AS raw
      WHERE NOT match(raw.namespace, '^acl-[0-9a-f]{24}$'))
WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY;

CREATE OR REPLACE VIEW loom.sql_v1_usage_daily
DEFINER = CURRENT_USER SQL SECURITY DEFINER
AS
WITH scope AS
(
    SELECT org, namespace FROM loom.sql_access FINAL
    WHERE user_name = currentUser() AND granted
),
restricted AS
(
    SELECT org, namespace FROM loom.namespaces FINAL WHERE classification = 'restricted'
    UNION ALL
    SELECT org, namespace FROM loom.code_repos FINAL WHERE classification = 'restricted'
)
SELECT
    toDate(ts, 'UTC') AS day, org, namespace, agent, operation, kind,
    count() AS ops,
    countIf(mirror_marked = 0 AND NOT success) AS errors,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens,
    sum(embedding_tokens) AS embedding_tokens,
    sumIf(llm_calls, mirror_marked = 0) AS llm_calls,
    sum(upstream_cost_usd) AS upstream_cost_usd,
    sumIf(bytes_written, mirror_marked = 0) AS bytes_written,
    sumIf(latency_ms, mirror_marked = 0) AS latency_sum_ms,
    ifNotFinite((quantilesIf(0.5, 0.95)(latency_ms, mirror_marked = 0) AS q)[1], 0) AS latency_p50_ms,
    ifNotFinite(q[2], 0) AS latency_p95_ms
FROM (
    SELECT raw.ts AS ts, raw.org AS org, raw.namespace AS namespace,
        if(raw.mirror_marked = 0, raw.agent, '-') AS agent,
        if(raw.mirror_marked = 0, raw.operation, '-') AS operation,
        if(raw.mirror_marked = 0, raw.kind, '-') AS kind,
        raw.mirror_marked AS mirror_marked, raw.success AS success,
        raw.input_tokens AS input_tokens, raw.output_tokens AS output_tokens,
        raw.embedding_tokens AS embedding_tokens, raw.llm_calls AS llm_calls,
        raw.upstream_cost_usd AS upstream_cost_usd, raw.bytes_written AS bytes_written,
        raw.latency_ms AS latency_ms
    FROM (SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY UNION ALL SELECT ts, org, namespace, agent, user_id, session_id, run_id, operation, request_id, trace_id, latency_ms, input_tokens, output_tokens, embedding_tokens, upstream_cost_usd, bytes_written, success, error_code, memory_key, impersonated_by, kind, args, model, llm_calls, build_commit, instance, instance_label, consumer, response_tokens, traffic_class, subject, transport, slo_class, status_class, budget_ms, ingested_at, mirror_marked FROM loom.agent_events_private WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY) AS raw
    WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY
)
GROUP BY day, org, namespace, agent, operation, kind;


SYSTEM START VIEW loom.agent_cost_rollup_refresh;
SYSTEM START VIEW loom.subject_last_seen_refresh;
