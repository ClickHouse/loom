"""The rows one assistant answer's tool calls left: its ledger rows (`loom.agent_events`) and
its memory reads (`loom.memory_exposures`)."""

from __future__ import annotations

from server.assistant.domain.support import RUN_ROWS, CallRow
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.set_chunk_session import SESSION_PREFIX
from server.observability.consumer import ASSISTANT_CONSUMER


async def query_digests(
    org: str, namespace: str, run_id: str, subject: str, *, ch: _CHClient | None = None
) -> set[str]:
    """The `sql_digest` each `insights.query` call of the answer's run returned (its
    ledger row's `out_sql_digest`): the answer's tool calls, made under the caller's
    subject as consumer `loom-assistant`."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT DISTINCT args['out_sql_digest'] AS digest FROM loom.agent_events "
        "WHERE org = {org:String} AND namespace = {ns:String} AND run_id = {run:String} "
        "AND subject = {subject:String} AND consumer = {consumer:String} "
        "AND operation = 'insights.query' AND success AND ts > now() - INTERVAL 1 DAY "
        # An answer makes a handful of tool calls.
        "LIMIT 100",
        {
            "org": org,
            "ns": namespace,
            "run": run_id,
            "subject": subject,
            "consumer": ASSISTANT_CONSUMER,
        },
    )
    return {str(r["digest"]) for r in rows if r.get("digest")}


async def returned_keys(
    org: str,
    namespace: str,
    run_id: str,
    subject: str,
    keys: list[str],
    *,
    ch: _CHClient | None = None,
) -> set[str]:
    """Which of `keys` the answer's tool calls were returned (`loom.memory_exposures`,
    which memory.search, ask, get, list and inspect record under the request's run and
    subject); a search hit on one of a key's chunks (`chunk:memory.set:<key>:<n>`) counts
    as its key."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        # A chunk id is `chunk:memory.set:<key>:<index>`: its key is what lies between the
        # prefix and the last `:<digits>`, compared exactly (a key may hold `:` itself).
        "SELECT DISTINCT if(startsWith(key, {chunk:String}), "
        "replaceRegexpOne(substring(key, length({chunk:String}) + 1), ':[0-9]+$', ''), key) "
        "AS cited FROM loom.memory_exposures ARRAY JOIN returned_keys AS key "
        "WHERE org = {org:String} AND namespace = {ns:String} AND actor = {subject:String} "
        "AND ts > now() - INTERVAL 1 DAY AND run_id = {run:String} "
        "AND has({keys:Array(String)}, cited) "
        # At most one row per cited key.
        "LIMIT 100",
        {
            "org": org,
            "ns": namespace,
            "run": run_id,
            "subject": subject,
            "keys": keys,
            # A chunk hit's key: `chunk:` and the chunk's session (`memory.set:<key>`).
            "chunk": f"chunk:{SESSION_PREFIX}",
        },
    )
    return {str(r["cited"]) for r in rows if r.get("cited")}


_ROW_COLUMNS = "operation, success, args['out_found'] AS out_found, args['out_hits'] AS out_hits"


def _rows(rows: list[dict[str, object]]) -> list[CallRow]:
    return [
        CallRow(
            str(r.get("operation") or ""),
            bool(r.get("success")),
            str(r.get("out_found") or ""),
            str(r.get("out_hits") or ""),
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
        f"SELECT {_ROW_COLUMNS} FROM loom.agent_events "
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
        f"SELECT {_ROW_COLUMNS} FROM loom.agent_events "
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
