"""Aggregate a reporting window from the ordinary ledger for adoption telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from clickhouse_connect.driver.exceptions import DatabaseError

from server import __version__
from server.core.ch import CHClient, sequential_reads
from server.core.ch_errors import error_code
from server.core.naming import not_mirror_sql

_REQUEST_SCOPE = f"""
ts >= {{start:DateTime('UTC')}} AND ts < {{end:DateTime('UTC')}}
  AND kind = 'request' AND slo_class != ''
  AND traffic_class NOT IN ('bench', 'background')
  AND mirror_marked = 0 AND {not_mirror_sql()}
"""

# The transport writes the authenticated subject independently of the request's user_id.
_IDENTIFIED_ACCOUNT = "org NOT IN ('', '-', '*') AND subject NOT IN ('', '-', '*')"
# The ledger stamps each process as hostname:pid and uses unknown when no host is available.
_KNOWN_PROCESS = "instance NOT IN ('', '-') AND NOT startsWith(instance, 'unknown:')"
_DAILY_COUNTS = ("daily_successful_requests", "active_accounts", "serving_processes")

ADOPTION_SQL = f"""
WITH
    ts >= {{window_start:DateTime('UTC')}} AS in_window,
    status_class = 'ok' AND in_window AS window_success,
    status_class = 'ok' AND {{complete_day:UInt8}} = 1 AS day_success
SELECT
    {{installation_id:String}} AS installation_id,
    {{loom_version:String}} AS loom_version,
    version() AS clickhouse_version,
    (SELECT count() FROM loom.namespaces FINAL
     WHERE active = 1 AND {not_mirror_sql()}) AS total_namespaces,
    countIf(in_window) AS requests,
    countIf(window_success) AS successful_requests,
    countIf(in_window AND status_class = 'server') AS server_errors,
    uniqIf(org, window_success AND org NOT IN ('', '-')) AS active_orgs,
    uniqIf(tuple(org, namespace), window_success AND namespace NOT IN ('', '-'))
        AS active_namespaces,
    uniqIf(tuple(org, agent), window_success AND agent NOT IN ('', '-')) AS active_agents,
    uniqIf(tuple(org, namespace, session_id), window_success AND session_id NOT IN ('', '-'))
        AS active_sessions,
    countIf(window_success AND startsWith(operation, 'memory.')) AS memory_requests,
    countIf(window_success AND startsWith(operation, 'code.')) AS code_requests,
    countIf(window_success AND startsWith(operation, 'knowledge.')) AS knowledge_requests,
    countIf(window_success AND startsWith(operation, 'conversation.'))
        AS conversation_requests,
    countIf(window_success AND startsWith(operation, 'tracker.')) AS tracker_requests,
    countIf(window_success AND startsWith(operation, 'task.')) AS task_requests,
    countIf(window_success AND transport = 'mcp') AS mcp_requests,
    countIf(window_success AND transport = 'http') AS http_requests,
    sumIf(llm_calls, in_window) AS llm_calls,
    sumIf(input_tokens, in_window) AS input_tokens,
    sumIf(output_tokens, in_window) AS output_tokens,
    sumIf(embedding_tokens, in_window) AS embedding_tokens,
    countIf(day_success) AS daily_successful_requests,
    uniqExactIf(tuple(org, subject), day_success AND {_IDENTIFIED_ACCOUNT}) AS active_accounts,
    if(daily_successful_requests = 0, 0,
       nullIf(uniqExactIf(instance, day_success AND {_KNOWN_PROCESS}), 0)) AS serving_processes
FROM loom.agent_events
WHERE {_REQUEST_SCOPE}
SETTINGS optimize_move_to_prewhere_if_final = 0
"""

_CONTACTS_SQL = f"""
    SELECT
        org, namespace,
        lowerUTF8(trimBoth(contact_email)) AS email,
        extract(email, '^[^@[:space:]]+@([a-z0-9.-]+)$') AS domain
    FROM loom.namespaces FINAL
    WHERE active = 1 AND classification = 'standard' AND {not_mirror_sql()}
      AND (org, namespace) NOT IN {{restricted:Array(Tuple(String, String))}}
      AND contact_email != ''
"""

_VALID_CONTACT_DOMAIN = """
length(contacts.domain) <= 253
  AND match(contacts.domain, concat('^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?[.])+',
                           '(?:[a-z]{2,63}|xn--[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)$'))
"""

# Attribute requests to the contact domain of their namespace.
DOMAINS_SQL = f"""
SELECT contacts.domain, uniqExact(contacts.email) AS user_count,
       sum(activity.requests) AS successful_requests
