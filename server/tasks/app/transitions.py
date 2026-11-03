"""``task.*`` writes: the task state machine. Create, accept, pull and claim
(the compare-and-swap), the owner's fenced writes, the reason ops, update
and reassign."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

from server.core import leases, readiness
from server.core.datetimes import utc_now as _now
from server.core.errors import PreconditionFailedError
from server.tasks.api.models_task import (
    TaskAcceptInput,
    TaskBlockInput,
    TaskClaimInput,
    TaskCompleteInput,
    TaskCreateInput,
    TaskCreateOutput,
    TaskDeferInput,
    TaskFailInput,
    TaskHeartbeatInput,
    TaskOutput,
    TaskProgressInput,
    TaskPullInput,
    TaskPullOutput,
    TaskReasonInput,
    TaskReassignInput,
    TaskReassignOutput,
    TaskResumeInput,
    TaskUnblockInput,
    TaskUpdateInput,
    TaskUpdateOutput,
)
from server.tasks.app import wakeups as task_wakeups
from server.tasks.app.common import (
    EDITABLE,
    TERMINAL,
    _actor,
    _caller_subject,
    _conflict,
    _drop_lease,
    _err,
    _in_scope,
    _invalid,
    _is_poster,
    _load_inbox,
    _load_inbox_or_none,
    _load_owned,
    _load_readable_inbox,
    _load_task_owner_side,
    _loads,
    _log_transition,
    _naive,
    _next_row,
    _not_found,
    _owner_check,
    _owner_refusal,
    _payload,
    _record,
    _release_lease,
    _require_owner_side,
    _state_conflict,
    _triage_next,
)
from server.tasks.app.trust import (
    _decide_mode,
    _poster_may_post,
    _token_subject,
    _triage_note,
)
from server.tasks.domain.cursors import BACKFILLED
from server.tasks.domain.states import TaskStatus
from server.tasks.store import repo as repo
from server.tasks.store.rows import InboxRow, TaskRow


async def handle_task_create(input: TaskCreateInput) -> TaskCreateOutput:
    """Poster side. ``namespace`` is the poster's own; ``inbox`` the target."""

    inbox = await _load_inbox(input.org, input.inbox)
    subject = _caller_subject(input.from_subject)
    if not await _poster_may_post(inbox, subject, input.agent):
        # A caller who may neither post nor read the inbox gets the unregistered-name answer:
        # the name is guessable, and a 403 would confirm the inbox exists.
        if not await _in_scope(input.org, inbox["owner_namespace"], write=False):
            raise _err(404, "RESOURCE_NOT_FOUND", f"inbox {input.inbox!r} is not registered")
        raise _err(403, "AUTH_INSUFFICIENT", "this principal may not post to that inbox")
    if input.disclosure not in set(inbox.get("accept_disclosure") or []):
        raise _invalid(
            f"inbox {input.inbox!r} does not accept disclosure {input.disclosure!r}",
            accept_disclosure=list(inbox.get("accept_disclosure") or []),
        )
    if input.parent_task_id and not repo.is_uuid(input.parent_task_id):
        raise _invalid("parent_task_id is not a task id")
    async with _one_create_per_key(input, subject):
        if input.idempotency_key:
            prior = await repo.fetch_task_by_idempotency_key(
                input.org, input.inbox, subject, input.idempotency_key
            )
            if prior is not None:
                return await _prior_task(prior)
        return await _create(input, inbox, subject)


async def _prior_task(prior: TaskRow, *, backfill: bool = True) -> TaskCreateOutput:
    """The answer to a create whose key already holds `prior`: a never-claimed task a
    cut-off create left without its created event gets it here (`backfill=False`: the
    first create is still running and writes its own event). A lease row it left out
    is the lease reconcile's to write, after its grace."""
    if backfill and prior["status"] in ("todo", "triage") and int(prior["fence"]) == 0:
        await _backfill_created_event(prior)
    return TaskCreateOutput(task=_payload(prior), created=False, seq=int(prior["version"]))


