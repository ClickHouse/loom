SELECT
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_totals
        )
    ) AS totals,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_versions
            ORDER BY installations DESC, loom_version, clickhouse_version LIMIT 12
        )
    ) AS versions,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_retention ORDER BY age
        )
    ) AS retention,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_activity ORDER BY period
        )
    ) AS activity,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_lifecycle ORDER BY bucket
        )
    ) AS lifecycle,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_oss_adoption
            ORDER BY day, source, metric LIMIT 4000
        )
    ) AS oss
SETTINGS output_format_json_quote_64bit_integers = 0
