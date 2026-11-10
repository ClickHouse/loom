"""Erasure saga engine behind `erasure.request` (the op handlers live in
`server/erasure/app/handlers.py`).

Each step is idempotent: re-running an erasure produces the same end state.
After every step we checkpoint progress into `erasure_requests` (a new
ReplacingMergeTree row with an updated `steps_completed` array). The recovery
worker (`server/erasure/workers/recovery.py`) replays sagas stuck in
`in_progress`, skipping steps already in `steps_completed` — so a Control
Plane restart mid-Saga doesn't leave a half-erased user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from server.core import pg
from server.core.background import register_deferred_task
from server.core.error_summary import error_summary
from server.core.errors import BackendUnavailableError, caller_text
from server.core.pg import PostgresUnavailableError
from server.erasure.api.erasure import (
    ErasureRequestInput,
    ErasureRequestOutput,
    _ErasureStepResult,
)
from server.erasure.app import backends as erasure_backends
from server.erasure.app.backends import ErasureBackendError, ExternalEraseBackend, StubBackend
from server.erasure.app.saga_steps import (
    NAMESPACE_STEPS,
    USER_STEPS,
    ErasureStepError,
    _step_verify_residue,
    authority_steps,
)
from server.erasure.domain.scopes import (
    applied_purge,
    parse_external_flag,
    parse_namespace_list,
    parse_purge_flag,
)
from server.erasure.store import census as erasure_census
from server.erasure.store import ledger as erasure_ledger
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store.provider_copies import fold_census
from server.erasure.store.rows import CheckpointRow, RequestRow, VerifyColumns, census_kept
from server.memory.app import webhooks
from server.memory.app.provenance import writer_actor
from server.observability import audit

log = logging.getLogger(__name__)

# A store that refused or timed out: the recovery worker retries the saga.
_BACKEND_FAILURES = (ErasureBackendError, BackendUnavailableError, PostgresUnavailableError)


def audit_namespace(scope_type: str, scope_value: str) -> str:
    """The namespace an erasure's audit rows carry: the scope's own for a
    single-namespace scope, `-` for one that spans namespaces."""
    return scope_value if scope_type == "namespace" else "-"


# ============================================================
# erasure.request
# ============================================================


def input_from_ledger_row(row: Mapping[str, Any]) -> ErasureRequestInput:
    """The request a saga's ledger row records, for a resume. `external` is
    not a ledger column: a `namespace_list` scope_value carries it (and
    `purge` too), and the saga reads it back from there."""
    return ErasureRequestInput(
        org=row["org"],
        scope_type=row["scope_type"],
        scope_value=row["scope_value"],
        requester=row["requester"],
        urgency=row["urgency"] or "normal",
        purge=bool(row.get("purge")),
    )


# The actor the recovery worker writes as: a process, no principal.
RECOVERY_WORKER_ACTOR = "erasure-recovery-worker"


def audit_actor(row: Mapping[str, Any]) -> str:
    """The actor of a saga's failed and completed rows: the ledger row's, else (a
    row an older build wrote) the resuming process."""
    return row["actor"] or writer_actor(RECOVERY_WORKER_ACTOR)


def request_row_template(
    *,
    erasure_id: str,
    input: ErasureRequestInput,
    actor: str,
    requested_at: datetime,
    attempts: int,
) -> RequestRow:
    """Common columns for every erasure_requests row we write. `actor` is the
    request's audit actor (`writer_actor` at request time, '' on a row an older
    build wrote); `requester` is what the body declares."""
    return {
        "id": erasure_id,
        "org": input.org,
        "scope_type": input.scope_type,
        "scope_value": input.scope_value,
        "requester": input.requester,
        "actor": actor,
        "urgency": input.urgency,
        # The purge the saga applies: a single namespace's never purges, whatever was asked.
        "purge": applied_purge(input.scope_type, input.scope_value, bool(input.purge)),
        "attempts": attempts,
        "requested_at": requested_at,
    }


async def checkpoint(
    *,
    row: RequestRow,
    status: str,
    steps_completed: list[str],
    current_step: int,
    failure_reason: str = "",
    completed_at: datetime | None = None,
    extra: VerifyColumns | None = None,
) -> None:
    """Write a state-transition row. Used after every Saga step and at terminal states.
    `extra` carries the verification columns on terminal rows."""
    ck: CheckpointRow = {
        **row,
        **(extra or VerifyColumns()),
        "status": status,
        "current_step": current_step,
        "failure_reason": failure_reason[:512],
        "completed_at": completed_at,
        "steps_completed": steps_completed,
        "last_checkpoint_at": datetime.now(UTC).replace(tzinfo=None),
        "version": time.time_ns(),
    }
    await erasure_ledger.insert_request_checkpoint(ck)


async def begin_erasure(
    input: ErasureRequestInput,
) -> tuple[ErasureRequestInput, RequestRow, str, datetime]:
    """Register a saga: normalise a `namespace_list` scope, write the
    `in_progress` checkpoint and the `erasure.started` audit row. Returns what
    `run_saga` needs. Authorization is the caller's — `erasure.request`
    checks admin on the target first; `code.repo.remove` has
    already passed the transport's ADMIN gate on the namespace it retires."""
    # A CLI process configured for another deployment's Postgres is refused here,
    # before the ledger row a recovery worker would later run.
    await pg.verify_deployment()
    erasure_id = str(uuid.uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    if input.scope_type == "namespace_list":
        # A `purge` already inside the scope_value object (the CLI writes it
        # there) wins over the field default; otherwise a namespaces-gc run
        # would tombstone the rows it means to delete. `external` travels the
        # same way.
        _purge = parse_purge_flag(input.scope_value, default=bool(input.purge))
        _external = parse_external_flag(input.scope_value, default=bool(input.external))
        input = input.model_copy(
            update={
                "purge": _purge,
                "external": _external,
                "scope_value": json.dumps(
                    {
                        "namespaces": parse_namespace_list(input.scope_value),
                        "purge": _purge,
                        "external": _external,
                    }
                ),
            }
        )
    row = request_row_template(
        erasure_id=erasure_id,
        input=input,
        # Never '': that marks a row an older build wrote, which a resume names
        # after the resuming process.
        actor=writer_actor(input.requester) or "-",
        requested_at=now,
        attempts=1,
    )

    await checkpoint(
        row=row,
        status="in_progress",
        steps_completed=[],
        current_step=0,
    )

    await audit.emit(
        event_type="erasure.started",
        org=input.org,
        namespace=audit_namespace(input.scope_type, input.scope_value),
        actor=row["actor"],
        subject={
            "erasure_id": erasure_id,
            "scope_type": input.scope_type,
            "scope_value": input.scope_value,
            "requester": input.requester,
        },
        request_id=erasure_id,
    )
    return input, row, erasure_id, now


# Detached sagas stay referenced until they finish — an asyncio task nothing holds can
# be collected mid-flight.
_DETACHED: set[asyncio.Task[Any]] = set()


def _detached_done(task: asyncio.Task[Any]) -> None:
    _DETACHED.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("erasure: detached saga ended with %s: %s", type(exc).__name__, exc)


async def run_erasure_detached(input: ErasureRequestInput) -> str:
    """Register the saga and run it in the background; the
    caller gets the erasure id at once. Every step checkpoints, so a process
    that dies mid-saga leaves an `in_progress` row the erasure_recovery worker
    resumes; `erasure.status` follows it. Used by `code.repo.remove`, whose
    caller is a maintainer clicking a button, not an operator waiting on a
    mutation."""
    input, row, erasure_id, now = await begin_erasure(input)
    task = asyncio.create_task(
        run_saga(
            input=input,
            row=row,
            erasure_id=erasure_id,
            requested_at=now,
            previously_completed=set(),
        ),
        name=f"erasure-{erasure_id[:8]}",
    )
    _DETACHED.add(task)
    register_deferred_task(task)
    task.add_done_callback(_detached_done)
    return erasure_id


async def run_saga(
    *,
    input: ErasureRequestInput,
    row: RequestRow,
    erasure_id: str,
    requested_at: datetime,
    previously_completed: set[str],
    residue: Mapping[str, Any] | str | None = None,
) -> ErasureRequestOutput:
    """Execute (or resume) the Saga. Recovery worker calls this too.

    `residue` is the last census over this saga's scope (the ledger's `residue` column,
    or a fresh count): the steps that own its tables are re-run even if
    `previously_completed` names them. A checkpoint is a claim that a step ran; the
    census is evidence of what it left. Without this, a purge saga whose tombstone steps
    were checkpointed before it switched to purging would fail `verify.residue` on every
    resume.
    """
    await pg.verify_deployment()  # a resumed saga too: refused before any step writes
    steps: list[_ErasureStepResult] = []
    # The census always runs again: a run that stopped between it and the terminal row
    # left no census on the ledger, so a resume that skipped it would complete without one.
    completed = set(previously_completed) - {"verify.residue"}
    # What the census tail counts if a step fails before `verify.residue`:
    # the saga's scope, and whether it purges.
    census_scope_type = "user_id" if input.scope_type == "user_id" else "namespace"
    reopened = erasure_census.steps_to_reopen(residue, census_scope_type) & completed
    if reopened:
        log.warning(
            "erasure %s: re-opening %s — the last census found rows in tables these "
            "checkpointed steps own",
            erasure_id,
            sorted(reopened),
        )
        completed -= reopened
    # Filled by the verify step; read back for the terminal row.
    verify: dict[str, Any] = {}
    census_scope_value: str | Sequence[str] = input.scope_value
    census_purge = applied_purge(input.scope_type, input.scope_value, bool(input.purge))
    # The saga id names every mutation this run submits,
    # so a resumed step finds its own pending mutation instead of adding one.
    with erasure_mutations.saga_scope(erasure_id):
        try:
            if input.scope_type == "user_id":
                # A user's erasure purges when asked (a GDPR deletion): the rows
                # leave the parts, not just the readable set.
                with erasure_mutations.purge_scope(census_purge):
                    steps = await _saga_user_id(
                        input.org,
                        input.scope_value,
                        completed=completed,
                        row=row,
                        verify=verify,
                    )
            elif input.scope_type == "namespace":
                steps = await _saga_namespace(
                    input.org,
                    input.scope_value,
                    completed=completed,
                    row=row,
                    verify=verify,
                )
            elif input.scope_type == "namespace_list":
                # One saga over a list of namespaces — every step
                # runs its predicate as `namespace IN (...)`, one mutation per
                # table; `purge` (kept in the ledger's scope_value so a resumed
                # saga honours it) turns the tombstone steps into deletes.
                names = parse_namespace_list(input.scope_value)
                census_scope_value = names
                with erasure_mutations.purge_scope(census_purge):
                    steps = await _saga_namespace(
                        input.org,
                        names,
                        completed=completed,
                        row=row,
                        verify=verify,
                        external=parse_external_flag(
                            input.scope_value, default=bool(input.external)
                        ),
                    )
            else:  # pragma: no cover — Pydantic enforces ScopeType
                raise ValueError(f"unsupported scope_type: {input.scope_type}")
        except Exception as e:
            # A backend that refused, timed out or dropped its connection is the
            # fail-closed path working: the recovery worker retries it, so log one line;
            # a traceback is for the unexpected case.
            cause = getattr(e, "cause", None)
            if isinstance(e, ErasureStepError) and (
                isinstance(cause, _BACKEND_FAILURES) or (cause is not None and pg.is_outage(cause))
            ):
                log.warning(
                    "erasure %s failed at step %s (backend, will retry): %s: %s",
                    erasure_id,
                    e.step,
                    type(cause).__name__,
                    cause,
                )
            else:
                log.exception("erasure %s failed mid-saga", erasure_id)
            # What the requester reads: the step and `caller_text` of the cause; a
            # failure outside a step (a checkpoint's insert) is `caller_text(e)` alone.
            # The text is in the log.
            reason = str(e) if isinstance(e, ErasureStepError) else caller_text(e)
            # The census tail: the census runs whether or not the steps did, so a
            # step that fails ahead of `verify.residue` does not leave readable
            # residue under `residue_count = 0`. The tail fills the holder and
            # never replaces the step's error: the reason stays the step's, the
            # residue is evidence beside it.
            await _census_tail(
                input.org, census_scope_type, census_scope_value, verify, purge=census_purge
            )
            census = _census(verify)
            completed_at = datetime.now(UTC).replace(tzinfo=None)
            await checkpoint(
                row=row,
                status="failed",
                steps_completed=sorted(completed),
                current_step=len(completed),
                failure_reason=reason,
                completed_at=completed_at,
                extra=_verify_columns(verify, census),
            )
            await audit.emit(
                event_type="erasure.failed",
                org=input.org,
                namespace=audit_namespace(input.scope_type, input.scope_value),
                actor=audit_actor(row),
                subject={
                    "erasure_id": erasure_id,
                    "requester": input.requester,
                    "error": reason[:512],
                    "steps_completed": sorted(completed),
                },
                request_id=erasure_id,
            )
            return ErasureRequestOutput(
                erasure_id=erasure_id,
                status="failed",
                steps=steps,
                requested_at=requested_at,
                completed_at=completed_at,
                residue_count=int(verify.get("count") or 0),
                residue={t: n for t, n in (verify.get("per_table") or {}).items() if n},
                verified_at=verify.get("verified_at"),
                census=census,
                purge=census_purge,
                failure_reason=reason[:512],
                steps_completed=sorted(completed),
            )
    # After the census and before the checkpoint: a crash in between reopens the saga,
    # which announces again under the same event id. The event names the erasure only,
    # never the user or a key.
    await webhooks.announce_erasure(
        input.org, _erased_namespaces(input.scope_type, census_scope_value), erasure_id
    )
    completed_at = datetime.now(UTC).replace(tzinfo=None)
    census = _census(verify)
    await checkpoint(
        row=row,
        status="completed",
        steps_completed=sorted(completed),
        current_step=99,
        completed_at=completed_at,
        extra=_verify_columns(verify, census),
    )

    await audit.emit(
        event_type="erasure.completed",
        org=input.org,
        namespace=audit_namespace(input.scope_type, input.scope_value),
        actor=audit_actor(row),
        subject={
            "erasure_id": erasure_id,
            "scope_type": input.scope_type,
            "scope_value": input.scope_value,
            "requester": input.requester,
            "steps": [s.model_dump() for s in steps],
            # Counts only: a copy of the receipt that outlives the ledger row.
            "census": census,
        },
        request_id=erasure_id,
    )

    return ErasureRequestOutput(
        erasure_id=erasure_id,
        status="completed",
        steps=steps,
        requested_at=requested_at,
        completed_at=completed_at,
        residue_count=int(verify.get("count") or 0),
        residue={},
        verified_at=verify.get("verified_at"),
        census=census,
        purge=census_purge,
        steps_completed=sorted(completed),
    )


async def _census_tail(
    org: str,
    scope_type: str,
    scope_value: str | Sequence[str],
    verify: dict[str, Any],
    *,
    purge: bool,
) -> None:
    """Run the residue census after a step failure, filling `verify` as the step does
    (`verified_at` only when every store was counted), never raising: residue here is
    evidence on the failed row, not a second failure. A no-op once the verify step ran."""
    if verify:
        return
    try:
        with erasure_mutations.purge_scope(purge):
            per_table, unread = await erasure_census.evidence_census(org, scope_type, scope_value)
    except Exception as e:  # noqa: BLE001 — the step's error is the reason; this is a footnote
        log.warning("erasure: census tail failed after a step failure: %s", error_summary(e))
        return
    for store, error in unread.items():
        log.warning("erasure: census tail could not read %s: %s", store, error_summary(error))
    verify["per_table"] = per_table
    verify["count"] = sum(per_table.values())
    if not unread:
        verify["verified_at"] = datetime.now(UTC).replace(tzinfo=None)


def verify_columns_from_output(out: ErasureRequestOutput | None) -> VerifyColumns:
    """The verify columns of a saga's returned output, for a checkpoint written
    by someone other than the saga (the recovery worker's terminal row): a census that
    found residue is kept even when it could not count every store (`census_kept`)."""
    if out is None or not census_kept(out.verified_at, out.residue_count):
        return {}
    return {
        "residue_count": out.residue_count,
        "verified_at": out.verified_at,
        "residue": json.dumps(out.residue, sort_keys=True),
        "census": _census_column(out.census),
    }


def _verify_columns(verify: dict[str, Any], census: dict[str, int] | None) -> VerifyColumns:
    """erasure_requests columns from the verify holder; empty when no census ran (the
    census tail raised), or when it found nothing and left a store uncounted
    (`census_kept`)."""
    if not census_kept(verify.get("verified_at"), verify.get("count")):
        return {}
    return {
        "residue_count": int(verify.get("count") or 0),
        "verified_at": verify.get("verified_at"),
        "residue": json.dumps(
            {t: n for t, n in (verify.get("per_table") or {}).items() if n}, sort_keys=True
        ),
        "census": _census_column(census),
    }


def _erased_namespaces(scope_type: str, scope_value: str | Sequence[str]) -> list[str] | None:
    """The namespaces an erasure webhook names; None for a user's erasure (every one)."""
    if scope_type == "user_id":
        return None
    return [scope_value] if isinstance(scope_value, str) else list(scope_value)


def _census_column(census: dict[str, int] | None) -> str:
    """The ledger's `census` text: '' for no receipt."""
    return json.dumps(census, sort_keys=True) if census is not None else ""


def _census(verify: dict[str, Any]) -> dict[str, int] | None:
    """Every count of a census that counted every store, the rebuild copies folded
    into one key; None when a store went uncounted (`verified_at` unset)."""
    if verify.get("verified_at") is None:
        return None
    return fold_census({str(t): int(n) for t, n in (verify.get("per_table") or {}).items()})


async def _do_step(
    *,
    step_name: str,
    step_fn: Callable[[], Awaitable[int]],
    completed: set[str],
    row: RequestRow,
    stub: bool = False,
) -> _ErasureStepResult:
    """Run one Saga step unless it's already in `completed`; checkpoint after.

    `step_fn` returns rows_affected; `stub` marks an unconfigured backend's
    step, whose 0 is noted as such.
    """

    if step_name in completed:
        return _ErasureStepResult(
            step=step_name,
            rows_affected=0,
            notes="skipped (resumed from checkpoint)",
        )
    # On exception, propagate so the saga wrapper records `failed` with the green steps
    # preserved — recovery resumes past them without re-doing them. Named after the
    # step: the reason on the ledger says which step stopped the saga, whatever the
    # cause's own message.
    try:
        with erasure_mutations.step_scope(step_name):
            n = await step_fn()
    except Exception as e:
        raise ErasureStepError(step_name, e) from e
    notes = ""
    if stub and n == 0:
        notes = "stub: not configured"
    result = _ErasureStepResult(step=step_name, rows_affected=n, notes=notes)
    completed.add(step_name)
    await checkpoint(
        row=row,
        status="in_progress",
        steps_completed=sorted(completed),
        current_step=len(completed),
    )
    return result


async def _saga_user_id(
    org: str,
    user_id: str,
    *,
    completed: set[str],
    row: RequestRow,
    verify: dict[str, Any] | None = None,
) -> list[_ErasureStepResult]:
    verify = verify if verify is not None else {}
    results: list[_ErasureStepResult] = []

    async def _step(name: str, fn: Callable[..., Awaitable[int]], *args: Any, **kw: Any) -> None:
        results.append(
            await _do_step(
                step_name=name, step_fn=lambda: fn(*args), completed=completed, row=row, **kw
            )
        )

    # A user erasure mutates rows in every namespace of the org; the write
    # marks' views see inserts only. Mark the org as moved first so no
    # precedent answer is served past this point without a fresh retrieval.
    # Best-effort: not a saga step, never blocks erasure.
    from server.core import namespace_write_marks as _marks

    await _marks.bump(org, _marks.ORG_WIDE, "erasure")

    for name, fn in authority_steps(USER_STEPS):
        await _step(name, fn, org, user_id)

    # External backends fail closed: they raise ErasureBackendError on a
    # transient failure and return 0 only for "nothing to erase". `_do_step`
    # lets the exception propagate, so the saga is recorded `failed` with the
    # failed step left out of `completed` for an idempotent retry.
    for backend in erasure_backends.all_backends():
        await _step(
            f"{backend.name}.delete_user",
            backend.delete_user_id,
            org,
            user_id,
            stub=isinstance(backend, StubBackend),
        )
    # Verified forgetting: the census is the last step and the
    # only one that can fail the saga on its own evidence.
    await _step("verify.residue", _step_verify_residue, org, "user_id", user_id, verify)
    return results


async def _saga_namespace(
    org: str,
    namespace: str | Sequence[str],
    *,
    completed: set[str],
    row: RequestRow,
    verify: dict[str, Any] | None = None,
    external: bool = True,
) -> list[_ErasureStepResult]:
    verify = verify if verify is not None else {}
    results: list[_ErasureStepResult] = []

    async def _step(name: str, fn: Callable[..., Awaitable[int]], *args: Any, **kw: Any) -> None:
        results.append(
            await _do_step(
                step_name=name, step_fn=lambda: fn(*args), completed=completed, row=row, **kw
            )
        )

    for name, fn in authority_steps(NAMESPACE_STEPS):
        await _step(name, fn, org, namespace)

    # External backends fail closed — see _saga_user_id. A namespace_list erasure may
    # run loom-only (`external=false`): the backends erase per namespace — Langfuse
    # looks up traces by tag, ClickStack counts then mutates each OTel table — and a
    # 200-namespace GC of throwaway bench namespaces is not what they are for.
    if not external:
        log.info(
            "erasure: external backends skipped for a loom-only namespace_list saga "
            "(%d namespaces)",
            len(erasure_mutations.namespaces_of(namespace)),
        )
    for backend in erasure_backends.all_backends() if external else []:

        async def _delete_each(b: ExternalEraseBackend = backend) -> int:
            return await erasure_backends.delete_namespaces(
                b, org, erasure_mutations.namespaces_of(namespace)
            )

        await _step(
            f"{backend.name}.delete_namespace",
            _delete_each,
            stub=isinstance(backend, StubBackend),
        )
    # Verified forgetting: the census is the last step and the
    # only one that can fail the saga on its own evidence.
    await _step("verify.residue", _step_verify_residue, org, "namespace", namespace, verify)
    return results
