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
            ORDER BY active_installations DESC, loom_version, clickhouse_version LIMIT 12
        )
    ) AS versions,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_version_trends ORDER BY period, product, version
        )
    ) AS version_trends,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_first_observation
        )
    ) AS first_observation,
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
            FROM loom_product_analytics.public_adoption_growth ORDER BY grain, period
        )
    ) AS growth,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_lifecycle ORDER BY bucket
        )
    ) AS lifecycle,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_domain_directories ORDER BY range_days
        )
    ) AS domain_directories,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_domain_accounts ORDER BY range_days, domain
        )
    ) AS domain_accounts,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_adoption_company_activity ORDER BY period
        )
    ) AS company_trends,
    (
        SELECT groupArray(payload) FROM (
            SELECT formatRowNoNewline('JSONEachRow', *) AS payload
            FROM loom_product_analytics.public_oss_adoption
            ORDER BY day, source, metric
        )
    ) AS oss
SETTINGS output_format_json_quote_64bit_integers = 0
