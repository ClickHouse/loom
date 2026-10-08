"""The conflict judge off the write's critical path.

`memory.set` runs a cosine conflict check on every searchable write; when a
peer sits in the judge's band the check makes one cheap LLM call before
the row is written: about a third of sets, at +1.0 s each (judged sets
2.1–2.3 s end to end). The deterministic tiers — canonical merge, the pinned 409,
templated substitution, the duplicate tier — stay inline; only the LLM call
moves.

Shape (two-phase, on the write): the row is inserted `active`
at once and the response says `conflict_verdict: pending`, `verdict_at:
deferred`; a task then runs the very same judge-and-fold the inline path
runs (`conflicts.settle_deferred_judgement`) and lands the verdict on the
row: `conflict` → the conflict marks, `refines` → the peers superseded and
linked, `merge` / `duplicate` → this row becomes `status=duplicate,
duplicate_of=<peer>` (a merge cannot un-insert; under a run this is what
the inline path writes too), `clear` → nothing. `memory.get`
shows the settled `status` / `duplicate_of` and a derived
`conflict_verdict` (`pending` while this process still holds the judge).
`await_verdict: true` on the call keeps the inline behaviour; `dry_run`
always judges inline (the verdict is the answer).

The task is per request (`asyncio.create_task`, request context kept, the
same shape as memory.ask's cited-used credit): the judge's statements and
the generation span carry the request id that wrote the row. Every settle
is one `memory_drops` row (`stage=conflict.deferred`, `reason=<verdict>`,
`item=<memory_key>`) so the settled distribution is one GROUP BY away, and
the process drains in-flight judges on shutdown. A judge lost to a crash
leaves the row `active`, which is the cosine verdict on a band-only check
and the fail-open the inline judge already had; the conflict sweep worker
re-checks rows without a watermark.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from server.core.background import register_deferred_task

log = logging.getLogger(__name__)

PENDING = "pending"
DEFERRED = "deferred"
INLINE = "inline"
# The ledger row the settle writes says `verdict_at=settled`.
SETTLED = "settled"
SETTLE_OPERATION = "memory.set.settle"
DROP_STAGE = "conflict.deferred"

_TASKS: set[asyncio.Task[Any]] = set()
_PENDING: dict[str, float] = {}

# What `memory.get` reports for a row from its lifecycle status.
_STATUS_VERDICT = {
    "active": "clear",
    "conflicted": "conflict",
    "duplicate": "duplicate",
    "superseded": "superseded",
    "rejected": "rejected",
    "tombstoned": "tombstoned",
}


def wanted(input: Any) -> bool:
    """Defer this write's judge: the caller did not ask to wait
    (`await_verdict`) and this is a real write (a dry run's verdict IS its
    answer)."""
    return not bool(getattr(input, "await_verdict", False)) and not bool(
        getattr(input, "dry_run", False)
    )


def verdict_at(conflict_verdict: str) -> str:
    """`deferred` for a pending verdict, `inline` for one settled in the
    response, '' when no check ran."""
    if not conflict_verdict:
        return ""
    return DEFERRED if conflict_verdict == PENDING else INLINE


def is_pending(memory_id: str) -> bool:
    return str(memory_id or "") in _PENDING


def pending_count() -> int:
    return len(_PENDING)


def verdict_from_row(memory_id: str, status: Any, duplicate_of: Any = "") -> str:
    """The verdict a row shows on read: `pending` while this process still
    holds its judge, else derived from the lifecycle status (`active` →
    `clear`, `conflicted` → `conflict`, `duplicate` → `duplicate`, …)."""
    if is_pending(memory_id):
        return PENDING
    s = str(status or "")
    if s == "active" and str(duplicate_of or ""):
        return "duplicate"
    return _STATUS_VERDICT.get(s, s)


def schedule_settle(
    ident: Any, conflict_result: Any, *, memory_id: str, cfg: Any = None
) -> asyncio.Task[str] | None:
    """After the row is in: run the deferred judge for a `pending` result.
    None when there is nothing pending or no running loop."""
    pj = getattr(conflict_result, "pending_judge", None)
    if pj is None or getattr(conflict_result, "verdict", "") != PENDING:
        return None
    from server.core.logging_config import request_id_var

    request_id = str(request_id_var.get() or "")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None  # no running loop (sync harness): the row stays active
    task = register_deferred_task(
        loop.create_task(
            settle(ident, pj, memory_id=memory_id, cfg=cfg, request_id=request_id),
            name="loom-conflict-judge",
        )
    )
    _PENDING[str(memory_id)] = time.monotonic()
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def settle(
    ident: Any, pj: Any, *, memory_id: str, cfg: Any = None, request_id: str = ""
) -> str:
    """Judge, fold and land the verdict on the row; returns the settled
    verdict (`error` when the settle itself failed — the row stays as
    inserted). Never raises. Writes one `memory_drops` row and one ledger
    row (`memory.set.settle`, the write's `request_id`)."""
    from server.memory.app import conflicts as cf
    from server.memory.app.drops import record_drop
    from server.observability import cost_accumulator

    started = time.monotonic()
    # A fresh accumulator in this task's context copy, so the judge's call
    # bills the settle row: the request's row was written when the response
    # went out, so otherwise the spend would show on the generation span only.
    cost_accumulator.begin_op()
    verdict = "error"
    result: Any = None
    try:
        result = await cf.settle_deferred_judgement(pj)
        verdict = await apply(ident, result, memory_id=memory_id, cfg=cfg)
    except Exception as e:  # noqa: BLE001 — the row is in; the verdict is best effort
        log.warning(
            "deferred conflict judge failed for %s/%s key=%r: %s",
            ident.org,
            ident.namespace,
            ident.memory_key,
            e,
        )
    finally:
        _PENDING.pop(str(memory_id), None)
    record_drop(ident.org, ident.namespace, DROP_STAGE, verdict, item=str(ident.memory_key))
    latency_ms = int((time.monotonic() - started) * 1000)
    emit_settle_row(
        ident,
        result,
        memory_id=memory_id,
        verdict=verdict,
        request_id=request_id,
        latency_ms=latency_ms,
    )
    log.info(
        "deferred conflict judge settled %s/%s key=%r → %s in %d ms",
        ident.org,
        ident.namespace,
        ident.memory_key,
        verdict,
        latency_ms,
    )
    return verdict


def settled_duplicate_of(result: Any, verdict: str) -> str:
    """The peer a settled `merge` / `duplicate` pointed this row at; '' otherwise."""
    if result is None or verdict not in ("merge", "duplicate"):
        return ""
    peer = result.existing_memory_id if verdict == "merge" else result.duplicate_of_id
    return str(peer or "")


def emit_settle_row(
    ident: Any,
    result: Any,
    *,
    memory_id: str,
    verdict: str,
    request_id: str,
    latency_ms: int,
) -> None:
    """One `loom.agent_events` row per settled judge —
    `operation='memory.set.settle'`, `kind='settle'`, the SAME `request_id`
    as the `memory.set` row, `out_conflict_verdict=<settled>`,
    `out_verdict_at='settled'`, `out_duplicate_of`, `out_memory_id`, the
    judge's `llm_calls` / cost / model, `latency_ms` = insert → verdict
    landed. The set's own row keeps `pending` / `deferred` (the table is
    append-only); the settled verdict is `argMax(args['out_conflict_verdict'],
    ts) … GROUP BY request_id` over the two operations. Not a registered op
    and never counted as a call: op-count readers filter `operation =
    'memory.set'`. Never raises."""
    try:
        from server.observability.ledger import emit_agent_event

        raw_args: dict[str, Any] = {}
        for k in ("run_id", "consumer"):
            v = getattr(ident, k, None)
            if v not in (None, ""):
                raw_args[k] = v
        # The transport helper bills the accumulator bound in this context —
        # `settle`'s fresh one — so the row carries the judge's call and
        # nothing of the request's.
        emit_agent_event(
            op_name=SETTLE_OPERATION,
            identity={
                "org": str(getattr(ident, "org", "") or ""),
                "namespace": str(getattr(ident, "namespace", "") or ""),
                "agent": str(getattr(ident, "agent", "") or ""),
                "user_id": str(getattr(ident, "user_id", "") or ""),
                "session_id": str(getattr(ident, "session_id", "") or ""),
            },
            request_id=request_id,
            latency_ms=int(latency_ms),
            success=verdict != "error",
            error_code="" if verdict != "error" else "SETTLE_FAILED",
            memory_key=str(getattr(ident, "memory_key", "") or ""),
            raw_args=raw_args,
            kind="settle",
            result={
                "conflict_verdict": verdict,
                "verdict_at": SETTLED,
                "memory_id": str(memory_id),
                **({"duplicate_of": dup} if (dup := settled_duplicate_of(result, verdict)) else {}),
            },
        )
    except Exception:  # ledger telemetry: the verdict already stands
        log.debug("settle ledger row emit failed", exc_info=True)


async def apply(ident: Any, result: Any, *, memory_id: str, cfg: Any = None) -> str:
    """Land a settled `ConflictResult` on a row that was inserted `active`:
    the same post-insert steps the inline path takes for `conflict` and
    `refines`; `merge` / `duplicate` flip this row to `status=duplicate`
    pointing at the peer (and a `merge` still reinforces the peer). Returns
    the judge's verdict."""
    from server.core.ch import get_client
    from server.memory.app import conflicts as cf

    verdict = str(getattr(result, "verdict", "") or "")
    if verdict == "conflict" and result.peers:
        await cf.mark_peers_conflicted(
            org=ident.org, new_memory_id=memory_id, peer_ids=[p.memory_id for p in result.peers]
        )
    elif (
        verdict == "refines"
        and result.refined_peers
        and not getattr(ident, "proposed", False)  # a trial fact supersedes nothing
    ):
        await cf.apply_refines(
            org=ident.org,
            namespace=ident.namespace,
            new_memory_id=memory_id,
            new_memory_key=ident.memory_key,
            refined_peers=result.refined_peers,
            ch=get_client(),
            relation=result.refine_relation,
            run_id=str(getattr(ident, "run_id", "-") or "-"),
        )
    elif verdict in ("merge", "duplicate"):
        peer_id = str(
            (result.existing_memory_id if verdict == "merge" else result.duplicate_of_id) or ""
        )
        await mark_duplicate(ident.org, memory_id, peer_id)
        if verdict == "merge":
            await cf.auto_reinforce_peer(ident, peer_id, ch=get_client(), cfg=cfg)
    return verdict


async def mark_duplicate(org: str, memory_id: str, peer_id: str) -> int:
    """This row becomes the peer's duplicate: a new RMT version with
    `status=duplicate`, `duplicate_of=<peer>` on loom.memory, mirrored onto
    the embeddings row by the version writer."""
    import server.memory.store as memory_repo

    if not peer_id or peer_id == memory_id:
        return 0
    return await memory_repo.write_versions_by_ids(
        org, [memory_id], lambda _row: {"status": "duplicate", "duplicate_of": peer_id}
    )


async def drain(timeout_s: float = 15.0) -> int:
    """Wait for in-flight judges (shutdown). Returns how many were pending."""
    pending = [t for t in _TASKS if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout_s)
    return len(pending)


def _reset_for_tests() -> None:
    _TASKS.clear()
    _PENDING.clear()


__all__ = [
    "DEFERRED",
    "DROP_STAGE",
    "INLINE",
    "PENDING",
    "SETTLED",
    "SETTLE_OPERATION",
    "apply",
    "drain",
    "emit_settle_row",
    "is_pending",
    "mark_duplicate",
    "pending_count",
    "schedule_settle",
    "settle",
    "settled_duplicate_of",
    "verdict_at",
    "verdict_from_row",
    "wanted",
]
