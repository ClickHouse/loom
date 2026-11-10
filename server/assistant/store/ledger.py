"""The rows one assistant answer's tool calls left: its ledger rows (`loom.agent_events`) and
its memory reads (`loom.memory_exposures`)."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class RunCalls:
    """What an answer's tool calls left: the ops whose calls found data, the ops whose calls
    ran (found or not), and whether any call failed or was refused."""

    returned: frozenset[str]
    failed: bool
    ran: frozenset[str] = frozenset()


async def run_calls(
    org: str, run_id: str, subject: str, *, ch: _CHClient | None = None
) -> RunCalls:
    """The answer's own calls, by its run, the caller's subject and consumer `loom-assistant`.
    No namespace filter: a code call is ledgered under the code namespace and a refused call
    under the sentinel; the org keeps the read inside the tenant. A call found data unless its
    row says it found nothing (a miss, an empty list, an abstention)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT groupUniqArrayIf(operation, success "
        "AND args['out_found'] != 'false' AND args['out_found_n'] != '0' "
        "AND args['out_hits'] != '0' AND args['out_section_hits'] != '0' "
        "AND args['out_abstain'] != 'true') AS returned, "
        "countIf(NOT success) > 0 AS failed, "
        "groupUniqArrayIf(operation, success) AS ran "
        "FROM loom.agent_events "
        "WHERE org = {org:String} AND run_id = {run:String} AND subject = {subject:String} "
        "AND consumer = {consumer:String} AND ts > now() - INTERVAL 1 DAY",
        {"org": org, "run": run_id, "subject": subject, "consumer": ASSISTANT_CONSUMER},
    )
    row = rows[0] if rows else {}
    return RunCalls(
        frozenset(str(o) for o in row.get("returned") or ()),
        bool(row.get("failed")),
        frozenset(str(o) for o in row.get("ran") or ()),
    )
