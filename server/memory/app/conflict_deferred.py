"""The conflict judge off the write's critical path: `memory.set` inserts the row
`active` with `conflict_verdict: pending`, and a task settles the verdict later.
The walkthrough is in docs/architecture/memory-architecture.md ("How a deferred
verdict settles"). Invariants:

- The deterministic tiers (canonical merge, pinned 409, templated substitution,
  duplicates) stay inline; only the LLM judge is deferred.
- The settle runs the same judge-and-fold as the inline path
  (`conflicts.settle_deferred_judgement`), in the writing request's context.
- A row is `pending` exactly while its embedding carries the pending mark
  (`store.conflict_marks`); the settle stamps it, and the leader's conflict sweep
  re-judges a mark a lost task left.
- `await_verdict: true` and `dry_run` judge inline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from server.core.background import register_deferred_task
from server.core.datetimes import naive_utc_now, to_naive_utc
from server.llm import customer
from server.memory.store import conflict_marks
from server.settings import settings

log = logging.getLogger(__name__)

PENDING = "pending"
DEFERRED = "deferred"
INLINE = "inline"
# The ledger row the settle writes says `verdict_at=settled`.
SETTLED = "settled"
SETTLE_OPERATION = "memory.set.settle"
# A settle whose write a newer one replaced before the verdict could land.
MOVED = "moved"
# The judge's call was held by the model-call switch: no verdict, nothing stamped,
# so the conflict sweep judges the write once the org is back.
HELD = "held"
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


def checked_at_for(conflict_result: Any) -> Any:
    """The `conflict_checked_at` a write's embedding row carries: the pending
    mark for a deferred judge, now for a verdict reached inline, NULL when no
    check ran."""
    verdict = str(getattr(conflict_result, "verdict", "") or "")
    if not verdict:
        return None
    return conflict_marks.PENDING_MARK if verdict == PENDING else naive_utc_now()


async def row_pending(row: Any) -> bool:
    """Whether this row's judge is still owed: this process's own judge, else the
    mark on its embedding row, read only for an `active` row written inside
    `PENDING_READ_SECONDS` while the judge is on (a settled verdict moves every
    other status; an older mark is read as settled)."""
    if str(getattr(row, "status", "") or "") != "active" or not settings.memory_conflict_judge:
        return False
    if is_pending(getattr(row, "memory_id", "")):
        return True
    written = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
    if not isinstance(written, datetime):
        return False
    age = (naive_utc_now() - to_naive_utc(written)).total_seconds()
    if age > conflict_marks.PENDING_READ_SECONDS:
        return False
    try:
        return await conflict_marks.key_has_pending_mark(
            str(row.org),
            str(row.namespace),
            str(row.agent),
            str(row.user_id),
            str(row.session_id),
            str(row.memory_key),
        )
    except Exception as e:  # noqa: BLE001 — a verdict label never fails the read
        log.warning("pending verdict read failed for key=%r: %s", row.memory_key, e)
        return False


async def stamp_checked(
    ident: Any, *, version: int, created_at: datetime, content_sha256: str = ""
) -> int:
    """The verdict landed: stamp the embedding rows the judged write (`version`,
    `created_at`, `content_sha256`) put on the key."""
    return await conflict_marks.stamp_checked(
        str(ident.org),
        str(ident.namespace),
        str(getattr(ident, "agent", "") or ""),
        str(getattr(ident, "user_id", "-") or "-"),
        str(getattr(ident, "session_id", "-") or "-"),
        str(ident.memory_key),
        version=int(version),
        created_at=created_at,
        content_sha256=content_sha256,
    )


async def still_current(ident: Any, content_sha256: str) -> bool:
    """Whether the key's live memory row is still the judged write: a verdict for
    an older revision must not land on the newer one, which owes its own judge.
    True when the write's hash is unknown (a row stored without
    `content_sha256`). The identity is the content: a later revision that wrote
    the same bytes back shares the verdict, which differs from its own only by
    what its peers did meanwhile."""
    import server.memory.store as memory_repo

    if not content_sha256:
        return True
    key = (
        str(ident.org),
        str(ident.namespace),
        str(getattr(ident, "agent", "") or ""),
        str(getattr(ident, "user_id", "-") or "-"),
        str(getattr(ident, "session_id", "-") or "-"),
        str(ident.memory_key),
    )
    rows = await memory_repo.memory_rows_by_identity_keys([key], sequential=True)
    mem = rows.get(key)
    return mem is not None and str(mem.content_sha256 or "") == content_sha256


def verdict_from_row(
    memory_id: str, status: Any, duplicate_of: Any = "", *, pending: bool = False
) -> str:
    """The verdict a row shows on read: `pending` while the row's judge is
    owed (`row_pending`), else derived from the lifecycle status (`active` →
    `clear`, `conflicted` → `conflict`, `duplicate` → `duplicate`, …)."""
    if pending:
        return PENDING
    s = str(status or "")
    if s == "active" and str(duplicate_of or ""):
        return "duplicate"
    return _STATUS_VERDICT.get(s, s)


def schedule_settle(
    ident: Any,
    conflict_result: Any,
    *,
    memory_id: str,
    version: int,
    created_at: datetime,
    content_sha256: str = "",
    cfg: Any = None,
) -> asyncio.Task[str] | None:
    """After the row is in (at `version`, its embedding at `created_at`): run the
    deferred judge for a `pending` result. None when there is nothing pending or no
    running loop."""
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
            settle(
                ident,
                pj,
                memory_id=memory_id,
                version=version,
                created_at=created_at,
                content_sha256=content_sha256,
                cfg=cfg,
                request_id=request_id,
            ),
            name="loom-conflict-judge",
        )
    )
    _PENDING[str(memory_id)] = time.monotonic()
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def settle(
    ident: Any,
    pj: Any,
    *,
    memory_id: str,
    version: int,
    created_at: datetime,
    content_sha256: str = "",
    cfg: Any = None,
    request_id: str = "",
) -> str:
    """Judge, fold and land the verdict on the row, then stamp the write's
    (`version`) `conflict_checked_at`; returns the settled verdict (`error` when the
    settle itself failed — the row stays as inserted, its mark left for the
    conflict sweep; `moved` when a newer write took the key while the judge ran,
    which lands nothing). Never raises. Writes one `memory_drops` row and one
    ledger row (`memory.set.settle`, the write's `request_id`)."""
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
        with customer.for_org(str(ident.org)):
            result = await cf.settle_deferred_judgement(pj)
        if await still_current(ident, content_sha256):
            verdict = await apply(
                ident, result, memory_id=memory_id, cfg=cfg, content_sha256=content_sha256
            )
        else:
            verdict = MOVED
    except customer.HOLDS:
        verdict = HELD
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
    if verdict not in ("error", MOVED, HELD):
        try:
            await stamp_checked(
                ident, version=version, created_at=created_at, content_sha256=content_sha256
            )
        except Exception as e:  # noqa: BLE001 — the verdict stands; the sweep re-judges at worst
            log.warning("conflict_checked_at stamp failed for key=%r: %s", ident.memory_key, e)
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


async def apply(
    ident: Any, result: Any, *, memory_id: str, cfg: Any = None, content_sha256: str = ""
) -> str:
    """Land a settled `ConflictResult` on a row that was inserted `active`:
    the same post-insert steps the inline path takes for `conflict` and
    `refines`; `merge` / `duplicate` flip this row to `status=duplicate`
    pointing at the peer (and a `merge` still reinforces the peer). Returns
    the judge's verdict, or `moved` when the row is no longer the write of
    `content_sha256`: the writes that flip it carry that precondition
    (`conflicts.judged_write`), so a revision committed meanwhile is never
    given this verdict."""
    from server.core.ch import get_client, sequential_reads
    from server.memory.app import conflicts as cf

    verdict = str(getattr(result, "verdict", "") or "")
    if verdict == "conflict" and result.peers:
        with sequential_reads():
            landed = await cf.mark_peers_conflicted(
                org=ident.org,
                new_memory_id=memory_id,
                peer_ids=[p.memory_id for p in result.peers],
                expected_sha256=content_sha256,
            )
        if not landed:
            return MOVED
    elif (
        verdict == "refines"
        and result.refined_peers
        and not getattr(ident, "proposed", False)  # a trial fact supersedes nothing
    ):
        # Peers only: `apply_refines` re-checks the row right before it retires any.
        if not await still_current(ident, content_sha256):
            return MOVED
        await cf.apply_refines(
            org=ident.org,
            namespace=ident.namespace,
            new_memory_id=memory_id,
            new_memory_key=ident.memory_key,
            refined_peers=result.refined_peers,
            ch=get_client(),
            relation=result.refine_relation,
            run_id=str(getattr(ident, "run_id", "-") or "-"),
            expected_sha256=content_sha256,
        )
    elif verdict in ("merge", "duplicate"):
        peer_id = str(
            (result.existing_memory_id if verdict == "merge" else result.duplicate_of_id) or ""
        )
        with sequential_reads():
            flipped = await mark_duplicate(
                ident.org, memory_id, peer_id, expected_sha256=content_sha256
            )
        if peer_id and peer_id != memory_id and not flipped:
            return MOVED
        if verdict == "merge":
            await cf.auto_reinforce_peer(ident, peer_id, ch=get_client(), cfg=cfg)
    return verdict


async def mark_duplicate(
    org: str, memory_id: str, peer_id: str, *, expected_sha256: str = ""
) -> int:
    """This row becomes the peer's duplicate: a new RMT version with
    `status=duplicate`, `duplicate_of=<peer>` on loom.memory, mirrored onto
    the embeddings row by the version writer; only the write of
    `expected_sha256` when given (`conflicts.judged_write`)."""
    import server.memory.store as memory_repo
    from server.memory.app.conflicts import judged_write

    if not peer_id or peer_id == memory_id:
        return 0
    skip, version = judged_write(expected_sha256)
    return await memory_repo.write_versions_by_ids(
        org,
        [memory_id],
        lambda row: {"status": "duplicate", "duplicate_of": peer_id} | version(row),
        skip=skip,
        judged=skip is not None,
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
    "MOVED",
    "PENDING",
    "SETTLED",
    "SETTLE_OPERATION",
    "apply",
    "checked_at_for",
    "drain",
    "emit_settle_row",
    "is_pending",
    "mark_duplicate",
    "pending_count",
    "row_pending",
    "schedule_settle",
    "settle",
    "settled_duplicate_of",
    "still_current",
    "stamp_checked",
    "verdict_at",
    "verdict_from_row",
    "wanted",
]
