import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from server.insights.store import adoption_telemetry as adoption_store
from server.insights.workers import adoption_telemetry
from server.settings import Settings, settings
from server.workers import pass_context, status_store, supervisor
from server.workers.registry import enabled_workers
from tests.unit.clickhouse_local import CLICKHOUSE, ROOT, run


def test_adoption_has_one_setting_and_is_enabled_by_default() -> None:
    assert {name for name in Settings.model_fields if name.startswith("adoption_telemetry_")} == {
        "adoption_telemetry_enabled"
    }
    assert Settings.model_fields["adoption_telemetry_enabled"].default is True


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_public_adoption_query_covers_current_and_complete_periods() -> None:
    telemetry = ROOT / "deploy" / "telemetry"
    clock = "toDateTime('2026-11-27 18:00:00', 'UTC')"
    warehouse = (telemetry / "warehouse.sql").read_text().strip().removesuffix(";")
    setup = [
        warehouse.replace("now('UTC')", clock),
        """
        INSERT INTO loom_product_analytics.adoption_enrollments (installation_id, is_internal)
        VALUES ('internal', 1)
        """,
        """
        INSERT INTO loom_product_analytics.adoption_reports
            (installation_id, window_start, window_end, day, schema_version, loom_version,
             clickhouse_version, requests, successful_requests, memory_requests, user_domains)
        WITH
            toDate('2026-11-27') - days_ago AS report_day,
            toDateTime(report_day, 'UTC') + toIntervalHour(if(instance = 'unfinished', 12, 0)) AS report_start
        SELECT
            instance, report_start, report_start + INTERVAL 12 HOUR,
            report_day, version, if(instance = 'smoke', '0.0.0-smoke', '1.0.0'),
            if(instance = 'smoke', '0.0.0-smoke', '26.0.0'), request_count, request_count, request_count,
            [(domain, toUInt64(1), request_count)]
        FROM VALUES(
            'instance String, days_ago Int16, version UInt8, domain String, request_count UInt64',
            ('present', 1, 2, 'active.example', 1),
            ('present', 3, 2, 'zeta.example', 8),
            ('present', 30, 2, 'near-cutoff.example', 1),
            ('present', 31, 2, 'midrange.example', 1),
            ('duplicate', 2, 2, 'active.example', 2),
            ('previous', 31, 2, 'previous.example', 3),
            ('boundary', 90, 2, 'boundary.example', 1),
            ('boundary', 57, 2, 'boundary.example', 8),
            ('boundary', 1, 2, 'boundary.example', 0),
            ('expired', 91, 2, 'old.example', 1),
            ('today', 0, 2, 'today.example', 5),
            ('unfinished', 0, 2, 'unfinished.example', 99),
            ('future', -1, 2, 'future.example', 1),
            ('legacy', 1, 1, 'private.example', 1),
            ('idle', 1, 2, 'idle.example', 0),
            ('internal', 1, 2, 'internal.example', 1),
            ('smoke', 1, 2, 'smoke.example', 1)
        )
        """,
        """
        INSERT INTO loom_product_analytics.oss_metrics (day, source, artifact, metric, value)
        VALUES
            (toDate('2026-11-26'), 'crates.io', 'clickhouse-loom', 'sdk_downloads', 2),
            (toDate('2026-11-26'), 'crates.io', 'unrelated', 'sdk_downloads', 9)
        """,
    ]

    [report] = run(setup, (telemetry / "public-adoption.sql").read_text())

    totals = {
        row["comparison"]: row
        for row in map(json.loads, report["totals"])
        if row["range_days"] == 30
    }
    assert set(totals) == {"current", "current_complete", "previous_complete"}
    assert totals["current"]["memory_requests"] == 18
    assert totals["current"]["last_reported"] == "2026-11-27"
    assert totals["current_complete"]["memory_requests"] == 13
    assert totals["current_complete"]["last_reported"] == "2026-11-26"
    assert totals["current"]["days_reported"] == totals["current_complete"]["days_reported"] == 4
    assert totals["previous_complete"]["memory_requests"] == 12

    assert [json.loads(row) for row in report["domain_directories"]] == [
        {
            "range_days": 30,
            "domains": ["zeta.example", "today.example", "active.example", "near-cutoff.example"],
        },
        {
            "range_days": 90,
            "domains": [
                "boundary.example",
                "zeta.example",
                "today.example",
                "active.example",
                "previous.example",
                "midrange.example",
                "near-cutoff.example",
            ],
        },
    ]
    [rust] = [json.loads(row) for row in report["oss"]]
    assert (rust["source"], rust["artifact"], rust["metric"], rust["value"]) == (
        "crates.io",
        "clickhouse-loom",
        "sdk_downloads",
        2,
    )

    growth = [json.loads(row) for row in report["growth"]]
    for grain in ("day", "week", "month"):
        bucketed = sorted(
            (row for row in growth if row["grain"] == grain), key=lambda row: row["period"]
        )
        assert sum(row["first_active"] for row in bucketed) == 6
        assert bucketed[-1]["cumulative_active"] == 6
    assert max(row["period"] for row in growth if row["grain"] == "day") == "2026-11-26"

    activity = [json.loads(row) for row in report["activity"]]
    assert sum(row["memory_requests"] for row in activity if row["grain"] == "day") == 27
    reported = {(row["grain"], row["period"]) for row in activity}
    observed = {(row["grain"], row["period"]) for row in growth}
    assert reported <= observed
    assert any(row["grain"] == "week" and row["first_active"] == 0 for row in growth)


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_complete_adoption_day_adds_missing_successful_requests_to_reported_attempts() -> None:
    warehouse = (
        (ROOT / "deploy" / "telemetry" / "warehouse.sql").read_text().strip().removesuffix(";")
    )
    rows = run(
        [
            warehouse.replace("now('UTC')", "toDateTime('2026-11-27 18:00:00', 'UTC')"),
            """
            INSERT INTO loom_product_analytics.adoption_reports
                (installation_id, window_start, window_end, day, schema_version, loom_version, clickhouse_version,
                 requests, successful_requests, server_errors, daily_successful_requests)
            VALUES
                ('missing-half', '2026-11-26 12:00:00', '2026-11-27 00:00:00', '2026-11-26',
                 3, '1.2.3', '26.1.0', 20, 10, 10, 25),
                ('both-halves', '2026-11-26 00:00:00', '2026-11-26 12:00:00', '2026-11-26',
                 2, '1.2.3', '26.1.0', 20, 10, 10, NULL),
                ('both-halves', '2026-11-26 12:00:00', '2026-11-27 00:00:00', '2026-11-26',
                 3, '1.2.3', '26.1.0', 20, 12, 3, 20),
                ('legacy', '2026-11-26 00:00:00', '2026-11-26 12:00:00', '2026-11-26',
                 2, '1.2.3', '26.1.0', 5, 3, 1, NULL)
            """,
        ],
        """
        SELECT installation_id, requests, successful_requests, server_errors
        FROM loom_product_analytics.loom_external_adoption_daily
        """,
    )

    assert {
        row["installation_id"]: (row["requests"], row["successful_requests"], row["server_errors"])
        for row in rows
    } == {"both-halves": (40, 22, 13), "legacy": (5, 3, 1), "missing-half": (35, 25, 10)}


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_domain_lifecycle_survives_installation_changes_and_reports_only_comparable_periods() -> (
    None
):
    warehouse = (
        (ROOT / "deploy" / "telemetry" / "warehouse.sql").read_text().strip().removesuffix(";")
    )
    setup = [
        warehouse.replace("now('UTC')", "toDateTime('2026-06-08 06:00:00', 'UTC')"),
        """
        INSERT INTO loom_product_analytics.adoption_reports
            (installation_id, window_start, window_end, day, schema_version, loom_version,
             clickhouse_version, requests, successful_requests, user_domains, daily_successful_requests)
        WITH toDateTime('2026-04-27', 'UTC') + toIntervalHour(number * 12) AS start
        SELECT 'coverage', start, start + INTERVAL 12 HOUR, toDate(start), multiIf(number = 44, 1, number = 17, 3, 2),
               '1.0.0', '26.0.0', 0, 0, [], if(number = 17, 0, NULL)
        FROM numbers(84)
        WHERE number != 16
        """,
        """
        INSERT INTO loom_product_analytics.adoption_reports
            (installation_id, window_start, window_end, day, schema_version, loom_version,
             clickhouse_version, requests, successful_requests, user_domains)
        WITH toDateTime(report_day, 'UTC') + toIntervalHour(hours) AS start
        SELECT installation, start, start + INTERVAL 12 HOUR, toDate(start), schema,
               '1.0.0', '26.0.0', volume, volume, [(domain, toUInt64(1), volume)]
        FROM VALUES(
            'installation String, report_day String, domain String, volume UInt64, hours UInt8, schema UInt8',
            ('old-team', '2026-05-04', 'gmail.com', 12000, 0, 2),
            ('new-team', '2026-05-11', 'gmail.com', 12000, 0, 2),
            ('new-team', '2026-05-11', 'gmail.com', 1000000, 12, 1),
            ('returning', '2026-05-04', 'returning.example', 1, 0, 2),
            ('returning', '2026-06-01', 'returning.example', 1, 0, 2),
            ('new', '2026-06-01', 'new.example', 1, 0, 2)
        )
        """,
    ]
    rows = run(
        setup,
        "SELECT * FROM loom_product_analytics.public_adoption_lifecycle WHERE grain = 'week'",
    )
    by_period = {(row["entity"], int(row["min_requests"]), row["bucket"]): row for row in rows}

    def activity(entity: str, threshold: int, period: str) -> tuple[int, ...]:
        row = by_period[entity, threshold, period]
        assert row["complete_coverage"] == 1
        return tuple(int(row[state]) for state in ("new", "retained", "reactivated", "inactive"))

    assert activity("deployment", 0, "2026-05-11") == (1, 0, 0, 2)
    assert activity("domain", 0, "2026-05-11") == (0, 1, 0, 1)
    assert activity("domain", 50_000, "2026-05-11") == (0, 1, 0, 0)
    assert activity("domain", 1_000_000, "2026-05-11") == (0, 0, 0, 0)
    assert activity("domain", 0, "2026-06-01") == (1, 0, 1, 0)
    assert {row["bucket"] for row in rows if row["entity"] == "domain"} == {
        "2026-05-04",
        "2026-05-11",
        "2026-06-01",
    }
    assert {row["bucket"] for row in rows if row["entity"] == "deployment"} == {
        "2026-05-04",
        "2026-05-11",
        "2026-05-18",
        "2026-05-25",
        "2026-06-01",
    }


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_public_adoption_query_publishes_only_measured_company_activity() -> None:
    telemetry = ROOT / "deploy" / "telemetry"
    clock = "toDateTime('2026-11-27 18:00:00', 'UTC')"
    warehouse = (telemetry / "warehouse.sql").read_text().strip().removesuffix(";")
    setup = [
        warehouse.replace("now('UTC')", clock),
        """
        INSERT INTO loom_product_analytics.adoption_enrollments (installation_id, is_internal)
        VALUES ('internal', 1)
        """,
        """
        INSERT INTO loom_product_analytics.adoption_reports
            (installation_id, window_start, window_end, day, schema_version, loom_version,
             clickhouse_version, requests, successful_requests, user_domains,
             active_accounts, serving_processes, daily_successful_requests, domain_activity)
        WITH
            toDate('2026-11-27') - days_ago AS report_day,
            toDateTime(report_day, 'UTC') + toIntervalHour(hours) AS report_start,
            if(instance = 'previous', NULL, measured) AS processes,
            multiIf(instance = 'morning-only', 63, instance = 'old-id' AND days_ago = 1 AND hours = 12,
                    request_count + 5, request_count) AS domain_requests
        SELECT
            instance, report_start, report_start + INTERVAL 12 HOUR, report_day, schema,
            loom, clickhouse, request_count, request_count,
            if(instance = 'quiet' AND schema = 3, [], [(domain, toUInt64(87), domain_requests)]),
            measured, processes, if(schema = 3, domain_requests, NULL),
            if(isNull(published), [], [(domain, assumeNotNull(published), domain_requests, processes)])
        FROM VALUES(
            'instance String, days_ago Int16, schema UInt8, hours UInt8, loom String,
             clickhouse String, domain String, request_count UInt64, measured Nullable(UInt64), published Nullable(UInt64)',
            ('internal', 372, 2, 0, '0.1.0', '25.11', 'internal.example', 3, NULL, NULL),
            ('smoke-loom', 371, 2, 0, '0.0.0-smoke', '25.11', 'smoke.example', 3, NULL, NULL),
            ('smoke-ch', 371, 2, 0, '0.1.0', '0.0.0-smoke', 'smoke.example', 3, NULL, NULL),
            ('old-id', 370, 1, 0, '1.0.0', '25.12', 'private.example', 3, NULL, NULL),
            ('old-id', 100, 3, 12, '1.0.0', '25.12', 'long-running.example', 100, 1, 1),
            ('previous', 51, 3, 12, '1.1.0', '26.1.0', 'previous.example', 50, 2, 2),
            ('legacy-overlap', 9, 1, 0, '1.1.0', '26.1.0', 'legacy-only.example', 99, NULL, NULL),
            ('legacy-overlap', 9, 2, 12, '1.1.0', '26.1.0', 'legacy-only.example', 0, NULL, NULL),
            ('quiet', 7, 2, 0, '1.1.0', '26.1.0', 'quiet.example', 8, NULL, NULL),
            ('morning-only', 6, 3, 12, '1.1.0', '26.1.0', 'gmail.com', 0, 1, 1),
            ('legacy', 4, 1, 0, '1.0.0', '25.12', 'private.example', 3, NULL, NULL),
            ('old-id', 3, 3, 12, '1.0.1', '25.12.1', 'long-running.example', 25, 4, 4),
            ('names-only', 3, 2, 12, '1.1.0', '26.1.0', 'names-only.example', 3, 999, 999),
            ('quiet', 2, 3, 12, '1.1.0', '26.1.0', 'quiet.example', 0, 0, NULL),
            ('old-id', 1, 2, 0, '1.1.0', '26.1.0', 'long-running.example', 5, NULL, NULL),
            ('old-id', 1, 3, 12, '1.1.0', '26.1.0', 'long-running.example', 12, 4, 4),
            ('second-id', 1, 3, 12, '1.1.0', '26.0.0', 'long-running.example', 20, 7, 7),
            ('names-only', 1, 2, 12, '1.1.0', '26.1.0', 'names-only.example', 17, NULL, NULL),
            ('legacy', 1, 1, 0, '1.0.2', '26.1.5', 'private.example', 0, NULL, NULL),
            ('noon', 1, 3, 0, '1.2.0', '26.1.0', 'long-running.example', 0, 999, 999),
            ('internal', 1, 3, 12, '1.2.0', '26.1.0', 'long-running.example', 99, 99, 99),
            ('smoke-ch', 1, 3, 12, '1.2.0', '0.0.0-smoke', 'long-running.example', 99, 99, 99),
            ('unfinished', 0, 3, 12, '1.2.0', '26.1.0', 'long-running.example', 99, 99, 99)
        )
        """,
    ]

    [report] = run(setup, (telemetry / "public-adoption.sql").read_text())

    [first_observation] = map(json.loads, report["first_observation"])
    assert first_observation == {
        "day": "2025-11-22",
        "loom_version": "1.0.0",
        "clickhouse_version": "25.12",
    }
    assert all("verified_companies" not in json.loads(row) for row in report["totals"])
    [morning_activity] = [
        row
        for row in map(json.loads, report["activity"])
        if row["grain"] == "day" and row["period"] == "2026-11-21"
    ]
    assert (morning_activity["operations"], morning_activity["active"]) == (63, 1)

    directories = {
        row["range_days"]: row["domains"] for row in map(json.loads, report["domain_directories"])
    }
    assert directories[30] == [
        "gmail.com",
        "long-running.example",
        "names-only.example",
        "quiet.example",
    ]
    assert directories[90] == [
        "gmail.com",
        "long-running.example",
        "previous.example",
        "names-only.example",
        "quiet.example",
    ]
    company_metrics = {
        (row["range_days"], row["domain"]): (
            row["day"],
            row["active_accounts"],
            row["successful_requests"],
            row["active_installations"],
            row["loom_version"],
            row["first_seen"],
            row["last_seen"],
            row["serving_processes"],
        )
        for row in map(json.loads, report["domain_accounts"])
    }
    assert company_metrics == {
        (30, "long-running.example"): (
            "2026-11-26",
            11,
            62,
            2,
            "1.1.0",
            "2026-08-19",
            "2026-11-26",
            11,
        ),
        (90, "long-running.example"): (
            "2026-11-26",
            11,
            62,
            2,
            "1.1.0",
            "2026-08-19",
            "2026-11-26",
            11,
        ),
        (30, "gmail.com"): ("2026-11-21", 1, 63, 1, "1.1.0", "2026-11-21", "2026-11-21", 1),
        (90, "gmail.com"): ("2026-11-21", 1, 63, 1, "1.1.0", "2026-11-21", "2026-11-21", 1),
        (90, "previous.example"): (
            "2026-10-07",
            2,
            50,
            1,
            "1.1.0",
            "2026-10-07",
            "2026-10-07",
            None,
        ),
    }
    companies = {
        (row["grain"], row["period"]): (row["companies"], row["days_reported"])
        for row in map(json.loads, report["company_trends"])
    }
    assert companies["day", "2026-11-21"] == (1, 1)
    assert ("day", "2026-11-23") not in companies
    assert companies["day", "2026-11-24"] == (2, 1)
    assert companies["day", "2026-11-25"] == (0, 1)
    assert companies["week", "2026-11-23"] == (2, 3)

    accounts = {
        (row["grain"], row["period"]): (
            row["active_accounts"],
            row["accounts_days_reported"],
            row["account_reporting_installations"],
        )
        for row in map(json.loads, report["activity"])
    }
    assert accounts["day", "2026-11-23"] == (None, 0, 0)
    assert accounts["day", "2026-11-25"] == (0, 1, 1)
    assert accounts["day", "2026-11-26"] == (11, 1, 2)
    assert accounts["week", "2026-11-23"] == (11, 3, 2)
    processes = {
        (row["grain"], row["period"]): (
            row["serving_processes"],
            row["processes_days_reported"],
            row["process_reporting_installations"],
        )
        for row in map(json.loads, report["activity"])
    }
    assert processes["day", "2026-10-07"] == (None, 0, 0)
    assert processes["day", "2026-11-25"] == (0, 1, 1)
    assert processes["week", "2026-11-23"] == (11, 3, 2)

    current_versions = [
        (row["loom_version"], row["clickhouse_version"], row["active_installations"])
        for row in map(json.loads, report["versions"])
    ]
    assert current_versions == [
        ("1.1.0", "26.1.0", 5),
        ("1.0.0", "25.12", 1),
        ("1.1.0", "26.0.0", 1),
    ]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_public_adoption_coverage_distinguishes_observed_days_from_complete_days() -> None:
    warehouse = (
        (ROOT / "deploy" / "telemetry" / "warehouse.sql").read_text().strip().removesuffix(";")
    )
    rows = run(
        [
            warehouse.replace("now('UTC')", "toDateTime('2026-11-24 18:00:00', 'UTC')"),
            """
            INSERT INTO loom_product_analytics.adoption_reports
                (installation_id, window_start, window_end, day, schema_version, loom_version, clickhouse_version)
            WITH toDateTime('2026-11-09', 'UTC') + toIntervalDay(number) + toIntervalHour(hours) AS start
            SELECT if(hours = 0, 'morning', 'afternoon'), start, start + INTERVAL 12 HOUR,
                   toDate(start), 2, '1.2.3', '26.1.0'
            FROM numbers(14)
            ARRAY JOIN [0, 12] AS hours
            WHERE hours = 0 OR number >= 7
            """,
            """
            INSERT INTO loom_product_analytics.adoption_reports
                (installation_id, window_start, window_end, day, schema_version, loom_version, clickhouse_version,
                 daily_successful_requests)
            VALUES ('final-only', '2026-11-23 12:00:00', '2026-11-24 00:00:00', '2026-11-23',
                    3, '1.2.3', '26.1.0', 0)
            """,
        ],
        """
        SELECT * FROM (
            SELECT 'activity' AS series, grain, period, days_reported, complete_days_reported
            FROM loom_product_analytics.public_adoption_activity
            UNION ALL
            SELECT 'domain' AS series, grain, period, days_reported, complete_days_reported
            FROM loom_product_analytics.public_adoption_company_activity
        )
        WHERE (grain = 'week' AND period < toDate('2026-11-23'))
           OR (grain = 'day' AND period = toDate('2026-11-23'))
        """,
    )

    assert {
        (row["series"], row["grain"], row["period"]): (
            row["days_reported"],
            row["complete_days_reported"],
        )
        for row in rows
    } == {
        ("activity", "week", "2026-11-09"): (7, 0),
        ("activity", "week", "2026-11-16"): (7, 7),
        ("activity", "day", "2026-11-23"): (1, 0),
        ("domain", "week", "2026-11-09"): (7, 0),
        ("domain", "week", "2026-11-16"): (7, 7),
        ("domain", "day", "2026-11-23"): (1, 1),
    }


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_public_adoption_activity_keeps_sparse_closed_opening_periods() -> None:
    warehouse = (
        (ROOT / "deploy" / "telemetry" / "warehouse.sql").read_text().strip().removesuffix(";")
    )
    clock = "toDateTime('2026-12-01 18:00:00', 'UTC')"
    rows = run(
        [
            warehouse.replace("now('UTC')", clock),
            """
            INSERT INTO loom_product_analytics.adoption_reports
                (installation_id, window_start, window_end, day, schema_version, loom_version, clickhouse_version)
            VALUES
                ('external', '2026-11-25 00:00:00', '2026-11-25 12:00:00', '2026-11-25', 2, '1.2.3', '26.1.0'),
                ('external', '2026-11-26 00:00:00', '2026-11-26 12:00:00', '2026-11-26', 2, '1.2.3', '26.1.0')
            """,
        ],
        """
        SELECT grain, period, period_end, days_reported, forecast_eligible, operations
        FROM loom_product_analytics.public_adoption_activity
        WHERE grain IN ('month', 'week')
        ORDER BY period
        """,
    )

    assert rows == [
        {
            "grain": grain,
            "period": period,
            "period_end": period_end,
            "days_reported": 2,
            "forecast_eligible": 0,
            "operations": 0,
        }
        for grain, period, period_end in [
            ("month", "2026-11-01", "2026-12-01"),
            ("week", "2026-11-23", "2026-11-30"),
        ]
    ]


