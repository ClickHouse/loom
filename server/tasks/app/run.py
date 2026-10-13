"""Run ledger handlers (`run.begin`, `run.complete`, `run.get`, `run.list`).

One row per agent execution in `loom.agent_runs` (ReplacingMergeTree on
org/namespace/run_id). begin inserts it active; complete re-inserts the same
row terminal, preserving `started_at` so the pair collapses under FINAL.

Everything else about a run is DERIVED at read time from tables that already
carry `run_id` / `session_id` — loom.memory (what it wrote / observed),
loom.search_trace_log (what it recalled), loom.agent_turn_usage (what it
cost) — so the ledger stays one small row and never disagrees with the
planes it summarises. The statements live in `server.tasks.store.runs`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.ch import get_client, sequential_reads
from server.core.datetimes import naive_utc_now as _now
from server.core.errors import AuthInsufficientError, IdentityIncompleteError, NotFoundError
from server.identity.app.request_scope import request_narrowed, request_scope
from server.identity.domain.read_scope import WHOLE_NAMESPACE, row_in_read_scope
from server.memory.app import run_feedback, run_retention
from server.operations.read_projection import project_rows
from server.tasks.api.models_run import (
    RunBeginInput,
    RunBeginOutput,
    RunCompleteInput,
    RunCompleteOutput,
    RunGetInput,
    RunGetOutput,
    RunGradeInput,
    RunGradeOutput,
    RunGradeProposal,
    RunListInput,
    RunListOutput,
    RunRow,
)
from server.tasks.domain.session_runs import is_session_run_id
from server.tasks.store import runs as runs_store
from server.tasks.store.rows import RunLedgerRow

log = logging.getLogger(__name__)

_METADATA_LIMIT = 8_000


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"
    return str(value)


def _metadata_json(meta: dict[str, Any]) -> str:
    text = json.dumps(meta or {}, default=str, separators=(",", ":"))
    if len(text) > _METADATA_LIMIT:
        return json.dumps({"_truncated": True, "_bytes": len(text)})
    return text


def _parse_metadata(raw: Any) -> dict[str, Any]:
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _grade_columns(existing: RunLedgerRow | None) -> RunLedgerRow:
    """The grade columns of an existing run row, or their defaults."""
    e = existing or {}
    return {
        "grade": str(e.get("grade") or ""),
        "grade_source": str(e.get("grade_source") or ""),
        "grade_reason": str(e.get("grade_reason") or ""),
        "grade_ref": str(e.get("grade_ref") or ""),
        "graded_at": e.get("graded_at"),
    }


def _row_to_model(r: RunLedgerRow) -> RunRow:
    return RunRow(
        run_id=str(r.get("run_id") or ""),
        agent=str(r.get("agent") or ""),
        user_id=str(r.get("user_id") or "-"),
        session_id=str(r.get("session_id") or "-"),
        goal=str(r.get("goal") or ""),
        user_request=str(r.get("user_request") or ""),
        expected_outcome=str(r.get("expected_outcome") or ""),
        environment=str(r.get("environment") or ""),
        model=str(r.get("model") or ""),
        consumer=str(r.get("consumer") or ""),
        status=r.get("status") or "active",
        started_at=_iso(r.get("started_at")),
        completed_at=_iso(r.get("completed_at")),
        duration_ms=int(r.get("duration_ms") or 0),
        final_response=str(r.get("final_response") or ""),
        confidence=float(r.get("confidence") or 0.0),
        grade=str(r.get("grade") or ""),
        grade_source=str(r.get("grade_source") or ""),
        grade_reason=str(r.get("grade_reason") or ""),
        grade_ref=str(r.get("grade_ref") or ""),
        graded_at=_iso(r.get("graded_at")),
        memories_used=[str(k) for k in (r.get("memories_used") or [])],
        memories_written=[str(k) for k in (r.get("memories_written") or [])],
        metadata=_parse_metadata(r.get("metadata")),
    )


def _merge_recalled(existing: Any, incoming: Any, cap: int = 500) -> list[str]:
    """Distinct, order-preserving union of two recalled-key lists."""
    out: list[str] = []
    seen: set[str] = set()
    for src in (existing, incoming):
        for k in src or []:
            key = str(k or "").strip()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
            if len(out) >= cap:
                return out
    return out


async def _recalled_keys(ch: Any, org: str, ns: str, run_id: str) -> list[str] | None:
    """Distinct keys memory.search returned under the run; None when the
    trace log is off (absence of telemetry is not zero recalls)."""
    from server.settings import settings

    if not settings.search_trace_log_enabled:
        return None
    return await runs_store.traced_recall_keys(ch, org, ns, run_id)


def _run_in_scope(run: RunLedgerRow) -> bool:
    """Whether the caller's read scope covers `run` (the whole namespace with no
    auth bound). A scope that does not resolve covers no run."""
    try:
        scope = request_scope()
    except (AuthInsufficientError, IdentityIncompleteError):
        return False
    return row_in_read_scope(scope, run.get("user_id"), run.get("session_id"))


def _owned_by(row: RunLedgerRow, user: str) -> bool:
    """`row` (a run, or the claim on its id) is this begin's user's, inside the
    caller's scope: a begin re-opens only its own user's run, whoever calls."""
    return str(row.get("user_id") or "-") == user and _run_in_scope(row)


async def _target_run(
    ch: Any, input: RunBeginInput, run_id: str, claim: runs_store.RunClaim | runs_store.NoClaim
) -> tuple[str, RunLedgerRow | None]:
    """`run_id` and its row when the caller may (re)open it, else the caller's own id
    for it (`_own_run_id`). `claim` is this begin's claim on `run_id`, made before
    this is called."""
    # Sequential: it must see the synchronous insert of a begin that has
    # already dropped its claim.
    with sequential_reads():
        existing = await runs_store.fetch_run(ch, input.org, input.namespace, run_id)
    if isinstance(claim, runs_store.RunClaim) and not claim.won:
        holder: RunLedgerRow = {"user_id": claim.user_id, "session_id": claim.session_id}
        held_by_caller = not claim.unreadable and _owned_by(holder, _user_of(input))
        if not held_by_caller and not (
            existing is not None and _owned_by(existing, _user_of(input))
        ):
            # Another user's claim, or one we cannot read, and no run of the caller's
            # under this id: a retry of the caller's own run keeps it (that holder
            # will read this row and go to its own id).
            return await _own_run(ch, input, run_id)
    if existing is not None and not _owned_by(existing, _user_of(input)):
        # Another user's run: start the caller's own run instead of taking that one over.
        return await _own_run(ch, input, run_id)
    if existing is None:
        # A begin that went to the caller's own id (no claim then) and lost its answer
        # is re-sent: it re-opens that run, whether or not a claim can be made now.
        # The server cannot tell a first begin from that retry, so every first begin
        # with a supplied id pays this one point read.
        own_id, own_row = await _own_run(ch, input, run_id)
        if own_row is not None:
            return own_id, own_row
        if claim is runs_store.NoClaim.UNAVAILABLE and request_narrowed():
            # Nothing atomic keeps another user's concurrent first begin off this id,
            # so a narrowed caller opens a new id only under its own server-made id.
            return own_id, None
    return run_id, existing


def _own_run_id(input: RunBeginInput, requested: str) -> str:
    """The server-made id a caller gets for `requested` when it cannot have that id:
    derived from (org, namespace, user, session, requested), so a retry of a begin
    whose answer was lost lands on the same run, and no other user or session can."""
    seed = json.dumps(
        [input.org, input.namespace, _user_of(input), input.session_id or "-", requested]
    )
    return "r" + hashlib.sha256(seed.encode()).hexdigest()[:31]


async def _own_run(
    ch: Any, input: RunBeginInput, requested: str
) -> tuple[str, RunLedgerRow | None]:
    """The caller's own id for `requested`, with its row when a begin already opened it."""
    run_id = _own_run_id(input, requested)
    with sequential_reads():
        existing = await runs_store.fetch_run(ch, input.org, input.namespace, run_id)
    if existing is not None and not _owned_by(existing, _user_of(input)):
        # Another caller named this derived id as its own run_id: a fresh id, so this
        # caller's retries lose their idempotency but never reach that run.
        return uuid.uuid4().hex, None
    return run_id, existing