async def _backfill_created_event(prior: TaskRow) -> None:
    """Log the created event a cut-off create left out; a late first create's event
    shares its dedup token, and readers keep only the first."""
    events = await repo.task_history(prior["org"], str(prior["task_id"]), consistent=True)
    if any(e["event"] == "created" for e in events):
        return
    if events:
        # The task was accepted or edited since: it landed in the status the first of
        # those events left, and its created event goes just before that one. A cursor
        # reader already past that seq never sees it: it is history only.
        landed, seq = str(events[0]["from_status"]), int(events[0]["seq"]) - 1
    else:
        # Nothing after it: a fresh seq, so cursor readers (webhooks, task.updates,
        # task.watch) past the task's version still deliver it.
        landed, seq = str(prior["status"]), await repo.server_version(int(prior["version"]))
    agent = str(prior["from_agent"])
    await _log_transition(
        cast(TaskRow, {**prior, "status": ""}),
        cast(TaskRow, {**prior, "status": landed}),
        seq,
        event="created",
        actor=_actor(f"process:{agent}"),
        agent=agent,
        # The mode follows from where it landed; whether a policy decided is not kept.
        patch={
            "kind": prior["kind"],
            "mode": "auto" if landed == "todo" else "triage",
            BACKFILLED: True,
        },
    )


@asynccontextmanager
async def _one_create_per_key(input: TaskCreateInput, subject: str) -> AsyncIterator[None]:
    """Creates with one poster's idempotency key run one at a time across replicas, from
    the lookup to the insert, so a retry that waited finds the task the first one
    stored. 409 while another create keeps the key past the wait, 503 when the
    lease store does not answer (as for a memory key's write lock)."""
    if not input.idempotency_key:
        yield
        return
    ident = "\x1f".join((input.org, input.inbox, subject, input.idempotency_key))
    name = "task.create/" + hashlib.sha256(ident.encode()).hexdigest()[:40]
    async with leases.holding(name, ttl_s=60, wait_s=10) as held:
        if not held:
            raise PreconditionFailedError(
                "another task.create with this idempotency_key is still running; retry"
            )
        yield


async def _create(input: TaskCreateInput, inbox: InboxRow, subject: str) -> TaskCreateOutput:
    mode, policy = await _decide_mode(
        inbox, subject=subject, agent=input.agent, kind=input.kind, brief=input.brief
    )
    if mode == "deny":
        raise _err(
            403,
            "TASK_POSTER_DENIED",
            f"inbox {input.inbox!r} denies kind {input.kind!r} from this poster",
        )
    status: TaskStatus = "todo" if mode == "auto" else "triage"
    now = _naive(_now())
    task_id = str(uuid.uuid4())
    task: TaskRow = {
        "org": input.org,
        "inbox": input.inbox,
        "namespace": inbox["owner_namespace"],
        "task_id": task_id,
        "kind": input.kind,
        "priority": input.priority,
        "disclosure": input.disclosure,
        "status": status,
        "reason": "",
        "blocked_on": "",
        "not_before": _naive(input.not_before),
        "resume_at": None,
        "deadline": _naive(input.deadline),
        "defer_count": 0,
        "title": input.title,
        "brief": json.dumps(input.brief, sort_keys=True, default=str),
        "from_agent": input.agent,
        "from_subject": subject,
        "origin": input.origin,
        "parent_task_id": input.parent_task_id or None,
        "idempotency_key": input.idempotency_key,
        "owner": "",
        "attempt": 0,
        "fence": 0,
        "lease_until": None,
        "outcome": "{}",
        "last_actor": "",
        "created_at": now,
        "updated_at": now,
        "version": 0,
        "is_deleted": 0,
    }
    # The transition starts from a "previous" row with no status yet, so the
    # event's from_status is empty and to_status is the landing state.
    start = cast(TaskRow, {**task, "status": ""})
    actor = _actor(f"process:{input.agent}")
    row, seq = await _next_row(start, actor=actor, status=status)
    if input.idempotency_key:
        stored = await repo.insert_created_task(row)
        if str(stored["task_id"]) != task_id:
            # A create whose lease ran out lost the race; the winner logs its own event.
            return await _prior_task(stored, backfill=False)
    else:
        await repo.insert_task_version(row)
    await _log_transition(
        start,
        row,
        seq,
        event="created",
        actor=actor,
        agent=input.agent,
        patch={"kind": input.kind, "mode": mode, "policy": bool(policy)},
    )
    backend = await repo.lease_backend()
    await backend.ensure_row(task_id, input.org, input.inbox, 0)
    nxt = _triage_note(inbox, input.agent, policy) if status == "triage" else ""
    return TaskCreateOutput(task=_payload(row), created=True, seq=seq, next=nxt)