FROM ({_CONTACTS_SQL}) AS contacts
LEFT JOIN (
    SELECT org, namespace, count() AS requests
    FROM loom.agent_events
    WHERE {_REQUEST_SCOPE} AND status_class = 'ok'
    GROUP BY org, namespace
) AS activity USING (org, namespace)
WHERE {_VALID_CONTACT_DOMAIN}
GROUP BY contacts.domain
ORDER BY successful_requests DESC, user_count DESC, contacts.domain
LIMIT 30
SETTINGS optimize_move_to_prewhere_if_final = 0
"""

DAILY_DOMAIN_ACTIVITY_SQL = f"""
WITH eligible_contacts AS (
    SELECT * FROM ({_CONTACTS_SQL}) AS contacts WHERE {_VALID_CONTACT_DOMAIN}
)
SELECT
    contacts.domain,
    uniqExact(contacts.email) AS user_count,
    uniqExactIf(tuple(contacts.org, activity.subject), activity.identified) AS active_accounts,
    sum(activity.requests) AS successful_requests,
    if(successful_requests = 0, 0,
       nullIf(uniqExactIf(activity.instance, activity.known_process), 0)) AS serving_processes
FROM eligible_contacts AS contacts
LEFT JOIN (
    SELECT
        org, namespace, subject, instance,
        {_IDENTIFIED_ACCOUNT} AS identified,
        {_KNOWN_PROCESS} AS known_process,
        count() AS requests
    FROM loom.agent_events
    WHERE {_REQUEST_SCOPE} AND status_class = 'ok'
        AND (org, namespace) IN (SELECT org, namespace FROM eligible_contacts)
    GROUP BY org, namespace, subject, instance
) AS activity USING (org, namespace)
GROUP BY contacts.domain
HAVING successful_requests > 0
ORDER BY contacts.domain
SETTINGS optimize_move_to_prewhere_if_final = 0
"""


_DOMAIN_ACTIVITY_FIELDS = ("domain", "active_accounts", "successful_requests", "serving_processes")
_SNAPSHOT_READ_LIMITS = {
    "max_rows_to_read": 5_000_000,
    "max_bytes_to_read": 512 * 1024**2,
    "read_overflow_mode": "throw",
}
_READ_LIMIT_CODES = (158, 307, 396)  # TOO_MANY_ROWS, TOO_MANY_BYTES, TOO_MANY_ROWS_OR_BYTES
_MAX_SNAPSHOT_DOMAINS = 2000  # With domain names capped at 253 bytes, this fits in one MiB.
_LIVE_DOMAINS_SQL = f"""
SELECT * FROM ({DAILY_DOMAIN_ACTIVITY_SQL})
ORDER BY successful_requests DESC, domain
LIMIT {_MAX_SNAPSHOT_DOMAINS}
"""


async def domain_snapshot(
    end: datetime,
    installation_id: UUID,
    *,
    restricted: set[tuple[str, str]],
    ch: CHClient,
) -> dict[str, Any] | None:
    start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    params = {"start": start, "end": end, "restricted": sorted(restricted)}
    with sequential_reads():
        try:
            rows = await ch.query_all(_LIVE_DOMAINS_SQL, params, settings=_SNAPSHOT_READ_LIMITS)
        except DatabaseError as exc:
            if error_code(exc) in _READ_LIMIT_CODES:
                return None
            raise
        version = await ch.query_one("SELECT version() AS clickhouse_version")
    if version is None:
        raise RuntimeError("adoption ClickHouse version is unavailable")
    return {
        "schema_version": 4,
        "installation_id": str(installation_id),
        "day": start.date().isoformat(),
        "observed_at": end.isoformat(),
        "loom_version": __version__,
        "clickhouse_version": version["clickhouse_version"],
        "domain_activity": [{name: row[name] for name in _DOMAIN_ACTIVITY_FIELDS} for row in rows],
    }


async def window_report(
    start: datetime,
    end: datetime,
    installation_id: UUID,
    *,
    restricted: set[tuple[str, str]],
    ch: CHClient,
) -> dict[str, Any]:
    complete_day = end.hour == 0
    activity_params = {"start": end - timedelta(days=1) if complete_day else start, "end": end}
    params = {
        **activity_params,
        "window_start": start,
        "complete_day": int(complete_day),
        "installation_id": str(installation_id),
        "loom_version": __version__,
    }
    domain_params = {**activity_params, "restricted": sorted(restricted)}
    domain_activity: list[dict[str, Any]] = []
    with sequential_reads():
        if complete_day:
            activity = await ch.query_all(DAILY_DOMAIN_ACTIVITY_SQL, domain_params)
            domains = [
                {name: row[name] for name in ("domain", "user_count", "successful_requests")}
                for row in activity
            ]
            domain_activity = [
                {name: row[name] for name in _DOMAIN_ACTIVITY_FIELDS} for row in activity
            ]
        else:
            domains = await ch.query_all(DOMAINS_SQL, domain_params)
        # Read totals last so late ledger rows can only increase the total.
        report = await ch.query_one(ADOPTION_SQL, params)
    if report is None:
        raise RuntimeError("adoption aggregation returned no row")
    if complete_day:
        report["domain_activity"] = domain_activity
    else:
        for name in _DAILY_COUNTS:
            del report[name]
    return {
        "schema_version": 3 if complete_day else 2,
        **report,
        "day": start.date().isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "user_domains": domains,
    }
