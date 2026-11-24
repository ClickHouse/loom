-- Run each SELECT in its own SQL cell on the OSS tab.
-- Input: one row per UTC day, source, artifact and metric in loom_oss_metrics_daily.

-- Latest repository and distribution counters. Keep the source and observation date visible.
SELECT source, artifact, metric, argMax(value, day) AS latest_value, max(day) AS observed_on
FROM loom_oss_metrics_daily
WHERE metric IN ('stars', 'forks', 'container_pulls_total', 'release_asset_downloads_total')
GROUP BY source, artifact, metric
ORDER BY source, artifact, metric;

-- Counter history. Each point is a snapshot, so changes include removals as well as additions.
SELECT day, source, artifact, metric, value
FROM loom_oss_metrics_daily
WHERE metric IN ('stars', 'forks', 'container_pulls_total', 'release_asset_downloads_total')
ORDER BY day, source, artifact, metric;

-- SDK download volumes by registry over the previous 30 complete UTC dates.
SELECT
    source,
    artifact,
    sum(value) AS downloads_30d,
    count() AS days_reported,
    max(day) AS observed_through
FROM loom_oss_metrics_daily
WHERE metric = 'sdk_downloads'
  AND day >= toDate(now('UTC')) - 30 AND day < toDate(now('UTC'))
GROUP BY source, artifact
ORDER BY source, artifact;

-- Daily SDK downloads. Missing dates are absent from the input feed.
SELECT day, source, artifact, value AS downloads
FROM loom_oss_metrics_daily
WHERE metric = 'sdk_downloads' AND day < toDate(now('UTC'))
ORDER BY day, source, artifact;