async def handle_task_accept(input: TaskAcceptInput) -> TaskOutput:
    """Accept a triaged task into the inbox's backlog (owner side), optionally setting priority and
    constraints."""

    task = await _load_task_owner_side(input.org, input.task_id)
    if task["status"] != "triage":
        raise await _state_conflict(
            "TASK_NOT_IN_TRIAGE", task, want="only a triaged task is accepted"
        )
    subject = _caller_subject()
    if _is_poster(task, subject):
        inbox = await _load_inbox_or_none(input.org, str(task.get("inbox") or ""))
        nxt = _triage_next(inbox)
        raise _err(
            403,
            "TASK_SELF_ACCEPT",
            f"a poster may not accept its own task. Next: {nxt}",
            state="triage",
            next=nxt,
        )
    changes: dict[str, Any] = {"status": "todo"}
    patch: dict[str, Any] = {}
    if input.priority is not None:
        changes["priority"] = input.priority
        patch["priority"] = input.priority
    if input.constraints is not None:
        brief = _loads(task.get("brief"), {})
        brief["constraints"] = input.constraints
        changes["brief"] = json.dumps(brief, sort_keys=True, default=str)
        patch["constraints"] = input.constraints
    row, seq = await _record(
        task,
        event="accepted",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        note=input.note,
        patch=patch,
        **changes,
    )
    return TaskOutput(task=_payload(row), seq=seq)


# ============================================================
# task.pull / task.claim — the compare-and-swap
# ============================================================


async def _claim_one(
    task: TaskRow, *, claimant: str, actor: str, lease_seconds: int
) -> tuple[TaskRow, int] | None:
    backend = await repo.lease_backend()
    lease_until = _naive(_now() + timedelta(seconds=lease_seconds))
    assert lease_until is not None
    task_id, fence = str(task["task_id"]), int(task.get("fence") or 0)
    result = await backend.try_claim(
        task_id, claimant, expected_fence=fence, lease_until=lease_until
    )
    if not result.won:
        return None
    # A lease row can be written from a status read nobody holds (the lease reconcile or
    # a reopen): the claim stands only if the task, read after winning, is still the todo
    # task at the fence it claimed. Otherwise it undoes its own bump and nothing else; a
    # row left for an ended task is the lease sweep's to remove.
    now = await repo.fetch_task(task["org"], task_id)
    if now is None or now["status"] != "todo" or int(now.get("fence") or 0) != fence:
        await backend.give_back(task_id, claimant, held_fence=result.fence, fence=fence)
        return None
    return await _record(
        task,
        event="claimed",
        actor=actor,
        agent=claimant,
        patch={"lease_seconds": lease_seconds, "backend": backend.name},
        status="in_progress",
        owner=claimant,
        fence=result.fence,
        lease_until=lease_until,
        attempt=int(task.get("attempt") or 0) + 1,
        reason="",
        blocked_on="",
    )