def _user_of(input: RunBeginInput) -> str:
    return input.user_id or "-"


# ------------------------------------------------------------------- handlers


async def begin_session_run(input: RunBeginInput, *, started_at: datetime) -> None:
    """Open, or re-open, an MCP session's implicit run under the id the transport
    made for it. The id binds the token (`session_run_id`) and an explicit
    run.begin cannot name it, so it takes no claim, and the id the session's calls
    carry is the one stored. A new row starts at `started_at`, when the transport
    registered the run; a re-opened one keeps its start, first goal and owner."""
    ch = get_client()
    with sequential_reads():
        existing = await runs_store.fetch_run(ch, input.org, input.namespace, input.run_id)
    if existing is not None:
        input = input.model_copy(
            update={
                "goal": str(existing.get("goal") or input.goal),
                "user_id": str(existing.get("user_id") or input.user_id),
                "session_id": str(existing.get("session_id") or input.session_id),
            }
        )
    await _open_run(ch, input, input.run_id, existing, started_at=started_at)


async def active_session_runs(org: str, run_id: str) -> list[RunLedgerRow]:
    """An MCP session's implicit run, when a web process of the server has begun
    it: oldest first, one per namespace when the subject's tokens default to
    different ones."""
    with sequential_reads():
        return await runs_store.active_runs_by_id(get_client(), org, run_id)


