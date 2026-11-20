-- The SQL surface: read-only views over a tenant's memory, conversations and request
-- ledger, for an org's own SQL clients (docs/reference/sql-surface.md). Each view
-- is SQL SECURITY DEFINER, so a SQL user granted SELECT on the views alone reads the
-- tables through them and never directly, and each view admits only the rows whose
-- (org, namespace) loom.sql_access grants to currentUser(), the user that sent the
-- statement. A user with no row there reads nothing. A namespace classified restricted
-- (loom.namespaces or loom.code_repos) is never in a view: Loom audits each read of one,
-- which a view cannot. The views leave out what Loom's own listings refuse: rejected
-- rows, the value of a `high` row and its hash (in the history, of every version of a key
-- that was ever `high`: a short secret's hash can be guessed), redacted values, and in the
-- current memories and the conversations, deleted memories and turns (the history keeps a
-- deletion's version). The memory and conversation views read the tables erasure deletes
-- from, so an erased row leaves them with its table.
--
-- Column names are a public contract: a v1 view only gains columns. A rename, a removal
-- or a changed meaning ships as sql_v2_* beside v1.

-- loom.sql_access: which (org, namespace) each SQL user of the surface reads.
-- `namespace = '*'` grants every namespace of the org, `org = '*'` the whole deployment.
-- `loom sql-access` writes it; a revocation writes the row again with granted = 0.
-- No content.
CREATE TABLE IF NOT EXISTS loom.sql_access
(
    `user_name` String,
    `org` String,
    `namespace` String,
    `granted` Bool,
    `updated_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (user_name, org, namespace)
SETTINGS non_replicated_deduplication_window = 1000;

-- loom.sql_v1_memories: one row per memory (identity and key), its current version.
CREATE VIEW IF NOT EXISTS loom.sql_v1_memories
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
  AND (org, namespace) NOT IN (SELECT org, namespace FROM restricted);

-- loom.sql_v1_memory_history: one row per version of every memory, deletions included.
CREATE VIEW IF NOT EXISTS loom.sql_v1_memory_history
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
)
WHERE status != 'rejected';

-- loom.sql_v1_conversations: one row per stored conversation turn of each user.
CREATE VIEW IF NOT EXISTS loom.sql_v1_conversations
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
GROUP BY org, namespace, agent, user_id, thread_id, msg_id
HAVING countIf(c.tombstoned_at IS NOT NULL) = 0;

-- loom.sql_v1_agent_events: the request ledger, one row per op call or worker pass, over
-- the 31 whole UTC days before today and today so far. The window is in the view's own
-- WHERE: a caller's filter runs on what the view returns and narrows no read, so only the
-- view's own filter can prune the table, which grows with every call.
CREATE VIEW IF NOT EXISTS loom.sql_v1_agent_events
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
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY;

-- loom.sql_v1_usage_daily: the request ledger summed per UTC day, agent, operation and kind,
-- over the same window as sql_v1_agent_events, so the two agree.
CREATE VIEW IF NOT EXISTS loom.sql_v1_usage_daily
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
  AND ts >= toStartOfDay(now(), 'UTC') - INTERVAL 31 DAY
GROUP BY day, org, namespace, agent, operation, kind;