async def test_explicit_opt_out_skips_adoption_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.model_fields["adoption_telemetry_enabled"].default is True
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", False)
    ch = AsyncMock()
    http = AsyncMock()
    monkeypatch.setattr(adoption_telemetry, "get_client", ch)
    monkeypatch.setattr(adoption_telemetry, "get_http_client", http)

    assert "adoption_telemetry" not in {worker.name for worker in enabled_workers()}
    assert not await adoption_telemetry.first_pass_due()
    await adoption_telemetry.run_pass()
    ch.assert_not_called()
    http.assert_not_called()


def test_adoption_worker_checks_every_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    worker = next(worker for worker in enabled_workers() if worker.name == "adoption_telemetry")
    assert worker.interval_seconds == 300


@pytest.mark.parametrize(
    ("cursor", "due"),
    [
        ("", True),
        ("2026-11-23T00:00:00+00:00", True),
        ("2026-11-23T12:00:00+00:00", False),
    ],
)
async def test_boot_reports_unacknowledged_windows_without_a_stagger(
    monkeypatch: pytest.MonkeyPatch, cursor: str, due: bool
) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    monkeypatch.setattr(
        adoption_telemetry, "utc_now", lambda: datetime(2026, 11, 24, 12, 59, tzinfo=UTC)
    )
    monkeypatch.setattr(status_store, "resume_cursor", AsyncMock(return_value=cursor))
    monkeypatch.setattr(status_store, "last_pass_finished", AsyncMock(return_value=None))
    worker = next(worker for worker in enabled_workers() if worker.name == "adoption_telemetry")

    delay, state = await supervisor._begin_loop(worker)

    assert state.cursor == cursor
    assert (delay, state.trigger) == (
        (0, "first_pass") if due else (supervisor.first_pass_delay(worker), "boot")
    )