async def close_session_run(org: str, run_id: str, *, reason: str) -> bool:
    """Complete every active row of an implicit session run; False when none is."""
    active = await active_session_runs(org, run_id)
    for row in active:
        await _complete_session_run(row, reason=reason)
    return bool(active)


async def close_idle_session_runs(
    idle_seconds: int, *, page: int = 50, budget_s: float = 120.0, org: str = ""
) -> int:
    """Complete the implicit session runs no call of their token reached within
    the idle window, from the ledger alone, so a run closes whichever web
    process served it and after any restart. Pages of `page` runs until one
    comes back short, closes none, or `budget_s` is spent; returns how many
    closed. `org` keeps a test inside its own rows."""
    deadline = time.monotonic() + budget_s
    closed = 0
    while True:
        stale = await runs_store.idle_session_runs(
            get_client(), idle_seconds=idle_seconds, limit=page, org=org
        )
        closed_now = 0
        for row in stale:
            try:
                await _complete_session_run(row, reason="idle")
                closed_now += 1
            except Exception as e:  # noqa: BLE001 — one run's close must not hold up the rest
                log.warning("session run %s: idle close failed: %s", row["run_id"], e)
        closed += closed_now
        if len(stale) < page or not closed_now or time.monotonic() >= deadline:
            return closed


async def _complete_session_run(row: RunLedgerRow, *, reason: str) -> None:
    await handle_run_complete(
        RunCompleteInput(
            org=str(row["org"]),
            namespace=str(row["namespace"]),
            run_id=str(row["run_id"]),
            status="completed",
            # The transport saw the session end; it cannot say how the work
            # went, and `none` writes no evidence or harm.
            outcome="none",
            # 0: from the run's start, which every web process shares.
            duration_ms=0,
            metadata={"closed_by": f"mcp_{reason}"},
        )
    )


async def handle_run_begin(input: RunBeginInput) -> RunBeginOutput:
    """Open a run. A caller-supplied run_id is claimed in Keeper, then checked
    against its row, then written, then released. A begin that finds the claim
    held by another user goes to a fresh id. A begin that takes the claim after
    the holder released it reads the holder's row, so it goes to a fresh id too:
    that is why the row read comes after the claim. The claim is released only
    once the row is written; a begin that fails first keeps it, so no other
    user's begin can take the id before a row holds it. Without a claim (no
    Keeper, or Keeper failed) a narrowed caller keeps a supplied id only for a
    run of its own that already exists; a new one gets a server-made id."""
    ch = get_client()
    supplied = (input.run_id or "").strip()
    if not supplied:
        # A server-made id cannot collide: nothing to claim or check.
        return await _open_run(ch, input, uuid.uuid4().hex, None)
    if is_session_run_id(supplied):
        # The MCP transport's session runs open only through begin_session_run.
        run_id, existing = await _own_run(ch, input, supplied)
        return await _open_run(ch, input, run_id, existing)
    claim = await runs_store.claim_run(
        ch, input.org, input.namespace, supplied, _user_of(input), input.session_id or "-"
    )
    run_id, existing = await _target_run(ch, input, supplied, claim)
    opened = await _open_run(ch, input, run_id, existing)
    if isinstance(claim, runs_store.RunClaim) and claim.won:
        await runs_store.delete_claim(ch, input.org, input.namespace, supplied)
    return opened