async def handle_task_pull(input: TaskPullInput) -> TaskPullOutput:
    """Owner side. ``status = todo`` claims up to ``limit`` tasks; ``triage``
    returns review items unclaimed. With ``wait_seconds`` the call blocks
    until something arrives (woken by a create/accept in this process, or by
    the periodic re-read for one that landed elsewhere), or until this
    process starts draining. A pull cancelled before it answers puts the
    tasks it claimed back, so none stays leased to a caller that never got it."""

    inbox = await _load_readable_inbox(input.org, input.inbox)
    await _require_owner_side(input.org, inbox["owner_namespace"])
    lease_seconds = input.lease_seconds or int(inbox["default_lease_seconds"])
    claimant = input.agent if input.agent != "-" else inbox["owner_agent"]
    actor = _actor(f"process:{claimant}")
    backend = await repo.lease_backend()
    claimed: list[TaskRow] = []
    try:
        return await _pull(input, inbox, claimed, claimant, actor, lease_seconds, backend.name)
    except asyncio.CancelledError:
        if input.status != "triage":
            await asyncio.shield(_return_claims(claimed, actor=actor, agent=claimant))
        raise


async def _return_claims(rows: list[TaskRow], *, actor: str, agent: str) -> None:
    for row in rows:
        await _release_lease(row, status="todo")
        await _record(
            row,
            event="claim_returned",
            actor=actor,
            agent=agent,
            reason="the pull was cancelled before it answered",
            status="todo",
            owner="",
            lease_until=None,
        )


async def _pull(
    input: TaskPullInput,
    inbox: Any,
    claimed: list[TaskRow],
    claimant: str,
    actor: str,
    lease_seconds: int,
    backend_name: str,
) -> TaskPullOutput:
    started = time.monotonic()
    deadline = started + input.wait_seconds
    key = task_wakeups.inbox_key(input.org, input.inbox)
    while True:
        if input.status == "triage":
            rows = await repo.list_tasks(
                input.org,
                inbox=input.inbox,
                statuses=["triage"],
                kinds=input.kinds or None,
                limit=input.limit,
            )
            claimed = rows
        else:
            cands = await repo.claimable_candidates(
                input.org,
                input.inbox,
                kinds=input.kinds or None,
                now=_naive(_now()) or _now(),
                limit=max(input.limit * 2, input.limit),
            )
            for cand in cands:
                # A claim that won its compare-and-set lands in `claimed` even
                # when the pull is cancelled meanwhile, so it can be returned.
                claim = asyncio.ensure_future(
                    _claim_one(cand, claimant=claimant, actor=actor, lease_seconds=lease_seconds)
                )
                try:
                    got = await asyncio.shield(claim)
                except asyncio.CancelledError:
                    got = await claim
                    if got is not None:
                        claimed.append(got[0])
                    raise
                if got is not None:
                    claimed.append(got[0])
                if len(claimed) >= input.limit:
                    break
        remaining = deadline - time.monotonic()
        if claimed or remaining <= 0 or readiness.draining():
            break
        await task_wakeups.wait(key, remaining)
    waited_ms = int((time.monotonic() - started) * 1000)
    note = ""
    if not claimed and input.status == "todo":
        note = await _triage_waiting_note(input.org, input.inbox)
    return TaskPullOutput(
        tasks=[_payload(r) for r in claimed], waited_ms=waited_ms, backend=backend_name, note=note
    )


async def _triage_waiting_note(org: str, inbox: str) -> str:
    """An empty pull that leaves triaged tasks behind says so: pull only takes todo, so a
    bare "no tasks" would hide the one just posted."""
    try:
        rows = await repo.inbox_counts(org, inbox=inbox)
    except Exception:  # noqa: BLE001 — a hint, never a failure
        return ""
    waiting = sum(int(r.get("n") or 0) for r in rows if r.get("status") == "triage")
    if not waiting:
        return ""
    return (
        f"no todo task; {waiting} wait in triage (pull only takes todo). "
        "task.pull status=triage lists them; the inbox owner accepts one with task.accept"
    )


