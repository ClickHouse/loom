"""Aggregate a reporting window from the ordinary ledger for adoption telemetry."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from server import __version__
from server.core.ch import CHClient, sequential_reads
from server.core.naming import not_mirror_sql

_REQUEST_SCOPE = f"""
ts >= {{start:DateTime('UTC')}} AND ts < {{end:DateTime('UTC')}}
  AND kind = 'request' AND slo_class != ''
  AND traffic_class NOT IN ('bench', 'background')
  AND mirror_marked = 0 AND {not_mirror_sql()}
"""

ADOPTION_SQL = f"""
SELECT
    2::UInt8 AS schema_version,
    {{installation_id:String}} AS installation_id,
    {{loom_version:String}} AS loom_version,
    version() AS clickhouse_version,
    (SELECT count() FROM loom.namespaces FINAL
     WHERE active = 1 AND {not_mirror_sql()}) AS total_namespaces,
    count() AS requests,
    countIf(status_class = 'ok') AS successful_requests,
    countIf(status_class = 'server') AS server_errors,
    uniqIf(org, status_class = 'ok' AND org NOT IN ('', '-')) AS active_orgs,
    uniqIf(tuple(org, namespace), status_class = 'ok' AND namespace NOT IN ('', '-'))
        AS active_namespaces,
    uniqIf(tuple(org, agent), status_class = 'ok' AND agent NOT IN ('', '-')) AS active_agents,
    uniqIf(tuple(org, namespace, session_id), status_class = 'ok' AND session_id NOT IN ('', '-'))
        AS active_sessions,
    countIf(status_class = 'ok' AND startsWith(operation, 'memory.')) AS memory_requests,
    countIf(status_class = 'ok' AND startsWith(operation, 'code.')) AS code_requests,
    countIf(status_class = 'ok' AND startsWith(operation, 'knowledge.')) AS knowledge_requests,
    countIf(status_class = 'ok' AND startsWith(operation, 'conversation.'))
        AS conversation_requests,
    countIf(status_class = 'ok' AND startsWith(operation, 'tracker.')) AS tracker_requests,
    countIf(status_class = 'ok' AND startsWith(operation, 'task.')) AS task_requests,
    countIf(status_class = 'ok' AND transport = 'mcp') AS mcp_requests,
    countIf(status_class = 'ok' AND transport = 'http') AS http_requests,
    sum(llm_calls) AS llm_calls,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens,
    sum(embedding_tokens) AS embedding_tokens
FROM loom.agent_events
WHERE {_REQUEST_SCOPE}
SETTINGS optimize_move_to_prewhere_if_final = 0
"""

# Attribute requests to the contact domain of their namespace.
DOMAINS_SQL = f"""
SELECT contacts.domain, uniqExact(contacts.email) AS user_count,
       sum(activity.requests) AS successful_requests
FROM (
    SELECT
        org, namespace,
        lowerUTF8(trimBoth(contact_email)) AS email,
        extract(email, '^[^@[:space:]]+@([a-z0-9.-]+)$') AS domain
    FROM loom.namespaces FINAL
    WHERE active = 1 AND classification = 'standard' AND {not_mirror_sql()}
      AND (org, namespace) NOT IN {{restricted:Array(Tuple(String, String))}}
      AND contact_email != ''
) AS contacts
LEFT JOIN (
    SELECT org, namespace, count() AS requests
    FROM loom.agent_events
    WHERE {_REQUEST_SCOPE} AND status_class = 'ok'
    GROUP BY org, namespace
) AS activity USING (org, namespace)
WHERE length(contacts.domain) <= 253
  AND match(contacts.domain, concat('^(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?[.])+',
                           '(?:[a-z]{{2,63}}|xn--[a-z0-9](?:[a-z0-9-]{{0,57}}[a-z0-9])?)$'))
GROUP BY contacts.domain
ORDER BY successful_requests DESC, user_count DESC, contacts.domain
LIMIT 30
SETTINGS optimize_move_to_prewhere_if_final = 0
"""


async def window_report(
    start: datetime,
    end: datetime,
    installation_id: UUID,
    *,
    restricted: set[tuple[str, str]] | None,
    ch: CHClient,
) -> dict[str, Any]:
    params = {
        "start": start,
        "end": end,
        "installation_id": str(installation_id),
        "loom_version": __version__,
    }
    with sequential_reads():
        domains = (
            await ch.query_all(
                DOMAINS_SQL, {"start": start, "end": end, "restricted": sorted(restricted)}
            )
            if restricted is not None
            else []
        )
        # Read totals last so late ledger rows can only increase the total.
        report = await ch.query_one(ADOPTION_SQL, params)
    if report is None:
        raise RuntimeError("adoption aggregation returned no row")
    return {
        **report,
        "day": start.date().isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "user_domains": domains,
    }
