"""Erasures after a restore (`loom reconcile`).

A restore loses what was written after its point, and with it the effect of the
erasures made after it in the restored store. Each store records them for the other:
Postgres in `loom.erasure_ledger`, ClickHouse in `loom.erasure_requests`.

- An erasure the other store records as completed after the point runs again (`replay`)
  on the restored store, with no census: it would count the other store, which holds
  the user's later writes. A step the restored copy shows ran before the point (the
  saga was still running then) is not run again, for the same reason: the restored
  checkpoint's `steps_completed`, or the `pg_steps` the Postgres ledger row records in
  each Postgres step's own transaction. After a ClickHouse restore the steps are every
  one but the Postgres authority's own (they hold, and running them again would delete
  what the user wrote after the erasure); its ClickHouse row reads `failed` with
  REPLAY_PENDING_REASON while they run, a row the recovery worker never resumes, and
  the saga's `completed` row and `erasure.completed` audit row (actor REPLAY_ACTOR)
  once all ran. After a Postgres restore only the Postgres steps run, on the restored
  copy, which holds nothing written after the point, and the `loom.erasure_ledger` row
  is completed once they ran; ClickHouse's row and audit trail are as they were. Either
  way the replay answers' step runs too: it drops the org's whole cache, in both
  stores, as in any erasure. A step that fails is reported, and the reconcile is run
  again; the next run selects the erasure again from the other store.
- An erasure with no terminal state is left to the recovery worker (`requeue`): after a
  ClickHouse restore its checkpoint is written again with every step to run but the
  Postgres ones the ledger records as committed (when the restored row shows it ended,
  completed or abandoned, the ledger row is marked so instead); after a Postgres
  restore its ledger row, and its checkpoint with the Postgres steps the restore undid
  reopened.
- An abandoned erasure (out of attempts) is not run: the operator's `loom
  erasure-re-verify` owns it.

External backends are not restored, so their steps are never run again. Every step is
idempotent, so a reconcile cut off is run again whole.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from server.core.datetimes import to_naive_utc
from server.erasure.api.erasure import ErasureRequestInput
from server.erasure.app.saga import (
    RECOVERY_WORKER_ACTOR,
    audit_actor,
    audit_namespace,
    checkpoint,
    request_row_template,
)
from server.erasure.app.saga_steps import (
    NAMESPACE_STEPS,
    PG_ONLY_STEPS,
    USER_STEPS,
    ErasureStepError,
)
from server.erasure.domain.failure_reason import REPLAY_PENDING_REASON
from server.erasure.store import ledger as erasure_ledger
from server.erasure.store import pg_ledger
from server.erasure.store.rows import RequestRow
from server.observability import audit

# The actor a replay's ledger rows and audit row name: a command, no principal.
REPLAY_ACTOR = "loom-reconcile"

Restored = Literal["clickhouse", "postgres"]


async def completed_since(restored: Restored, point: datetime) -> list[dict[str, Any]]:
    """The erasures the other store records as completed after `point`, oldest first."""
    if restored == "clickhouse":
        return await pg_ledger.completed_since(point)
    return await erasure_ledger.completed_since(point)


async def still_open(restored: Restored) -> list[dict[str, Any]]:
    """The erasures the other store records with no terminal state."""
    if restored == "clickhouse":
        return await pg_ledger.still_open()
    return await erasure_ledger.still_open()


# The steps with a Postgres side: the authority's own, and the replay answers' (a cache
# with a Postgres copy), which a Postgres restore undoes.
PG_STEPS = PG_ONLY_STEPS | {"idempotency.delete"}


def _replayed(scope_type: str, restored: Restored) -> list[tuple[str, Any]]:
    """The Loom steps a replay runs on the restored store, in saga order (the external
    backends and the census are never among them)."""
    steps = USER_STEPS if scope_type == "user_id" else NAMESPACE_STEPS
    if restored == "clickhouse":
        return [(n, step) for n, step in steps if n not in PG_ONLY_STEPS]
    return [(n, step) for n, step in steps if n in PG_STEPS]


def _request(
    entry: Mapping[str, Any], actor: str = REPLAY_ACTOR
) -> tuple[ErasureRequestInput, RequestRow]:
    input = ErasureRequestInput(
        org=entry["org"],
        scope_type=entry["scope_type"],
        scope_value=entry["scope_value"],
        requester=str(entry.get("requester") or REPLAY_ACTOR),
        urgency="urgent" if entry.get("urgency") == "urgent" else "normal",
        purge=bool(entry.get("purge")),
    )
    row = request_row_template(
        erasure_id=str(entry["id"]),
        input=input,
        actor=actor,
        requested_at=to_naive_utc(entry["requested_at"]),
        attempts=1,
    )
    return input, row


async def replay(entry: Mapping[str, Any], restored: Restored, point: datetime) -> str:
    """Run a completed erasure `entry` again on the restored store, restored to `point`,
    under its own id; "completed" once every step ran, else the `ErasureStepError` of the
    first that failed. Its completed row carries no census (`erasure-re-verify` reads it
    unverified): one would count the other store's later writes."""
    input, row = _request(entry)
    erasure_id = str(entry["id"])
    completed_at = to_naive_utc(entry["completed_at"])
    if restored == "postgres":
        await pg_ledger.record(row)
        # A Postgres step the restored copy records as committed ran before the point:
        # run again, it would delete what the user wrote after it.
        marked = await pg_ledger.pg_steps(input.org, erasure_id)
        steps = [s for s in _replayed(input.scope_type, restored) if s[0] not in marked]
        await _run_steps(input, erasure_id, steps)
        await pg_ledger.complete(input.org, erasure_id, completed_at)
        return "completed"
    # What the restored ledger says, from a row checkpointed before the point (its steps'
    # deletes are in every table the backup copied after its start) or one an earlier
    # run of the reconcile wrote: completed, nothing runs again; still running, the
    # steps it names as done do not (the saga was running at the point). The pending
    # row keeps that list for a run again.
    trusted = await erasure_ledger.trusted_row(
        input.org, erasure_id, before=point, actor=REPLAY_ACTOR
    )
    if trusted is not None and trusted["status"] == "completed":
        return "completed"
    done = set(trusted["done"]) if trusted is not None else set()
    await checkpoint(
        row=row,
        status="failed",
        steps_completed=sorted(done),
        current_step=len(done),
        failure_reason=REPLAY_PENDING_REASON,
    )
    steps = [s for s in _replayed(input.scope_type, restored) if s[0] not in done]
    await _run_steps(input, erasure_id, steps)
    names = [n for n, _ in (USER_STEPS if input.scope_type == "user_id" else NAMESPACE_STEPS)]
    await checkpoint(
        row=row,
        status="completed",
        steps_completed=sorted(names),
        current_step=99,  # the saga's terminal row
        completed_at=completed_at,
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
            "replayed": True,
        },
        request_id=erasure_id,
    )
    return "completed"