async def handle_task_claim(input: TaskClaimInput) -> TaskOutput:
    """Claim a todo task under a lease; the returned fence guards every later progress, block or
    finish."""

    task = await _load_task_owner_side(input.org, input.task_id)
    if task["status"] != "todo":
        raise await _state_conflict("TASK_NOT_CLAIMABLE", task, want="only a todo task is claimed")
    not_before = task.get("not_before")
    if not_before and not_before > _naive(_now()):
        raise _conflict(
            "TASK_NOT_YET", "task is scheduled for later", not_before=task["not_before"]
        )
    inbox = await _load_inbox(input.org, task["inbox"])
    claimant = input.agent if input.agent != "-" else inbox["owner_agent"]
    got = await _claim_one(
        task,
        claimant=claimant,
        actor=_actor(f"process:{claimant}"),
        lease_seconds=input.lease_seconds or int(inbox["default_lease_seconds"]),
    )
    if got is None:
        raise _conflict("TASK_ALREADY_CLAIMED", "another claimant won the lease")
    return TaskOutput(task=_payload(got[0]), seq=got[1])


async def handle_task_heartbeat(input: TaskHeartbeatInput) -> TaskOutput:
    task = await _load_owned(input.org, input.task_id, agent=input.agent, fence=input.fence)
    inbox = await _load_inbox(input.org, task["inbox"])
    lease_until = _naive(
        _now() + timedelta(seconds=input.lease_seconds or int(inbox["default_lease_seconds"]))
    )
    assert lease_until is not None
    backend = await repo.lease_backend()
    ok = await backend.set_owner(
        str(task["task_id"]),
        fence=int(task["fence"]),
        owner=str(task.get("owner") or input.agent),
        lease_until=lease_until,
        status="in_progress",
    )
    if not ok:
        raise _conflict("TASK_FENCE_STALE", "the lease this fence belonged to is over")
    row, seq = await _record(
        task,
        event="heartbeat",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        lease_until=lease_until,
    )
    return TaskOutput(task=_payload(row), seq=seq)


async def handle_task_progress(input: TaskProgressInput) -> TaskOutput:
    """Record a progress note on a claimed task and extend its lease."""

    task = await _load_owned(input.org, input.task_id, agent=input.agent, fence=input.fence)
    row, seq = await _record(
        task,
        event="progress",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        note=input.note,
    )
    return TaskOutput(task=_payload(row), seq=seq)


async def _load_for_park(
    input: TaskDeferInput | TaskBlockInput, *, status: TaskStatus, code: str, want: str = ""
) -> TaskRow:
    """The owner-side task a defer or block parks: an in_progress task must be
    the caller's own (its lease is released into `status`); otherwise only a
    todo task may be parked."""
    task = await _load_task_owner_side(input.org, input.task_id)
    if task["status"] == "in_progress":
        task = await _load_owned(input.org, input.task_id, agent=input.agent, fence=input.fence)
        await _release_lease(task, status=status)
    elif task["status"] != "todo":
        raise await _state_conflict(code, task, want=want)
    return task


async def handle_task_defer(input: TaskDeferInput) -> TaskOutput:
    """Owner side. Releases the lease and parks the task; after the inbox's
    ``max_defers`` it is blocked on a human instead of parked again."""

    task = await _load_for_park(
        input,
        status="deferred",
        code="TASK_NOT_DEFERRABLE",
        want="only a todo or in_progress task is deferred",
    )
    actor = _actor(f"process:{input.agent}")
    inbox = await _load_inbox(input.org, task["inbox"])
    defers = int(task.get("defer_count") or 0) + 1
    if defers > int(inbox["max_defers"]):
        row, seq = await _record(
            task,
            event="blocked",
            actor=actor,
            agent=input.agent,
            reason=f"deferred {defers} times; last reason: {input.reason}",
            status="blocked",
            blocked_on="human",
            owner="",
            lease_until=None,
            defer_count=defers,
            resume_at=None,
        )
    else:
        row, seq = await _record(
            task,
            event="deferred",
            actor=actor,
            agent=input.agent,
            reason=input.reason,
            patch={"resume_at": input.resume_at},
            status="deferred",
            owner="",
            lease_until=None,
            defer_count=defers,
            resume_at=_naive(input.resume_at),
        )
    return TaskOutput(task=_payload(row), seq=seq)