async def test_missing_deployment_id_surfaces_status_without_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    monkeypatch.setattr(adoption_telemetry, "get_client", object)
    monkeypatch.setattr(adoption_telemetry, "recorded_authority", AsyncMock(return_value=None))
    http = AsyncMock()
    monkeypatch.setattr(adoption_telemetry, "get_http_client", http)

    with pass_context.ensure_pass("adoption_telemetry") as ctx:
        await adoption_telemetry.run_pass()
        assert ctx.outcome == {"status": "missing_deployment_id"}
        assert ctx.cursor == ""
    http.assert_not_called()


async def test_snapshot_read_limit_skips_only_the_live_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    now = datetime(2026, 11, 24, 3, tzinfo=UTC)
    monkeypatch.setattr(adoption_telemetry, "utc_now", lambda: now)
    deployment_id = uuid4()
    monkeypatch.setattr(
        adoption_telemetry,
        "recorded_authority",
        AsyncMock(return_value={"deployment_id": str(deployment_id)}),
    )
    ch = AsyncMock()
    ch.query_all.side_effect = DatabaseError("Code: 158. Limit for rows to read exceeded.")
    monkeypatch.setattr(adoption_telemetry, "get_client", lambda: ch)
    monkeypatch.setattr(adoption_telemetry, "window_report", AsyncMock(return_value={}))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(adoption_telemetry, "_send_report", send)

    with pass_context.ensure_pass("adoption_telemetry") as ctx:
        await adoption_telemetry.run_pass()
        assert ctx.cursor == "2026-11-23T12:00:00+00:00"
        assert ctx.outcome["snapshot"] == "read_limit"
    send.assert_awaited_once_with({}, "window_start")
    assert ch.query_all.await_args.kwargs["settings"] == adoption_store._SNAPSHOT_READ_LIMITS
    ch.query_one.assert_not_called()


