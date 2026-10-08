"""The exposure ledger: which token saw which memories.

An agent's memory leakage can be measured as the number of distinct records an attacker
extracts across many prompts. The other logs cannot compute that after the fact:
`search_trace_log` is off by default and keyed on the client-asserted `agent`;
`memory_usage_events` records ids without a request; `agent_events` records a query
digest without ids. This module records one row per read call with the verified token
subject, the returned keys and a query digest — never the query text, which is what kept
the trace log off — so it can stay on by default, and a materialized view keeps that
metric (distinct keys per actor per hour) current.

Fire-and-forget: observability only, never read by retrieval; a failed
write is logged and swallowed. Gated by
`LOOM_MEMORY_EXPOSURE_LEDGER_ENABLED` (default on).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from server.core.datetimes import naive_utc_now

log = logging.getLogger(__name__)

TABLE = "loom.memory_exposures"
BREADTH_TABLE = "loom.memory_exposure_breadth_hourly"

_SENTINEL = "-"


def query_digest(text: str | None) -> str:
    """`sha256:<16 hex>/<chars>` — the agent_events.args shape.
    Same query, same digest, so an operator can correlate without reading it."""
    if not text:
        return ""
    h = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"sha256:{h}/{len(text)}"


def _ident(value: Any) -> str:
    v = str(value or "").strip()
    return v if v else _SENTINEL


def build_row(
    *,
    operation: str,
    org: str,
    namespace: str,
    agent: str = "",
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    read_scope: str = "namespace",
    query: str | None = None,
    top_k: int = 0,
    keys: list[str],
    below_floor: int = 0,
    auth: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Pure builder for one ledger row; unit-tested without ClickHouse."""
    return {
        "ts": naive_utc_now(),
        "org": org,
        "namespace": namespace,
        "actor": str(getattr(auth, "subject", "") or "") if auth is not None else "",
        "token_id": str(getattr(auth, "token_id", "") or "") if auth is not None else "",
        "agent": str(agent or ""),
        "user_id": _ident(user_id),
        "session_id": _ident(session_id),
        "run_id": _ident(run_id),
        "operation": operation,
        "request_id": str(request_id or ""),
        "read_scope": str(read_scope or "namespace"),
        "query_digest": query_digest(query),
        "top_k": int(top_k or 0),
        "returned_keys": [str(k) for k in keys],
        "returned_count": len(keys),
        "below_floor_count": int(below_floor or 0),
    }


def enabled() -> bool:
    from server.settings import settings

    return bool(settings.memory_exposure_ledger_enabled)


def record_exposure(
    *,
    operation: str,
    org: str,
    namespace: str,
    keys: list[str],
    agent: str = "",
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    read_scope: str = "namespace",
    query: str | None = None,
    top_k: int = 0,
    below_floor: int = 0,
) -> dict[str, Any] | None:
    """Append one row for a read that returned `keys`. Returns the row it
    scheduled (or None when the ledger is off or nothing was returned) so a
    caller can assert on it without a database."""
    if not enabled() or not keys:
        return None
    from server.auth.context import current_auth
    from server.core.logging_config import request_id_var
    from server.observability.run_context import ledger_run_for

    row = build_row(
        operation=operation,
        org=org,
        namespace=namespace,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        # The read's own run (a scope) when it carried one, else the run the transport
        # bound the request to — the join key the usage fold derives `acted` from.
        run_id=ledger_run_for(run_id),
        read_scope=read_scope,
        query=query,
        top_k=top_k,
        keys=keys,
        below_floor=below_floor,
        auth=current_auth(),
        request_id=request_id_var.get() or "",
    )

    async def _bg() -> None:
        try:
            from server.core.ch import get_client

            await get_client().insert(
                TABLE,
                [row],
                settings={"async_insert": 1, "wait_for_async_insert": 0},
            )
        except Exception as e:  # noqa: BLE001 — telemetry never fails a read
            log.warning("exposure-ledger write failed (non-fatal): %s", e)

    from server.memory.app import after_write

    # No running loop (sync caller / unit test): built, not written.
    after_write.schedule(_bg(), name="memory.exposure_ledger")
    return row


def keys_of(hits: list[Any]) -> tuple[list[str], int]:
    """(returned keys, below-floor count) from a hit list; computed rows
    (`source == "computed"`) carry no stored memory and are skipped."""
    keys: list[str] = []
    below = 0
    for h in hits:
        if getattr(h, "source", "") == "computed":
            continue
        k = getattr(h, "memory_key", "")
        if not k:
            continue
        keys.append(str(k))
        comps = getattr(h, "score_components", None)
        if isinstance(comps, dict) and comps.get("below_floor"):
            below += 1
    return keys, below


SUMMARY_SQL = f"""
SELECT
    actor,
    agent,
    uniqExactMerge(keys_state) AS distinct_keys,
    uniqExactMerge(calls_state) AS calls,
    min(hour) AS first_hour,
    max(hour) AS last_hour
FROM {BREADTH_TABLE}
WHERE org = {{org:String}} AND namespace = {{ns:String}}
  AND hour >= now() - toIntervalHour({{hours:UInt32}})
GROUP BY actor, agent
ORDER BY distinct_keys DESC
LIMIT {{lim:UInt32}}
"""

NAMESPACE_ROWS_SQL = """
SELECT count() AS n FROM loom.memory FINAL
WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
"""


async def summarize(
    org: str, namespace: str, *, since_hours: int = 24, limit: int = 50
) -> tuple[list[dict[str, Any]], int]:
    """Per-(actor, agent) distinct keys and calls over the window, plus the
    namespace's live row count so the caller can report the fraction —
    distinct keys extracted over memory size."""
    from server.core.ch import get_client

    ch = get_client()
    rows = await ch.query_all(
        SUMMARY_SQL,
        params={"org": org, "ns": namespace, "hours": int(since_hours), "lim": int(limit)},
    )
    total = await ch.query_one(NAMESPACE_ROWS_SQL, params={"org": org, "ns": namespace})
    return rows, int((total or {}).get("n") or 0)
