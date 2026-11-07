"""What a request against a restricted namespace leaves behind.

Three writers run after the authorization gate and would otherwise record
what the gate protected: the agent_events argument projection (query /
question text), the code_search_trace row (query text, returned paths) and
the audit ledger, which otherwise records nothing for a plain read. For a
restricted namespace the first two keep identifiers and digests and drop
the text, and the third gains one row per request — the same shape as
`memory.sensitive_read`, per request rather than per row.

Every check here is `is_restricted_cached`: synchronous, no I/O, answered
from the snapshot the gate populated a moment earlier in the same request.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from server.core.background import register_deferred_task

log = logging.getLogger(__name__)

EVENT_TYPE = "namespace.restricted_access"


def digest_text(text: str) -> str:
    """`sha256:<hex>` of the text — what a restricted trace stores in place
    of the query, so repeats are still countable."""
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# The audit row's INSERT is written beside the request. The
# set holds the live writes of the running loop; `drain()` at shutdown lets
# them land before the ClickHouse client goes away.
_TASKS: set[asyncio.Task[Any]] = set()


def _schedule(coro: Any) -> None:
    loop = asyncio.get_running_loop()
    # only live tasks of this loop stay in the set: a task of a closed loop can
    # never complete or be discarded
    for stale in [t for t in _TASKS if t.done() or t.get_loop() is not loop]:
        _TASKS.discard(stale)
    task = loop.create_task(coro)
    register_deferred_task(task)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(_note_failure)


def _note_failure(task: asyncio.Task[Any]) -> None:
    # `audit.emit` never raises; a test double may — retrieve it so the loop
    # does not log "exception was never retrieved"
    if not task.cancelled() and task.exception() is not None:
        log.debug("restricted access audit write failed: %s", task.exception())


async def drain(timeout_s: float = 5.0) -> int:
    """Wait for the audit rows still being written (at most `timeout_s`);
    returns how many were in flight. Shutdown calls it before the client
    disconnects; tests call it to observe the row."""
    live = [t for t in _TASKS if not t.done()]
    if live:
        await asyncio.wait(live, timeout=timeout_s)
    return len(live)


async def note_restricted_access(
    auth: Any,
    *,
    op: str,
    org: str | None,
    namespace: str | None,
    transport: str,
    granted_to: str = "",
) -> bool:
    """One audit row for a request the gate admitted to a restricted
    namespace. Returns True when the row was written (or, with
    `restricted_audit_inline` off, scheduled). Never raises.

    By default the row is written and waited for before the handler runs, so a
    process killed after answering never loses it; the read pays that insert's
    round trip, up to `ch_batched_insert_wait_max_ms` of batching, and the part
    write. `restricted_audit_inline=false` writes it beside the request instead
    (same row, same hash, drained at shutdown) and accepts losing it to a
    SIGKILL."""
    if auth is None or not org or not namespace:
        return False
    try:
        from server.identity.app.namespace_classification import is_restricted_cached
        from server.settings import settings

        if not is_restricted_cached(str(org), str(namespace)):
            return False
        from server.memory.domain.trust import actor_for
        from server.observability import audit

        write = audit.emit(
            EVENT_TYPE,
            org=str(org),
            namespace=str(namespace),
            actor=actor_for(
                str(getattr(auth, "subject", "") or "-"),
                is_service_account=bool(getattr(auth, "is_service_account", False)),
            ),
            subject={
                "op": op,
                "transport": transport,
                "token_id": str(getattr(auth, "token_id", "") or ""),
                "roles": [r.name.lower() for r in getattr(auth, "roles", ())],
                # a mint: the token subject the namespace was granted to
                **({"granted_to": granted_to} if granted_to else {}),
            },
        )
        if settings.restricted_audit_inline:
            await write
        else:
            _schedule(write)
        return True
    except Exception as e:  # noqa: BLE001 — the ledger never fails the request
        log.debug("restricted access audit skipped: %s", e)
        return False


async def note_restricted_grants(
    auth: Any, *, op: str, org: str, namespaces: list[str], granted_to: str
) -> None:
    """The row of each restricted namespace a token mint granted `granted_to`,
    written concurrently."""
    await asyncio.gather(
        *(
            note_restricted_access(
                auth, op=op, org=org, namespace=ns, transport="http", granted_to=granted_to
            )
            for ns in namespaces
        )
    )


__all__ = ["EVENT_TYPE", "digest_text", "drain", "note_restricted_access", "note_restricted_grants"]