@pytest.mark.parametrize(
    "rejection",
    [
        httpx.Response(302, headers={"Location": "https://other.example/adoption"}),
        httpx.Response(200, json={"stored": True, "window_start": "not-this-window"}),
        None,
    ],
    ids=["redirect", "wrong-acknowledgement", "oversized-acknowledgement"],
)
async def test_delivery_retries_without_recounting_after_ack(
    monkeypatch: pytest.MonkeyPatch,
    rejection: httpx.Response | None,
) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    monkeypatch.setattr(
        adoption_telemetry, "utc_now", lambda: datetime(2026, 11, 24, 3, tzinfo=UTC)
    )
    deployment_id = uuid4()
    authority = AsyncMock(return_value={"deployment_id": str(deployment_id)})
    monkeypatch.setattr(adoption_telemetry, "recorded_authority", authority)
    monkeypatch.setattr(adoption_telemetry, "get_client", object)
    restricted_pairs = {("test", "restricted")}

    monkeypatch.setattr(adoption_telemetry, "configured_restricted_pairs", lambda: restricted_pairs)

    async def report(
        start: datetime,
        end: datetime,
        installation_id: UUID,
        *,
        restricted: set[tuple[str, str]],
        ch: object,
    ) -> dict[str, str]:
        assert installation_id != deployment_id
        assert start == datetime(2026, 11, 23, 12, tzinfo=UTC)
        assert end == datetime(2026, 11, 24, tzinfo=UTC)
        assert restricted == restricted_pairs
        return {"installation_id": str(installation_id), "window_start": start.isoformat()}

    aggregate = AsyncMock(side_effect=report)
    monkeypatch.setattr(adoption_telemetry, "window_report", aggregate)

    async def snapshot(
        end: datetime,
        installation_id: UUID,
        *,
        restricted: set[tuple[str, str]],
        ch: object,
    ) -> dict[str, str]:
        assert restricted == restricted_pairs
        assert end == datetime(2026, 11, 24, 2, 55, tzinfo=UTC)
        return {"installation_id": str(installation_id), "observed_at": end.isoformat()}

    snapshots = AsyncMock(side_effect=snapshot)
    monkeypatch.setattr(adoption_telemetry, "domain_snapshot", snapshots)
    completed: list[httpx.Request] = []

    def collect(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "loom.clickhouse.com"
        assert request.url.path == "/api/v1/adoption"
        payload = json.loads(request.content)
        acknowledgement = {"stored": True, **payload}
        if "observed_at" in payload:
            return httpx.Response(200, json=acknowledgement)
        completed.append(request)
        if len(completed) > 1:
            return httpx.Response(200, json=acknowledgement)
        if rejection is not None:
            return rejection
        padded = json.dumps(acknowledgement).encode().ljust(4097, b" ")
        return httpx.Response(200, content=padded)

    async with httpx.AsyncClient(transport=httpx.MockTransport(collect)) as client:
        monkeypatch.setattr(adoption_telemetry, "get_http_client", lambda: client)
        with pass_context.ensure_pass("adoption_telemetry") as ctx:
            await adoption_telemetry.run_pass()
            assert ctx.outcome == {"installation_id": str(aggregate.await_args.args[2])}
            assert ctx.cursor == "" and ctx.count == 1
            await adoption_telemetry.run_pass()
            assert ctx.cursor == "2026-11-23T12:00:00+00:00"
            ctx.resume_cursor = ctx.cursor
            await adoption_telemetry.run_pass()

    assert len(completed) == 2 and aggregate.await_count == 2
    assert snapshots.await_count == 3
    assert completed[0].content == completed[1].content
    payload = json.loads(completed[0].content)
    assert UUID(payload["installation_id"]).version == 5
    assert payload["window_start"] == "2026-11-23T12:00:00+00:00"


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_public_domain_directory_uses_latest_snapshot_until_daily_report() -> None:
    telemetry = ROOT / "deploy" / "telemetry"
    clock = "toDateTime('2026-11-27 01:30:00', 'UTC')"
    warehouse = (telemetry / "warehouse.sql").read_text().strip().removesuffix(";")
    [report] = run(
        [
            warehouse.replace("now('UTC')", clock),
            """
            INSERT INTO loom_product_analytics.adoption_enrollments (installation_id, is_internal)
            VALUES ('internal', 1)
            """,
            """
            INSERT INTO loom_product_analytics.adoption_reports
                (installation_id, window_start, window_end, day, schema_version,
                 loom_version, clickhouse_version, requests, successful_requests,
                 daily_successful_requests, user_domains, domain_activity)
            VALUES
                ('measured', '2026-11-26 12:00:00', '2026-11-27 00:00:00', '2026-11-26',
                 3, '1.2.0', '26.1.0', 23, 23, 23, [('live.example', 1, 23)],
                 [('live.example', 3, 23, 2)]),
                ('capped', '2026-11-26 12:00:00', '2026-11-27 00:00:00', '2026-11-26',
                 3, '1.2.0', '26.1.0', 4, 4, 4, [('capped.example', 1, 4)], []),
                ('stale', '2026-11-26 12:00:00', '2026-11-27 00:00:00', '2026-11-26',
                 3, '1.2.0', '26.1.0', 1, 1, 1, [('stale.example', 1, 1)], [])
            """,
            """
            INSERT INTO loom_product_analytics.adoption_domain_snapshots
                (schema_version, installation_id, day, observed_at, loom_version,
                 clickhouse_version, domain_activity)
            VALUES
                (4, 'measured', '2026-11-27', '2026-11-27 01:25:00', '1.3.0', '26.1.0',
                 [('live.example', 2, 7, 2), ('new.example', 1, 2, NULL)]),
                (4, 'measured', '2026-11-27', '2026-11-27 01:20:00', '1.3.0', '26.1.0',
                 [('live.example', 3, 5, 1)]),
                (4, 'a', '2026-11-27', '2026-11-27 01:25:00', '1.3.0', '26.1.0',
                 [('shared.example', 1, 3, 1)]),
                (4, 'z', '2026-11-27', '2026-11-27 01:20:00', '1.2.0', '26.1.0',
                 [('shared.example', 1, 2, 1)]),
                (4, 'measured', '2026-11-26', '2026-11-26 23:55:00', '1.3.0', '26.1.0',
                 [('live.example', 3, 20, 2)]),
                (4, 'capped', '2026-11-26', '2026-11-26 23:55:00', '1.3.0', '26.1.0',
                 [('capped.example', 1, 3, 1), ('removed.example', 2, 8, 1)]),
                (4, 'internal', '2026-11-27', '2026-11-27 01:25:00', '1.3.0', '26.1.0',
                 [('internal.example', 1, 30, 1)]),
                (4, 'smoke', '2026-11-27', '2026-11-27 01:25:00', '1.3.0', '0.0.0-smoke',
                 [('smoke.example', 1, 50, 1)]),
                (4, 'stale', '2026-11-26', '2026-11-26 23:25:00', '1.3.0', '26.1.0',
                 [('stale.example', 1, 30, 1)])
            """,
        ],
        (telemetry / "public-adoption.sql").read_text(),
    )

    directories = [json.loads(row) for row in report["domain_directories"]]
    assert all(
        row["domains"]
        == ["live.example", "shared.example", "capped.example", "new.example", "stale.example"]
        for row in directories
    )
    domain_rows = {
        row["domain"]: row
        for row in map(json.loads, report["domain_accounts"])
        if row["range_days"] == 30
    }
    assert domain_rows["live.example"] == {
        "range_days": 30,
        "domain": "live.example",
        "active_installations": 1,
        "successful_requests": 30,
        "active_accounts": 2,
        "serving_processes": 2,
        "loom_version": "1.3.0",
        "day": "2026-11-27",
        "first_seen": "2026-11-26",
        "last_seen": "2026-11-27",
    }
    assert domain_rows["new.example"]["successful_requests"] == 2
    assert domain_rows["capped.example"]["successful_requests"] == 4
    assert domain_rows["capped.example"]["active_accounts"] == 1
    assert domain_rows["capped.example"]["loom_version"] == "1.2.0"
    assert "stale.example" not in domain_rows
    assert domain_rows["shared.example"]["active_installations"] == 2
    assert domain_rows["shared.example"]["loom_version"] == "1.3.0"