async def handle_task_block(input: TaskBlockInput) -> TaskOutput:
    """Mark a task blocked, releasing its lease, with the reason and what it is blocked on."""

    task = await _load_for_park(input, status="blocked", code="TASK_NOT_BLOCKABLE")
    row, seq = await _record(
        task,
        event="blocked",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        reason=input.reason,
        patch={"blocked_on": input.blocked_on},
        status="blocked",
        blocked_on=input.blocked_on,
        owner="",
        lease_until=None,
    )
    return TaskOutput(task=_payload(row), seq=seq)


async def handle_task_unblock(input: TaskUnblockInput) -> TaskOutput:
    """Owner side, or the poster when the task is blocked on the sender."""

    task = await repo.fetch_task(input.org, input.task_id)
    if task is None:
        raise _not_found()
    subject = _caller_subject()
    poster = task.get("blocked_on") == "sender" and _is_poster(task, subject)
    owner_side, unverified = await _owner_check(input.org, task["namespace"])
    if not owner_side and not poster:
        _owner_refusal(unverified)
        raise _not_found()
    if task["status"] != "blocked":
        raise await _state_conflict("TASK_NOT_BLOCKED", task)
    row, seq = await _record(
        task,
        event="unblocked",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        note=input.note,
        status="todo",
        blocked_on="",
        reason="",
    )
    return TaskOutput(task=_payload(row), seq=seq)


async def handle_task_resume(input: TaskResumeInput) -> TaskOutput:
    """Owner side: a deferred task back to ``todo`` now (a person or the bot
    decided not to wait for ``resume_at``)."""

    task = await _load_task_owner_side(input.org, input.task_id)
    if task["status"] != "deferred":
        raise await _state_conflict("TASK_NOT_DEFERRED", task)
    row, seq = await _record(
        task,
        event="resumed",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        note=input.note,
        status="todo",
        resume_at=None,
        reason="",
    )
    return TaskOutput(task=_payload(row), seq=seq)


async def _finish(
    input: Any, *, status: str, event: str, reason: str, outcome: dict[str, Any], note: str = ""
) -> TaskOutput:
    task = await _load_owned(input.org, input.task_id, agent=input.agent, fence=input.fence)
    await _drop_lease(task)
    row, seq = await _record(
        task,
        event=event,
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        reason=reason,
        note=note,
        patch={"outcome_keys": sorted(outcome.keys())},
        status=status,
        owner="",
        lease_until=None,
        outcome=json.dumps(outcome, sort_keys=True, default=str),
    )
    return TaskOutput(task=_payload(row), seq=seq)


async def handle_task_complete(input: TaskCompleteInput) -> TaskOutput:
    return await _finish(
        input, status="done", event="done", reason="", outcome=input.outcome, note=input.note
    )


async def handle_task_fail(input: TaskFailInput) -> TaskOutput:
    return await _finish(
        input, status="failed", event="failed", reason=input.reason, outcome=input.outcome
    )


# ============================================================
# wont_do / cancel / reopen — with a reason, from several states
# ============================================================


