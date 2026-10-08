-- The lexical leg's corpus statistics as plain numbers: one row per
-- (org, namespace, term) with its document frequency, plus one row per
-- namespace with term = '' carrying the chunk count (df) and the mean chunk
-- length (avgdl).
--
-- code_term_freq holds each term's df as a uniqExact state over chunk ids,
-- so reading one term fetches a granule of such states: a question of 40
-- words read ~350 MB from object storage on a cold cache. This table holds
-- the same numbers in about 20 bytes a row, so the lexical leg reads every
-- word of a question and the namespace totals in one small statement.
--
-- The view below rebuilds it from code_term_freq and code_doc_stats every
-- hour and replaces its contents whole, so a df here is at most an hour
-- old. A namespace without the '' row (created since the last refresh, or
-- before the first one) is read from the two source tables as before.
-- Old builds never read it.
CREATE TABLE IF NOT EXISTS loom.code_term_df
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `term` String,
    `df` UInt32,
    `avgdl` Float32 DEFAULT 0,
    `refreshed_at` DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (org, namespace, term)
SETTINGS index_granularity = 1024;

CREATE MATERIALIZED VIEW IF NOT EXISTS loom.code_term_df_refresh
REFRESH EVERY 1 HOUR TO loom.code_term_df
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `term` String,
    `df` UInt32,
    `avgdl` Float32,
    `refreshed_at` DateTime
)
DEFINER = loom SQL SECURITY DEFINER
AS SELECT
    org,
    namespace,
    term,
    toUInt32(uniqExactMerge(df_state)) AS df,
    toFloat32(0) AS avgdl,
    now() AS refreshed_at
FROM loom.code_term_freq
WHERE term != ''
GROUP BY
    org,
    namespace,
    term
UNION ALL
SELECT
    org,
    namespace,
    '' AS term,
    toUInt32(count()) AS df,
    toFloat32(avgOrDefault(token_count)) AS avgdl,
    now() AS refreshed_at
FROM loom.code_doc_stats FINAL
GROUP BY
    org,
    namespace
SETTINGS optimize_aggregation_in_order = 1;