async def _run_steps(
    input: ErasureRequestInput, erasure_id: str, steps: list[tuple[str, Any]]
) -> None:
    """`steps` in order over the erasure's scope, as its saga runs them: each under its
    name in the mutation tag, its failure an `ErasureStepError` naming it."""
    from server.core import namespace_write_marks as marks
    from server.erasure.domain.scopes import applied_purge, parse_namespace_list
    from server.erasure.store import mutations as erasure_mutations

    # As `run_saga` sets them.
    scope: str | list[str] = input.scope_value
    purge = applied_purge(input.scope_type, input.scope_value, bool(input.purge))
    if input.scope_type == "namespace_list":
        scope = parse_namespace_list(input.scope_value)
    if input.scope_type == "user_id":
        await marks.bump(input.org, marks.ORG_WIDE, "erasure")
    with erasure_mutations.saga_scope(erasure_id), erasure_mutations.purge_scope(purge):
        for name, step in steps:
            with erasure_mutations.step_scope(name):
                try:
                    await step(input.org, scope)
                except Exception as e:
                    raise ErasureStepError(name, e) from e


async def requeue(entry: Mapping[str, Any], restored: Restored, point: datetime) -> None:
    """Leave an open erasure `entry` to the recovery worker on the store restored to
    `point`."""
    # The saga the recovery worker resumes keeps this actor on every row it writes: not
    # REPLAY_ACTOR, whose rows a later reconcile trusts whatever their time.
    input, row = _request(entry, actor=RECOVERY_WORKER_ACTOR)
    if restored == "postgres":
        await pg_ledger.record(row)
        # The restore undid the Postgres steps that committed after its point; those the
        # restored copy records stay done (a step commits its mark before ClickHouse's
        # checkpoint names it), so the resume leaves later writes alone.
        kept = await pg_ledger.pg_steps(input.org, str(entry["id"]))
        done = set(entry.get("steps_completed") or [])
        steps = sorted((done - PG_STEPS) | kept)
        row["attempts"] = int(entry.get("attempts") or 1)
    else:
        # The erasure ended before the point, but its Postgres ledger row missed it (a
        # completion `saga._complete_ledger_row` could not write, an abandonment
        # `recovery._abandon` could not): the restored row says so, and is left as it is.
        trusted = await erasure_ledger.trusted_row(
            input.org, str(entry["id"]), before=point, actor=REPLAY_ACTOR
        )
        if trusted is not None and trusted["status"] in ("completed", "abandoned"):
            ended = trusted["status"]
            mark = pg_ledger.complete if ended == "completed" else pg_ledger.abandon
            await mark(input.org, str(entry["id"]), trusted["completed_at"])
            return
        # ClickHouse lost every checkpoint after its point: every step runs again but the
        # Postgres ones the live authority records as committed. The resumed saga is an
        # ordinary one: its external steps, its census and its announcement run.
        steps = sorted(set(entry.get("pg_steps") or []))
    await checkpoint(
        row=row,
        status="in_progress",
        steps_completed=steps,
        current_step=len(steps),
    )
