-- No sql_v1 view shows a mirror's rows: a mirror (a namespace named acl-<24 hex>) holds
-- documents each shown only to a reader its store's permission list admits, which a view
-- cannot check (docs/architecture/document-sources.md). Each view is replaced with the
-- same columns and one more filter. The runner refuses this file while any namespace of
-- that form is registered or holds live rows (server/core/schema/prechecks.py).

CREATE OR REPLACE VIEW loom.sql_v1_memories
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
    org, namespace, agent, user_id, session_id, run_id, memory_key, memory_id,
    memory_type,
    if(redacted_at IS NULL AND sensitivity != 'high',
       if(length(toString(value_json)) > 2, toString(value_json), value),
       NULL) AS value,
    multiIf(redacted_at IS NOT NULL, 'redacted', sensitivity = 'high', 'sensitive', '')
        AS value_withheld,
    version, status, confidence, pinned, tags, sensitivity, source_class, actor,
    if(redacted_at IS NULL AND sensitivity != 'high', content_sha256, NULL) AS content_sha256,
    created_at, updated_at, expires_at, valid_at, invalid_at
FROM loom.memory FINAL
WHERE tombstoned_at IS NULL
  AND status NOT IN ('rejected', 'tombstoned')
  AND ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$');

CREATE OR REPLACE VIEW loom.sql_v1_memory_history
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
    org, namespace, agent, user_id, session_id, run_id, memory_key, memory_id,
    memory_type,
    if(redacted_at IS NULL AND NOT ever_high,
       if(length(toString(value_json)) > 2, toString(value_json), value),
       NULL) AS value,
    multiIf(redacted_at IS NOT NULL, 'redacted', ever_high, 'sensitive', '') AS value_withheld,
    version, status, confidence, pinned, tags, sensitivity, source_class, actor,
    if(redacted_at IS NULL AND NOT ever_high, content_sha256, NULL) AS content_sha256,
    created_at, updated_at, tombstoned_at
FROM
(
    SELECT *,
        max(sensitivity = 'high') OVER (
            PARTITION BY org, namespace, agent, user_id, session_id, memory_key) AS ever_high
    FROM loom.memory_versions FINAL
    WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
           OR org IN (SELECT org FROM scope WHERE namespace = '*')
           OR (SELECT count() FROM scope WHERE org = '*') > 0)
      AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
      AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
)
WHERE status != 'rejected';

CREATE OR REPLACE VIEW loom.sql_v1_conversations
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
    org, namespace, agent, user_id, thread_id, msg_id,
    argMax(role, c.ts) AS role,
    argMax(content, c.ts) AS content,
    max(tokens) AS tokens,
    argMax(run_id, c.ts) AS run_id,
    max(c.ts) AS ts
FROM loom.conversations AS c
WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
GROUP BY org, namespace, agent, user_id, thread_id, msg_id
HAVING countIf(c.tombstoned_at IS NOT NULL) = 0;

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
FROM loom.agent_events
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
    countIf(NOT success) AS errors,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens,
    sum(embedding_tokens) AS embedding_tokens,
    sum(llm_calls) AS llm_calls,
    sum(upstream_cost_usd) AS upstream_cost_usd,
    sum(bytes_written) AS bytes_written,
    sum(latency_ms) AS latency_sum_ms,
    (quantiles(0.5, 0.95)(latency_ms) AS q)[1] AS latency_p50_ms,
    q[2] AS latency_p95_ms
FROM loom.agent_events
WHERE ((org, namespace) IN (SELECT org, namespace FROM scope)
       OR org IN (SELECT org FROM scope WHERE namespace = '*')
       OR (SELECT count() FROM scope WHERE org = '*') > 0)
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted)
  AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY
GROUP BY day, org, namespace, agent, operation, kind;