async def _open_run(
    ch: Any,
    input: RunBeginInput,
    run_id: str,
    existing: RunLedgerRow | None,
    *,
    started_at: datetime | None = None,
) -> RunBeginOutput:
    """Write the run's active row and answer with its id. A new row starts at
    `started_at` (default: now)."""
    # The begin row in the ledger carries the run it opened, so begin and complete join
    # on `run_id` (the body names none; the transport's fallback is the header run, empty
    # for a bare client).
    from server.observability.run_context import set_ledger_run

    set_ledger_run(run_id)
    started = started_at or _now()
    if existing is not None and existing.get("started_at"):
        # Same run_id re-sent (hook retry, tracing id reuse): a replace that
        # keeps the original start so duration stays honest.
        started = existing["started_at"]
    row: RunLedgerRow = {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "user_id": input.user_id or "-",
        "session_id": input.session_id or "-",
        "run_id": run_id,
        "goal": input.goal,
        "user_request": input.user_request,
        "expected_outcome": input.expected_outcome,
        "environment": input.environment,
        "model": input.model,
        "consumer": input.consumer,
        "status": "active",
        "started_at": started,
        "completed_at": None,
        "duration_ms": 0,
        "final_response": "",
        "confidence": 0.0,
        "memories_used": [],
        "memories_written": [],
        # A re-sent begin keeps a grade the run already has.
        **_grade_columns(existing),
        "metadata": _metadata_json(
            {**input.metadata, "role": input.role.strip().lower()}
            if input.role.strip()
            else input.metadata
        ),
    }
    await runs_store.insert_runs(ch, [row])
    return RunBeginOutput(
        run_id=run_id,
        status="active",
        started_at=_iso(started),
        next=(
            f"run_id={run_id}. Pass it as run_id on memory.set, memory.ask, "
            "memory.search and observe.record so the run's writes and recalls "
            "join this ledger. When done, call run.complete with the memory_keys "
            "you actually relied on in memories_used."
        ),
    )


@dataclass(frozen=True, slots=True)
class _UsageCredit:
    """What crediting the relied-on memories did: the keys that resolve to a
    live memory (the ledger keeps only those), their rows, and the usage
    events written per kind."""

    used_keys: list[str]
    id_rows: list[dict[str, Any]]
    used: int = 0
    evidence: int = 0
    harm: int = 0


async def _credit_used(
    ch: Any, input: RunCompleteInput, existing: RunLedgerRow, outcome: str
) -> _UsageCredit:
    """`used` for every memory the agent relied on; `evidence` on top when the run
    succeeded, `harm` on top when it failed (a failed run costs what it used, never
    what it merely recalled). All are best-effort usage events (never fail the close)."""
    used_keys = list(dict.fromkeys(k.strip() for k in input.memories_used if k and k.strip()))
    if not used_keys:
        return _UsageCredit(used_keys=[], id_rows=[])
    used_applied = 0
    evidence_applied = 0
    harm_applied = 0
    # Only rows inside the caller's read scope take credit: a token bounded to one
    # user never moves another user's counters. A scope that does not resolve
    # credits nothing (the close itself still lands).
    try:
        scope = request_scope()
    except (AuthInsufficientError, IdentityIncompleteError):
        return _UsageCredit(used_keys=[], id_rows=[])
    id_rows = await runs_store.live_memories_by_key(
        ch, input.org, input.namespace, used_keys, scope=scope
    )
    # The row records what the agent relied on among memories that exist;
    # a key that resolves to nothing is reported (used_applied) but not
    # kept, so the ledger never points at a memory that is not there.
    resolved = {str(r["memory_key"]) for r in id_rows}
    used_keys = [k for k in used_keys if k in resolved]
    seen_ids: set[str] = set()
    for r in id_rows:
        mid = str(r["memory_id"])
        if mid in seen_ids:
            continue
        seen_ids.add(mid)
        try:
            await run_feedback.record_usage(
                memory_id=mid,
                org=input.org,
                namespace=input.namespace,
                event_type="used",
                event_count=int(input.weight),
                agent=str(existing.get("agent") or ""),
                ch=ch,
            )
            used_applied += 1
            if outcome == "positive":
                await run_feedback.record_usage(
                    memory_id=mid,
                    org=input.org,
                    namespace=input.namespace,
                    event_type="evidence",
                    event_count=int(input.weight),
                    agent=str(existing.get("agent") or ""),
                    ch=ch,
                )
                evidence_applied += 1
            elif outcome == "negative":
                await run_feedback.record_usage(
                    memory_id=mid,
                    org=input.org,
                    namespace=input.namespace,
                    event_type="harm",
                    event_count=int(input.weight),
                    agent=str(existing.get("agent") or ""),
                    ch=ch,
                )
                harm_applied += 1
        except Exception as e:  # noqa: BLE001 — per-row best effort
            log.warning("run.complete usage event failed for %s: %s", mid, e)
    return _UsageCredit(
        used_keys=used_keys,
        id_rows=id_rows,
        used=used_applied,
        evidence=evidence_applied,
        harm=harm_applied,
    )


