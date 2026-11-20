"""The rows one assistant answer's tool calls left: its ledger rows (`loom.agent_events`) and
its memory reads (`loom.memory_exposures`)."""

from __future__ import annotations

from typing import Any

from server.assistant.domain.support import RUN_ROWS, CallRow
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.set_chunk_session import SESSION_PREFIX
from server.observability.consumer import ASSISTANT_CONSUMER
from server.observability.store.event_reads import MASKED_EVENTS


def _evidence(row: dict[str, Any] | None, column: str) -> set[str] | None:
    if not isinstance(row, dict):
        return None
    protected = row.get("protected")
    if type(protected) not in (bool, int) or protected != 0:
        return None
    values = row.get(column)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        return None
    return {value for value in values if value}


async def query_digests(
    org: str, namespace: str, run_id: str, subject: str, *, ch: _CHClient | None = None
) -> set[str] | None:
    """SQL evidence under the answer's identity, or unknown protected participation."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        SELECT max(mirror_marked) AS protected,
               groupUniqArrayIf(100)(args['out_sql_digest'],
                   success AND args['out_sql_digest'] != '') AS digests
        FROM {MASKED_EVENTS}
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND run_id = {{run:String}}
          AND subject = {{subject:String}} AND consumer = {{consumer:String}}
          AND operation = 'insights.query' AND ts > now() - INTERVAL 1 DAY
        """,
        {
            "org": org,
            "ns": namespace,
            "run": run_id,
            "subject": subject,
            "consumer": ASSISTANT_CONSUMER,
        },
    )
    return _evidence(row, "digests")


async def returned_keys(
    org: str,
    namespace: str,
    run_id: str,
    subject: str,
    keys: list[str],
    *,
    ch: _CHClient | None = None,
) -> set[str] | None:
    """Returned cited keys, or unknown when a protected call can leave no exposure."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        WITH (
            SELECT max(mirror_marked)
            FROM {MASKED_EVENTS}
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND run_id = {{run:String}}
              AND subject = {{subject:String}} AND consumer = {{consumer:String}}
              AND ts > now() - INTERVAL 1 DAY
        ) AS protected
        SELECT protected, groupUniqArray(100)(cited) AS keys
        FROM (
            SELECT DISTINCT if(startsWith(key, {{chunk:String}}),
                replaceRegexpOne(substring(key, length({{chunk:String}}) + 1), ':[0-9]+$', ''), key)
                AS cited
            FROM loom.memory_exposures ARRAY JOIN returned_keys AS key
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND actor = {{subject:String}}
              AND ts > now() - INTERVAL 1 DAY AND run_id = {{run:String}}
              AND has({{keys:Array(String)}}, cited)
            LIMIT 100
        )
        """,
        {
            "org": org,
            "ns": namespace,
            "run": run_id,
            "subject": subject,
            "keys": keys,
            "chunk": f"chunk:{SESSION_PREFIX}",
            "consumer": ASSISTANT_CONSUMER,
        },
    )
    return _evidence(row, "keys")


_ROW_COLUMNS = (
    "operation, success, args['out_found'] AS out_found, "
    "args['out_hits'] AS out_hits, mirror_marked"
)


def _rows(rows: list[dict[str, object]]) -> list[CallRow]:
    return [
        CallRow(
            str(r.get("operation") or ""),
            bool(r.get("success")),
            str(r.get("out_found") or ""),
            str(r.get("out_hits") or ""),
            protected=bool(r.get("mirror_marked")),
        )
        for r in rows
    ]


async def run_rows(
    org: str, run_id: str, subject: str, *, ch: _CHClient | None = None
) -> list[CallRow]:
    """The answer's own calls' rows, oldest first, by its run, the caller's subject and
    consumer `loom-assistant`, up to RUN_ROWS + 1 (one more says the run was cut). No
    namespace filter: a code call is ledgered under the code namespace and a refused call
    under the sentinel; the org keeps the read inside the tenant."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT {_ROW_COLUMNS} FROM {MASKED_EVENTS} "
        "WHERE org = {org:String} AND run_id = {run:String} AND subject = {subject:String} "
        "AND consumer = {consumer:String} AND ts > now() - INTERVAL 1 DAY "
        # request_id breaks a tie of two calls in one millisecond, so each read lists them alike.
        f"ORDER BY ts, request_id LIMIT {RUN_ROWS + 1}",
        {"org": org, "run": run_id, "subject": subject, "consumer": ASSISTANT_CONSUMER},
    )
    return _rows(rows)


async def live_rows(
    org: str,
    run_id: str,
    subject: str,
    *,
    namespace: str,
    started: float,
    ch: _CHClient | None = None,
) -> list[CallRow]:
    """The rows an answer still streaming has left so far, oldest first, up to RUN_ROWS + 1,
    read again from its start (`started`, epoch seconds) on each poll.

    Before the answer's run id is known: by the namespace (the primary key's prefix), the
    caller's subject, consumer `loom-assistant` and the second before `started`. The calls of
    the caller's other answers in the namespace that land then mix in, which progress text
    accepts; the answer's last event replaces them with its own run's when that run was
    read (else the dashboard keeps the live list, mixed or not). The one second takes three
    clocks to agree within it: this replica's (`started`), that of the replica that served
    the call (`ts`) and ClickHouse's (`ingested_at`). A wider window would take in the
    caller's previous answer; under more skew a row shows only once the run id is known.

    Once it is known: by the run, with no namespace filter (code and refused calls included)
    and a minute of clock skew."""
    ch = ch or _default_client()
    since_ms = int(started * 1000) - (60_000 if run_id else 1000)
    scope = "run_id = {run:String}" if run_id else "namespace = {ns:String}"
    rows = await ch.query_all(
        f"SELECT {_ROW_COLUMNS} FROM {MASKED_EVENTS} "
        f"WHERE org = {{org:String}} AND {scope} AND subject = {{subject:String}} "
        "AND consumer = {consumer:String} "
        "AND ts >= fromUnixTimestamp64Milli({since:Int64}) "
        # A constant lower bound, so the minmax index on ingested_at prunes parts.
        "AND ingested_at >= fromUnixTimestamp64Milli({since:Int64}) "
        # request_id breaks a tie of two calls in one millisecond, so each read lists them alike.
        f"ORDER BY ts, request_id LIMIT {RUN_ROWS + 1}",
        {
            "org": org,
            "ns": namespace,
            "run": run_id,
            "subject": subject,
            "consumer": ASSISTANT_CONSUMER,
            "since": since_ms,
        },
    )
    return _rows(rows)
