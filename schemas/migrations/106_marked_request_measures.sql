-- Old event writers remain ordinary; existing aggregate keys and states survive.
-- Mirror creation stays disabled through migration and complete fleet finalization.
-- Pause each local scheduler without cancelling an append already in flight, then
-- wait for it to finish. Every refresh-owning replica must complete this cutover.
SYSTEM PAUSE VIEW loom.agent_cost_rollup_refresh;
SYSTEM PAUSE VIEW loom_telemetry.sli_minute_refresh;
SYSTEM PAUSE VIEW loom.subject_last_seen_refresh;
SYSTEM WAIT VIEW loom.agent_cost_rollup_refresh;
SYSTEM WAIT VIEW loom_telemetry.sli_minute_refresh;
SYSTEM WAIT VIEW loom.subject_last_seen_refresh;

-- Old event writers omit the server-owned marker and remain ordinary.
ALTER TABLE loom.agent_events
    ADD COLUMN IF NOT EXISTS mirror_marked UInt8 DEFAULT 0;

-- Existing aggregate states and keys retain their original types and meaning.
ALTER TABLE loom.agent_cost_rollup_daily
    ADD COLUMN IF NOT EXISTS marked_cost_usd_state AggregateFunction(sum, Decimal(18, 6)),
    ADD COLUMN IF NOT EXISTS marked_tokens_state AggregateFunction(sum, UInt64),
    ADD COLUMN IF NOT EXISTS marked_embed_tokens_state AggregateFunction(sum, UInt64),
    ADD COLUMN IF NOT EXISTS marked_ops_state AggregateFunction(count);

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
FROM loom.agent_events
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

ALTER TABLE loom_telemetry.sli_minute_refresh MODIFY QUERY
WITH
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
    countIf(status_class = 'ok' AND latency_ms > budget_ms) AS slow,
    quantilesTDigestStateIf(0.5, 0.95, 0.99)(latency_ms, status_class = 'ok') AS latency,
    fold_to AS ingested_hwm
FROM loom.agent_events
WHERE kind = 'request' AND slo_class != '' AND ts >= now64(3) - toIntervalDay(31)
  AND ingested_at > folded AND ingested_at <= fold_to
  AND mirror_marked = 0 AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
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
-- One insert stream, squashed into one block whatever its size, so the
-- refresh writes one part: the sentinel and the rows it vouches for commit
-- together.
SETTINGS max_insert_threads = 1, min_insert_block_size_rows = 1000000000000,
    min_insert_block_size_bytes = 0;

ALTER TABLE loom.subject_last_seen_refresh MODIFY QUERY
SELECT
    org,
    subject,
    maxState(ts) AS last_seen,
    max(ts) AS ts_hwm
FROM loom.agent_events
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
FROM loom.agent_events
WHERE (ts > (
    SELECT max(ts_hwm)
    FROM loom.subject_last_seen
    WHERE (org = '') AND (subject = '')
)) AND (ts <= (now64(3) - toIntervalSecond(10)))
HAVING count() > 0;

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
FROM (SELECT raw.ts AS ts, raw.org AS org, raw.namespace AS namespace, raw.agent AS agent, raw.user_id AS user_id, raw.session_id AS session_id, raw.run_id AS run_id, raw.operation AS operation, raw.request_id AS request_id, raw.trace_id AS trace_id, raw.memory_key AS memory_key, raw.impersonated_by AS impersonated_by, raw.kind AS kind, raw.build_commit AS build_commit, raw.instance AS instance, raw.instance_label AS instance_label, raw.consumer AS consumer, raw.traffic_class AS traffic_class, raw.subject AS subject, raw.transport AS transport, raw.slo_class AS slo_class, raw.budget_ms AS budget_ms, raw.ingested_at AS ingested_at, raw.mirror_marked AS mirror_marked, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.latency_ms), 'UInt32') AS latency_ms, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.input_tokens), 'UInt32') AS input_tokens, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.output_tokens), 'UInt32') AS output_tokens, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.embedding_tokens), 'UInt32') AS embedding_tokens, CAST(if(raw.mirror_marked != 0, CAST(0, 'Decimal(18, 6)'), raw.upstream_cost_usd), 'Decimal(18, 6)') AS upstream_cost_usd, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt64'), raw.bytes_written), 'UInt64') AS bytes_written, CAST(if(raw.mirror_marked != 0, CAST(false, 'Bool'), raw.success), 'Bool') AS success, CAST(if(raw.mirror_marked != 0, CAST('', 'LowCardinality(String)'), raw.error_code), 'LowCardinality(String)') AS error_code, CAST(if(raw.mirror_marked != 0, CAST(map(), 'Map(LowCardinality(String), String)'), raw.args), 'Map(LowCardinality(String), String)') AS args, CAST(if(raw.mirror_marked != 0, CAST('', 'LowCardinality(String)'), raw.model), 'LowCardinality(String)') AS model, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt16'), raw.llm_calls), 'UInt16') AS llm_calls, CAST(if(raw.mirror_marked != 0, CAST(0, 'UInt32'), raw.response_tokens), 'UInt32') AS response_tokens, CAST(if(raw.mirror_marked != 0, CAST('', 'LowCardinality(String)'), raw.status_class), 'LowCardinality(String)') AS status_class FROM loom.agent_events AS raw
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
    FROM loom.agent_events AS raw
    WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY
)
GROUP BY day, org, namespace, agent, operation, kind;

-- Resume the unchanged schedules only after every local definition is replaced.
SYSTEM START VIEW loom.agent_cost_rollup_refresh;
SYSTEM START VIEW loom_telemetry.sli_minute_refresh;
SYSTEM START VIEW loom.subject_last_seen_refresh;