async def handle_run_complete(input: RunCompleteInput) -> RunCompleteOutput:
    ch = get_client()
    # Complete follows begin by a few hundred milliseconds, and the point read may land
    # on a replica that has not seen the begin's part yet (observed: NOT_FOUND 0.2-3.6 s
    # after a begin that existed). Sequential consistency makes the read see the
    # synchronous insert that preceded it.
    with sequential_reads():
        existing = await runs_store.fetch_run(ch, input.org, input.namespace, input.run_id)
    # Another user's run answers like a missing one.
    if existing is None or not _run_in_scope(existing):
        raise NotFoundError(
            f"run {input.run_id!r} not found in {input.org}/{input.namespace}; "
            "call run.begin first",
            run_id=input.run_id,
        )

    now = _now()
    started = existing.get("started_at")
    duration_ms = int(input.duration_ms or 0)
    if duration_ms <= 0 and isinstance(started, datetime):
        duration_ms = max(0, int((now - started.replace(tzinfo=None)).total_seconds() * 1000))

    outcome = (
        input.outcome
        or {
            "completed": "positive",
            "failed": "negative",
            "cancelled": "none",
        }[input.status]
    )

    credit = await _credit_used(ch, input, existing, outcome)
    used_keys = credit.used_keys

    # A procedure the agent relied on is a procedure the agent ran: the same report that
    # credits the row also lands one row in its run ledger, so procedure.evolve (and the
    # scheduled evolve worker) has evidence without a second call the agent never makes.
    # The path is left empty: loom knows the run happened and how it ended, not which
    # steps were visited, and never guesses. `none` outcomes write nothing. Best effort;
    # never fails the close.
    procedure_runs_recorded = 0
    if used_keys and outcome in ("positive", "negative"):
        procedural = [r for r in credit.id_rows if str(r.get("memory_type") or "") == "procedural"]
        procedure_runs_recorded = await _record_procedure_runs(
            input,
            existing,
            procedural,
            ok=(outcome == "positive"),
            duration_ms=duration_ms,
        )
    reflection_key = ""
    if input.reflection is not None and not input.reflection.is_empty():
        reflection_key = await _write_reflection(existing, input)

    # The client said how the run ended: that label belongs to the session too, where the
    # contrast pass pairs on it and where it outranks anything a judge would later read
    # into the summary. Best effort; `none` says nothing and writes nothing.
    session_id = str(existing.get("session_id") or "-")
    if outcome in ("positive", "negative") and session_id != "-":
        await _label_session(ch, existing, input, outcome, session_id)

    scope = request_scope()
    written = await runs_store.written_keys(
        ch, input.org, input.namespace, input.run_id, scope=scope
    )
    observations = await runs_store.observation_count(
        ch, input.org, input.namespace, input.run_id, scope=scope
    )
    recalled = await _recalled_keys(ch, input.org, input.namespace, input.run_id)
    retain_mode, retention = await _retain_run_rows(ch, input, now)

    meta = _parse_metadata(existing.get("metadata"))
    if input.metadata:
        meta.update(input.metadata)
    if input.recalled_keys:
        # What the session's tools served, kept beside the run for run.get's `recalled`;
        # never a usage event.
        meta["recalled_keys"] = _merge_recalled(meta.get("recalled_keys"), input.recalled_keys)
    row = RunLedgerRow(**existing)
    row.update(
        {
            "status": input.status,
            "started_at": started if isinstance(started, datetime) else now,
            "completed_at": now,
            "duration_ms": duration_ms,
            "final_response": input.final_response,
            "confidence": float(input.confidence),
            "memories_used": used_keys,
            "memories_written": written,
            "metadata": _metadata_json(meta),
        }
    )
    await runs_store.insert_runs(ch, [row])
    # run.begin already dropped its claim; this drops one a begin left behind.
    await runs_store.delete_claim(ch, input.org, input.namespace, input.run_id)
    from server.memory.app.recall_credit import note_closer_from_request

    note_closer_from_request(input.org)  # this consumer closes the usage loop
    return RunCompleteOutput(
        run_id=input.run_id,
        status=input.status,
        duration_ms=duration_ms,
        used_applied=credit.used,
        evidence_applied=credit.evidence,
        harm_applied=credit.harm,
        memories_written=written,
        recalled=-1 if recalled is None else len(recalled),
        observations=observations,
        reflection_key=reflection_key,
        procedure_runs_recorded=procedure_runs_recorded,
        retain=retain_mode,
        retained=retention.retained,
        demoted=retention.demoted,
        expiring=retention.expiring,
    )