async def _load_for_reason_op(input: TaskReasonInput) -> tuple[TaskRow, bool, bool]:
    """The row, whether the caller is on the owner side (else the poster), and
    whether an owner-side refusal was unverified (see `_owner_check`)."""
    task = await repo.fetch_task(input.org, input.task_id)
    if task is None:
        raise _not_found()
    poster = _is_poster(task, _caller_subject())
    owner_side, unverified = await _owner_check(input.org, task["namespace"])
    if not owner_side and not poster:
        _owner_refusal(unverified)
        raise _not_found()
    if task["status"] in TERMINAL:
        raise await _state_conflict("TASK_TERMINAL", task, want="it is already finished")
    if task["status"] == "in_progress":
        if not owner_side:
            _owner_refusal(unverified)
            raise _conflict("TASK_IN_PROGRESS", "the task is owned; ask its owner")
        if input.fence is not None and int(input.fence) != int(task.get("fence") or 0):
            raise _conflict("TASK_FENCE_STALE", "the lease this fence belonged to is over")
    return task, owner_side, unverified


async def handle_task_wont_do(input: TaskReasonInput) -> TaskOutput:
    """Owner side (an approver from triage, the owner from anywhere)."""

    task, owner_side, unverified = await _load_for_reason_op(input)
    if not owner_side:
        _owner_refusal(unverified)
        raise _not_found()
    # The lease goes before the status row, so a claim racing this one finds none.
    await _drop_lease(task)
    row, seq = await _record(
        task,
        event="wont_do",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        reason=input.reason,
        status="wont_do",
        owner="",
        lease_until=None,
    )
    return TaskOutput(task=_payload(row), seq=seq)


async def handle_task_cancel(input: TaskReasonInput) -> TaskOutput:
    """Owner side, or the poster while nobody owns the task."""

    task, _owner_side, _unverified = await _load_for_reason_op(input)
    # The lease goes before the status row, so a claim racing this one finds none.
    await _drop_lease(task)
    row, seq = await _record(
        task,
        event="cancelled",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        reason=input.reason,
        status="cancelled",
        owner="",
        lease_until=None,
    )
    return TaskOutput(task=_payload(row), seq=seq)


async def handle_task_reopen(input: TaskReasonInput) -> TaskOutput:
    """Owner side, a person: ``failed`` or ``wont_do`` back to ``todo``."""

    task = await _load_task_owner_side(input.org, input.task_id)
    if task["status"] not in ("failed", "wont_do", "cancelled"):
        raise await _state_conflict("TASK_NOT_REOPENABLE", task)
    backend = await repo.lease_backend()
    await backend.ensure_row(
        str(task["task_id"]), task["org"], task["inbox"], int(task["fence"]), reset_idle=True
    )
    row, seq = await _record(
        task,
        event="reopened",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        reason=input.reason,
        status="todo",
        owner="",
        lease_until=None,
        blocked_on="",
    )
    return TaskOutput(task=_payload(row), seq=seq)


# ============================================================
# task.update — the brief, under optimistic concurrency
# ============================================================


async def handle_task_update(input: TaskUpdateInput) -> TaskUpdateOutput:
    """Owner side or poster. Allowed while the task is editable (triage,
    todo, blocked, deferred); an ``if_match_version`` behind the row answers
    409 with the current row so the editor can merge."""

    task = await repo.fetch_task(input.org, input.task_id)
    if task is None:
        raise _not_found()
    poster = _is_poster(task, _caller_subject())
    owner_side, unverified = await _owner_check(input.org, task["namespace"])
    if not owner_side and not poster:
        _owner_refusal(unverified)
        raise _not_found()
    if task["status"] not in EDITABLE:
        raise _conflict("TASK_NOT_EDITABLE", f"task is {task['status']!r}; add a note instead")
    if int(task["version"]) != int(input.if_match_version):
        raise _conflict(
            "PRECONDITION_FAILED",
            "task changed since it was read",
            current_version=int(task["version"]),
            current=_payload(task).model_dump(mode="json"),
        )
    changes: dict[str, Any] = {}
    patch: dict[str, Any] = {}
    if input.title is not None:
        changes["title"] = input.title
        patch["title"] = input.title
    if input.brief is not None:
        changes["brief"] = json.dumps(input.brief, sort_keys=True, default=str)
        patch["brief_keys"] = sorted(input.brief.keys())
    if input.priority is not None:
        changes["priority"] = input.priority
        patch["priority"] = input.priority
    if input.clear_deadline:
        changes["deadline"] = None
        patch["deadline"] = None
    elif input.deadline is not None:
        changes["deadline"] = _naive(input.deadline)
        patch["deadline"] = input.deadline
    if input.clear_not_before:
        changes["not_before"] = None
        patch["not_before"] = None
    elif input.not_before is not None:
        changes["not_before"] = _naive(input.not_before)
        patch["not_before"] = input.not_before
    if not changes and not input.note:
        raise _invalid("nothing to change")
    row, seq = await _record(
        task,
        event="updated",
        actor=_actor(f"process:{input.agent}"),
        agent=input.agent,
        note=input.note,
        patch=patch,
        **changes,
    )
    return TaskUpdateOutput(task=_payload(row), seq=seq)