async def _retain_run_rows(ch: Any, input: RunCompleteInput, now: datetime) -> tuple[str, Any]:
    """Apply the call's `retain` (else the server default) to the run's live rows;
    fail-open."""
    from server.settings import settings

    mode = input.retain or str(settings.memory_run_complete_retain)
    if mode not in run_retention.RETAIN_MODES:
        mode = "keep"
    counts = await run_retention.apply(
        org=input.org,
        namespace=input.namespace,
        run_id=input.run_id,
        mode=mode,
        ch=ch,
        now=now,
        demote_factor=float(settings.memory_expiry_demote_factor),
        demote_floor=float(settings.memory_expiry_demote_floor),
        expire_days=int(settings.memory_run_complete_expire_days),
    )
    return mode, counts


async def _record_procedure_runs(
    input: Any,
    existing: RunLedgerRow,
    rows: list[dict[str, Any]],
    *,
    ok: bool,
    duration_ms: int = 0,
) -> int:
    """One procedure.record_run per procedural memory in memories_used, under
    the procedure row's own identity. Returns how many landed."""
    if not rows:
        return 0
    recorded = 0
    seen: set[str] = set()
    for r in rows:
        key = str(r.get("memory_key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            await run_feedback.record_procedure_run(
                org=input.org,
                namespace=input.namespace,
                agent=str(r.get("agent") or "-"),
                user_id=str(r.get("user_id") or "-"),
                session_id=str(r.get("session_id") or "-"),
                memory_key=key,
                ok=ok,
                note=f"run.complete {input.run_id} ({input.status})"[:512],
                # The run's wall clock is the one cost a run report knows.
                duration_ms=max(0, int(duration_ms or 0)),
            )
            recorded += 1
        except Exception as e:  # noqa: BLE001 — per-row best effort
            log.warning("run.complete procedure run for %s failed: %s", key, e)
    return recorded


async def _label_session(
    ch: Any, existing: RunLedgerRow, input: RunCompleteInput, outcome: str, session_id: str
) -> None:
    from server.memory.app.session_outcomes import record_outcome

    try:
        await record_outcome(
            ch,
            org=input.org,
            namespace=input.namespace,
            agent=str(existing.get("agent") or "-"),
            user_id=str(existing.get("user_id") or "-"),
            session_id=session_id,
            run_id=input.run_id,
            outcome=outcome,
            confidence=float(input.confidence) if input.confidence > 0 else 1.0,
            reason=f"run.complete status={input.status}",
            source="client",
        )
    except Exception as e:  # noqa: BLE001 — the close must not fail on the label
        log.warning("run.complete: session outcome write failed for %s: %s", input.run_id, e)


_REFLECTION_ITEM_LIMIT = 500


async def _write_reflection(existing: RunLedgerRow, input: RunCompleteInput) -> str:
    """One `reflection` memory per run, through the normal memory.set path
    (blacklist, embedding, provenance) under the run's own identity. Excluded
    from unfiltered recall by memory_types.HIDDEN_FROM_UNFILTERED_SEARCH.
    Best-effort: a failed write never fails the close."""
    assert input.reflection is not None
    r = input.reflection

    def _clean(items: list[str]) -> list[str]:
        return [str(x).strip()[:_REFLECTION_ITEM_LIMIT] for x in items if str(x).strip()]

    lists = {
        "what_worked": _clean(r.what_worked),
        "what_failed": _clean(r.what_failed),
        "assumptions": _clean(r.assumptions),
        "lessons": _clean(r.lessons),
        "improvements": _clean(r.improvements),
    }
    if not any(lists.values()):
        return ""  # whitespace-only lists: nothing was learned
    evidence_keys = list(dict.fromkeys(str(k).strip() for k in r.evidence if str(k).strip()))
    value: dict[str, Any] = {
        "run_id": input.run_id,
        "goal": str(existing.get("goal") or ""),
        "status": input.status,
        **lists,
        # A lesson names what it rests on.
        "evidence": evidence_keys,
    }
    # The role the run declared at begin: the reflection is tagged with it so
    # memory.search(role=...) prefers same-role lessons.
    role = str(_parse_metadata(existing.get("metadata")).get("role") or "").strip().lower()
    tags = ["reflection", f"run:{input.run_id}", f"status:{input.status}"]
    if role:
        value["role"] = role
        tags.append(f"role:{role}")
    key = f"reflection:{input.run_id}"
    try:
        await run_feedback.write_reflection(
            org=input.org,
            namespace=input.namespace,
            agent=str(existing.get("agent") or "-"),
            user_id=str(existing.get("user_id") or "-"),
            session_id=str(existing.get("session_id") or "-"),
            run_id=input.run_id,
            memory_key=key,
            value=value,
            tags=tags,
            confidence=float(r.confidence),
            # Unvalidated lessons fade: memory.verify or a positive outcome renews; 0 means
            # no expiry.
            ttl_seconds=int(r.expires_in_days) * 86400 if r.expires_in_days > 0 else None,
        )
    except Exception as e:  # noqa: BLE001 — the close must not fail on a lesson
        log.warning("run.complete: reflection write failed for %s: %s", input.run_id, e)
        return ""
    # One `derived_from` edge per evidence key, so the grounding grade and the
    # graph leg see what the lesson rests on. Best-effort per edge.
    if evidence_keys:
        for ek in evidence_keys[:50]:
            try:
                await run_feedback.link_derived_from(
                    org=input.org,
                    namespace=input.namespace,
                    from_key=key,
                    to_key=ek,
                    explanation=f"reflection of run {input.run_id}",
                    confidence=float(r.confidence),
                    run_id=input.run_id,
                )
            except Exception as e:  # noqa: BLE001 — the reflection stands
                log.warning("run.complete: reflection edge %s -> %s failed: %s", key, ek, e)
    return key


# A session-addressed grade covers at most this many runs (a PR is reviewed a handful of
# times; the cap only stops a runaway session).
_GRADE_SESSION_LIMIT = 200


async def _runs_to_grade(ch: Any, input: RunGradeInput) -> list[RunLedgerRow]:
    """The run addressed by `run_id`, or the session's runs, newest first."""
    run_id = input.run_id.strip()
    return await runs_store.runs_to_grade(
        ch,
        input.org,
        input.namespace,
        run_id=run_id,
        session_id=input.session_id.strip(),
        limit=1 if run_id else _GRADE_SESSION_LIMIT,
    )


async def handle_run_grade(input: RunGradeInput) -> RunGradeOutput:
    """Write a verdict onto closed runs, then let the namespace owner's proposal policy
    (auto mode only) act on the proposals those runs used.

    The grade is a new version of each run row carrying every column, so a
    regrade replaces the verdict and the per-memory counts (derived from the
    rows, `proposals.graded_counts`) follow it. Unlike run.complete's outcome
    it writes no usage events: a self-reported `completed` and a graded pass
    are different signals, and folding both into `evidence` would count a run
    twice."""
    from server.memory.app import proposals as _proposals

    ch = get_client()
    rows = await _runs_to_grade(ch, input)
    if not rows:
        what = f"run {input.run_id!r}" if input.run_id.strip() else f"session {input.session_id!r}"
        raise NotFoundError(f"{what} has no runs in {input.org}/{input.namespace}")
    source, reason, ref = input.source.strip(), input.reason.strip(), input.ref.strip()
    now = _now()
    to_write: list[RunLedgerRow] = []
    counted: set[str] = set()
    skipped_active = unchanged = 0
    for r in rows:
        if str(r.get("status") or "") == "active":
            skipped_active += 1  # a run is graded after it closed
            continue
        counted.update(str(k) for k in (r.get("memories_used") or []) if k)
        if (r.get("grade"), r.get("grade_source"), r.get("grade_reason"), r.get("grade_ref")) == (
            input.grade,
            source,
            reason,
            ref,
        ):
            unchanged += 1
            continue
        row = RunLedgerRow(**r)
        row.update(
            {
                "grade": input.grade,
                "grade_source": source,
                "grade_reason": reason,
                "grade_ref": ref,
                "graded_at": now,
            }
        )
        to_write.append(row)
    if to_write:
        await runs_store.insert_runs(ch, to_write)
    decided: list[Any] = []
    # Every grade call re-applies the policy: an owner who turned `auto` on
    # after the runs were graded gets the decision on the next grade.
    if counted:
        try:
            decided = await _proposals.apply_policy(
                input.org, input.namespace, sorted(counted), ch=ch
            )
        except Exception as e:  # noqa: BLE001 — the grade landed; the policy retries on the next
            log.warning("run.grade: proposal policy failed for %s: %s", input.namespace, e)
    return RunGradeOutput(
        graded=len(to_write),
        run_ids=[str(r["run_id"]) for r in to_write],
        skipped_active=skipped_active,
        unchanged=unchanged,
        memories_counted=len(counted),
        proposals_decided=[
            RunGradeProposal(memory_key=k, decision=d, passes=c.passes, fails=c.fails)
            for k, d, c in decided
        ],
    )


async def handle_run_get(input: RunGetInput) -> RunGetOutput:
    ch = get_client()
    existing = await runs_store.fetch_run(ch, input.org, input.namespace, input.run_id)
    if existing is None:
        return RunGetOutput(found=False)
    run = _row_to_model(existing)
    # run.get is refused to narrowed tokens (in neither SCOPED_OPS nor NARROW_SAFE_OPS),
    # so it always reads the whole namespace.
    written = await runs_store.written_keys(
        ch, input.org, input.namespace, input.run_id, scope=WHOLE_NAMESPACE
    )
    observations = await runs_store.observation_count(
        ch, input.org, input.namespace, input.run_id, scope=WHOLE_NAMESPACE
    )
    recalled = await _recalled_keys(ch, input.org, input.namespace, input.run_id)
    # The keys the plugin's hooks saw served under this session join the trace-derived
    # set (MCP reads are ledgered under the connection's own run and never reach the
    # trace query above).
    stashed = run.metadata.get("recalled_keys") if isinstance(run.metadata, dict) else None
    if stashed:
        recalled = _merge_recalled(recalled, stashed)
    turns, cost = (
        await runs_store.session_cost(ch, input.org, input.namespace, run.session_id)
        if run.session_id != "-"
        else (0, 0.0)
    )
    return RunGetOutput(
        found=True,
        run=run,
        recalled=-1 if recalled is None else len(recalled),
        recalled_keys=recalled or [],
        memories_written=written,
        observations=observations,
        turns=turns,
        cost_usd=cost,
    )


async def handle_run_list(input: RunListInput) -> RunListOutput:
    """List a namespace's agent runs, newest first, filtered by agent, session, status or grade."""

    where: list[str] = []
    params: dict[str, Any] = {}
    if input.agent:
        where.append("agent = {agent:String}")
        params["agent"] = input.agent
    if input.session_id:
        where.append("session_id = {sid:String}")
        params["sid"] = input.session_id
    if input.status:
        where.append("status = {status:String}")
        params["status"] = input.status
    # The memory-activity fold is a filter, not a client-side slice, so `limit` counts
    # visible runs.
    if input.memory_activity == "with":
        where.append("(notEmpty(memories_used) OR notEmpty(memories_written))")
    elif input.memory_activity == "without":
        where.append("(empty(memories_used) AND empty(memories_written))")
    # A grader pages the runs it has not graded yet.
    if input.grade == "ungraded":
        where.append("grade = ''")
    elif input.grade == "graded":
        where.append("grade != ''")
    elif input.grade:
        where.append("grade = {grade:String}")
        params["grade"] = input.grade
    rows = await runs_store.list_runs(
        get_client(),
        input.org,
        input.namespace,
        where=where,
        params=params,
        limit=int(input.limit),
    )

    runs = [_row_to_model(r) for r in rows]
    project_rows(runs, RunRow, input.fields, always=frozenset({"run_id"}))
    return RunListOutput(runs=runs)


async def recent_run_heads(
    ch: Any, *, org: str, namespace: str, agent: str, user_id: str, limit: int
) -> list[dict[str, Any]]:
    """The identity's newest runs (id, session, goal, status, start, duration) for a
    reader outside the run ledger. Agent `*`/`-` drops the agent predicate; user_id
    `-`/empty drops the user predicate."""
    where: list[str] = []
    params: dict[str, Any] = {}
    if agent and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id and user_id != "-":
        where.append("user_id = {uid:String}")
        params["uid"] = user_id
    return await runs_store.recent_run_heads(
        ch, org, namespace, where=where, params=params, limit=int(limit)
    )