async def handle_task_reassign(input: TaskReassignInput) -> TaskReassignOutput:
    """Owner side: cancel here, re-post into another inbox as a child task
    (the destination's trust policy applies to the original poster)."""

    task = await _load_task_owner_side(input.org, input.task_id)
    if task["status"] in TERMINAL:
        raise await _state_conflict("TASK_TERMINAL", task, want="it is already finished")
    dest = await _load_inbox(input.org, input.to_inbox)
    if dest["inbox"] == task["inbox"]:
        raise _invalid("the task is already in that inbox")
    if task["disclosure"] not in set(dest.get("accept_disclosure") or []):
        raise _invalid(
            f"inbox {input.to_inbox!r} does not accept disclosure {task['disclosure']!r}"
        )
    # The destination's decision comes first: a refusal (deny, or evidence its policy
    # requires) leaves the task where it is instead of cancelled with no child.
    mode, _policy = await _decide_mode(
        dest,
        subject=task.get("from_subject") or "",
        agent=task.get("from_agent") or "-",
        kind=task["kind"],
        brief=_loads(task.get("brief"), {}),
        # the stored poster subject was stamped from its token when auth is on
        verified=bool(task.get("from_subject")) and bool(_token_subject()),
    )
    if mode == "deny":
        raise _err(403, "TASK_POSTER_DENIED", "the destination denies this poster and kind")
    actor = _actor(f"process:{input.agent}")
    # The lease goes before the status row, so a claim racing this one finds none.
    await _drop_lease(task)
    old, _seq = await _record(
        task,
        event="reassigned",
        actor=actor,
        agent=input.agent,
        reason=input.reason,
        patch={"to_inbox": input.to_inbox},
        status="cancelled",
        owner="",
        lease_until=None,
    )
    now = _naive(_now())
    new_id = str(uuid.uuid4())
    child = TaskRow(**task)
    child.update(
        {
            "task_id": new_id,
            "inbox": dest["inbox"],
            "namespace": dest["owner_namespace"],
            "status": "todo" if mode == "auto" else "triage",
            "reason": "",
            "blocked_on": "",
            "resume_at": None,
            "defer_count": 0,
            "parent_task_id": str(task["task_id"]),
            "idempotency_key": "",
            "owner": "",
            "attempt": 0,
            "fence": 0,
            "lease_until": None,
            "outcome": "{}",
            "created_at": now,
        }
    )
    new, seq = await _record(
        cast(TaskRow, {**child, "status": ""}),
        event="created",
        actor=actor,
        agent=input.agent,
        reason=input.reason,
        patch={"kind": task["kind"], "mode": mode, "reassigned_from": str(task["task_id"])},
        status=child["status"],
    )
    backend = await repo.lease_backend()
    await backend.ensure_row(new_id, input.org, dest["inbox"], 0)
    return TaskReassignOutput(task=_payload(old), replacement=_payload(new))
