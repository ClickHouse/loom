"""What every task inbox handler shares: refusals, the caller's identity,
row <-> payload, the loaders, and the write that records a transition.

The handlers are split by what they serve: ``inboxes`` (inbox.*), ``trust``
(who may post and in which mode), ``transitions`` (task.* writes, the state
machine), ``reads`` (task.* reads) and ``sweep`` (the sweeper's
transitions). SQL lives in ``server.tasks.store.repo``.

Authorization has three sides and every handler names which one it takes:

* **owner side** — writer on the inbox's ``owner_namespace``: pull, claim,
  accept, and every transition of a task in the inbox.
* **poster side** — the principal that created the task (``from_subject``):
  may read it, cancel it while nobody owns it, and answer a block on the
  sender.
* **id-addressed reads** answer ``found = false`` on refusal.

Every transition writes one ``task_events`` row and one new ``tasks``
version row carrying every column (the ReplacingMergeTree rule), then wakes
the long polls that may care.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, cast, overload

from fastapi import HTTPException

from server.core.datetimes import utc_now as _now
from server.tasks.api.models_task import (
    InboxPayload,
    PolicyPayload,
    TaskEventPayload,
    TaskPayload,
)
from server.tasks.app import wakeups as task_wakeups
from server.tasks.store import repo as repo
from server.tasks.store.rows import InboxRow, PolicyRow, TaskEventRow, TaskRow

log = logging.getLogger(__name__)

TERMINAL = frozenset({"done", "wont_do", "failed", "cancelled"})
EDITABLE = frozenset({"triage", "todo", "blocked", "deferred"})


# ============================================================
# errors
# ============================================================


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status, detail=detail)


def _not_found() -> HTTPException:
    return _err(404, "RESOURCE_NOT_FOUND", "task not found")


def _invalid(message: str, **extra: Any) -> HTTPException:
    return _err(422, "VALIDATION_FAILED", message, **extra)


def _conflict(code: str, message: str, **extra: Any) -> HTTPException:
    return _err(409, code, message, **extra)


def _triage_next(inbox: InboxRow | None) -> str:
    """Who moves a triaged task, and with which op. Loom never moves it: the owner decides."""
    owner = f"agent {inbox['owner_agent']!r}" if inbox else "the inbox's owner agent"
    ns = f" on {inbox['owner_namespace']!r}" if inbox else ""
    return (
        f"it waits for the inbox owner ({owner}, or a person with write{ns}) to task.accept "
        "or task.wont_do it; the poster may task.cancel it; to skip triage for this poster "
        "the owner sets inbox.policy_set mode=auto"
    )


def _state_next(task: TaskRow, inbox: InboxRow | None = None) -> str:
    """The next step from ``task``'s state, named by op, for a refusal."""
    status = str(task.get("status") or "")
    if status == "triage":
        return _triage_next(inbox)
    if status == "todo":
        return "task.pull (or task.claim with its task_id) takes it"
    if status == "in_progress":
        owner = task.get("owner") or "?"
        fence = int(task.get("fence") or 0)
        return (
            f"it is owned by {owner!r} under fence {fence}; the owner ends it with "
            "task.complete / task.fail / task.defer, passing that fence"
        )
    if status == "blocked":
        return "task.unblock returns it to todo"
    if status == "deferred":
        return "task.resume returns it to todo (it also returns by itself at resume_at)"
    if status in ("failed", "wont_do", "cancelled"):
        return "it is finished; a person on the owner side may task.reopen it"
    if status == "done":
        return "it is finished"
    return "task.get shows its state"


async def _state_conflict(code: str, task: TaskRow, *, want: str = "") -> HTTPException:
    """A 409 that says the state, who can move it and the next op, instead of
    a bare "task is 'triage'" the caller can only guess past."""
    inbox = None
    if task.get("status") == "triage":
        inbox = await _load_inbox_or_none(str(task.get("org") or ""), str(task.get("inbox") or ""))
    status = str(task.get("status") or "")
    head = f"task is {status!r}" + (f"; {want}" if want else "")
    nxt = _state_next(task, inbox)
    return _conflict(code, f"{head}. Next: {nxt}", state=status, next=nxt)


# ============================================================
# identity
# ============================================================


# ClickHouse DateTime ends in 2106; a "never" from a caller is clamped so a
# far-future not_before or deadline is stored rather than refused as a 500.
_DATETIME_MAX = datetime(2105, 12, 31, 23, 59, 59)


@overload
def _naive(dt: datetime) -> datetime: ...
@overload
def _naive(dt: None) -> None: ...
@overload
def _naive(dt: datetime | None) -> datetime | None: ...
def _naive(dt: datetime | None) -> datetime | None:
    """ClickHouse DateTime columns are written naive-UTC; a tz-aware value
    from the body is converted, a naive one is taken as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return min(dt, _DATETIME_MAX)


def _caller_subject(fallback: str = "") -> str:
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is not None and auth.subject:
        return str(auth.subject)
    return fallback


def _actor(fallback: str) -> str:
    from server.memory.app.provenance import writer_actor

    return writer_actor(fallback=fallback) or fallback


def _caller_is_human() -> bool:
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is None:
        return False
    return not auth.is_service_account


async def _in_scope(org: str, namespace: str, *, write: bool) -> bool:
    from server.auth.authz import row_in_scope
    from server.operations.base import Role

    return await row_in_scope(org, namespace, Role.WRITER if write else Role.VIEWER)


async def _require_owner_side(org: str, namespace: str) -> None:
    if not await _in_scope(org, namespace, write=True):
        raise _err(403, "AUTH_INSUFFICIENT", "not a writer on the inbox's owner namespace")


async def _require_admin_side(org: str, namespace: str) -> None:
    from server.auth.authz import row_in_scope
    from server.operations.base import Role

    if not await row_in_scope(org, namespace, Role.ADMIN):
        raise _err(403, "AUTH_INSUFFICIENT", "not an admin of the inbox's owner namespace")


def _is_poster(task: TaskRow, subject: str) -> bool:
    return bool(subject) and task.get("from_subject") == subject


# ============================================================
# row <-> payload
# ============================================================


def _loads(text: Any, default: Any) -> Any:
    if not text:
        return default
    if isinstance(text, dict | list):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _payload(row: TaskRow) -> TaskPayload:
    return TaskPayload(
        task_id=str(row["task_id"]),
        org=row["org"],
        inbox=row["inbox"],
        namespace=row["namespace"],
        kind=row["kind"],
        priority=int(row["priority"]),
        disclosure=row["disclosure"],
        status=row["status"],
        reason=row.get("reason") or "",
        blocked_on=row.get("blocked_on") or "",
        not_before=row.get("not_before"),
        resume_at=row.get("resume_at"),
        deadline=row.get("deadline"),
        defer_count=int(row.get("defer_count") or 0),
        title=row["title"],
        brief=_loads(row.get("brief"), {}),
        from_agent=row["from_agent"],
        from_subject=row.get("from_subject") or "",
        origin=row.get("origin") or "",
        parent_task_id=str(row["parent_task_id"]) if row.get("parent_task_id") else None,
        owner=row.get("owner") or "",
        attempt=int(row.get("attempt") or 0),
        fence=int(row.get("fence") or 0),
        lease_until=row.get("lease_until"),
        outcome=_loads(row.get("outcome"), {}),
        last_actor=row.get("last_actor") or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=int(row["version"]),
    )


def _event_payload(row: TaskEventRow) -> TaskEventPayload:
    return TaskEventPayload(
        seq=int(row["seq"]),
        task_id=str(row["task_id"]),
        inbox=row["inbox"],
        event=row["event"],
        from_status=row.get("from_status") or "",
        to_status=row.get("to_status") or "",
        actor=row.get("actor") or "",
        agent=row.get("agent") or "",
        fence=int(row.get("fence") or 0),
        version=int(row.get("version") or 0),
        reason=row.get("reason") or "",
        patch=_loads(row.get("patch"), {}),
        note=row.get("note") or "",
        ts=row["ts"],
    )


def _inbox_payload(row: InboxRow) -> InboxPayload:
    return InboxPayload(
        org=row["org"],
        inbox=row["inbox"],
        owner_agent=row["owner_agent"],
        owner_namespace=row["owner_namespace"],
        description=row.get("description") or "",
        posters=list(row.get("posters") or []),
        accept_disclosure=list(row.get("accept_disclosure") or []),
        default_lease_seconds=int(row["default_lease_seconds"]),
        max_attempts=int(row["max_attempts"]),
        max_defers=int(row["max_defers"]),
        triage_escalate_after_seconds=int(row["triage_escalate_after_seconds"]),
        webhook_url=row.get("webhook_url") or "",
        created_by=row.get("created_by") or "",
        owner_subject=owner_subject(row),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=int(row["version"]),
    )


def _policy_payload(row: PolicyRow) -> PolicyPayload:
    return PolicyPayload(
        org=row["org"],
        inbox=row["inbox"],
        poster=row["poster"],
        kind=row["kind"],
        mode=row["mode"],
        require_evidence=list(row.get("require_evidence") or []),
        set_by=row.get("set_by") or "",
        set_at=row["set_at"],
        version=int(row["version"]),
    )


# ============================================================
# writes: one event + one version row; the loaders
# ============================================================


def _new_version(task: TaskRow, **changes: Any) -> TaskRow:
    """The next version row: every column of the current row, the changes
    applied, ``version``/``updated_at`` advanced. Datetimes are written naive
    UTC (the driver reads a naive value as UTC on insert here)."""
    row: dict[str, Any] = {c: task.get(c) for c in repo.TASK_FIELDS}
    row.update(changes)
    row["version"] = repo.next_version()
    row["updated_at"] = _naive(_now())
    for col in ("not_before", "resume_at", "deadline", "lease_until", "created_at"):
        row[col] = _naive(row.get(col))
    row["task_id"] = str(row["task_id"])
    row["parent_task_id"] = str(row["parent_task_id"]) if row.get("parent_task_id") else None
    row["is_deleted"] = int(row.get("is_deleted") or 0)
    return cast(TaskRow, row)


async def _record(
    task: TaskRow,
    *,
    event: str,
    actor: str,
    agent: str,
    reason: str = "",
    note: str = "",
    patch: dict[str, Any] | None = None,
    dedup_token: str = "",
    **changes: Any,
) -> tuple[TaskRow, int]:
    """Apply ``changes`` as a new version row and log the transition. Returns
    the new row and the event's ``seq``. Wakes the inbox's pulls and the
    poster's updates cursor."""
    from_status = str(task.get("status") or "")
    row = _new_version(task, **changes, last_actor=actor)
    to_status = str(row["status"])
    if "reason" in changes:
        row["reason"] = changes["reason"]
    elif to_status != from_status:
        # A status change carries the event's reason on the row (empty on a
        # move back to the happy path: resume, unblock, accept), so the board
        # can filter by it; a note or heartbeat leaves the row's reason alone.
        row["reason"] = reason
    seq = row["version"]
    await repo.insert_task_version(row, dedup_token=dedup_token)
    await repo.insert_event(
        {
            "org": row["org"],
            "inbox": row["inbox"],
            "namespace": row["namespace"],
            "task_id": row["task_id"],
            "seq": seq,
            "event": event,
            "from_status": from_status,
            "to_status": to_status,
            "actor": actor,
            "agent": agent,
            "from_subject": row.get("from_subject") or "",
            "fence": int(row.get("fence") or 0),
            "version": seq,
            "reason": reason or (changes.get("reason") or ""),
            "patch": json.dumps(patch or {}, sort_keys=True, default=str),
            "note": note,
            "ts": _naive(_now()),
        }
    )
    task_wakeups.notify(
        task_wakeups.inbox_key(row["org"], row["inbox"]),
        task_wakeups.poster_key(row["org"], row.get("from_subject") or ""),
    )
    return row, seq


async def _load_inbox(org: str, inbox: str) -> InboxRow:
    row = await repo.fetch_inbox_row(org, inbox)
    if row is None:
        raise _err(404, "RESOURCE_NOT_FOUND", f"inbox {inbox!r} is not registered")
    return row


async def _load_readable_inbox(org: str, inbox: str) -> InboxRow:
    """The inbox, for a read addressed by its name. A caller who may not read
    the owner namespace gets the unregistered-name 404, not a 403: the name is
    guessable, and a distinct refusal would confirm which inboxes exist."""
    row = await repo.fetch_inbox_row(org, inbox)
    if row is None or not await _in_scope(org, row["owner_namespace"], write=False):
        raise _err(404, "RESOURCE_NOT_FOUND", f"inbox {inbox!r} is not registered")
    return row


async def _load_inbox_or_none(org: str, inbox: str) -> InboxRow | None:
    try:
        return await repo.fetch_inbox_row(org, inbox)
    except Exception:  # noqa: BLE001 — a refusal's wording must not fail the refusal
        return None


def owner_subject(inbox: InboxRow) -> str:
    """The subject that registered the inbox, read from ``created_by``
    (``human:<subject>`` / ``process:<subject>``, stamped from the token).
    "" for an unattributed row (an unauthenticated stack)."""
    from server.memory.domain.trust import HUMAN_PREFIX, PROCESS_PREFIX

    created_by = str(inbox.get("created_by") or "")
    for prefix in (HUMAN_PREFIX, PROCESS_PREFIX):
        if created_by.startswith(prefix):
            return created_by[len(prefix) :]
    return ""


async def _load_task_owner_side(org: str, task_id: str) -> TaskRow:
    task = await repo.fetch_task(org, task_id)
    if task is None:
        raise _not_found()
    if not await _in_scope(org, task["namespace"], write=True):
        # A refusal must not confirm the id exists.
        raise _not_found()
    return task


async def _load_owned(org: str, task_id: str, *, agent: str, fence: int) -> TaskRow:
    task = await _load_task_owner_side(org, task_id)
    if task["status"] != "in_progress":
        raise await _state_conflict("TASK_NOT_IN_PROGRESS", task, want="this write needs the lease")
    if int(task.get("fence") or 0) != int(fence):
        raise _conflict(
            "TASK_FENCE_STALE",
            "the lease this fence belonged to is over",
            current_fence=int(task.get("fence") or 0),
            owner=task.get("owner") or "",
        )
    if agent != "-" and task.get("owner") not in ("", agent):
        raise _conflict("TASK_OWNED_ELSEWHERE", f"task is owned by {task.get('owner')!r}")
    return task


async def _release_lease(task: TaskRow, *, status: str) -> None:
    backend = await repo.lease_backend()
    await backend.set_owner(
        str(task["task_id"]),
        fence=int(task.get("fence") or 0),
        owner="",
        lease_until=_naive(datetime(1970, 1, 1, tzinfo=UTC)) or datetime(1970, 1, 1),
        status=status,
    )


async def _drop_lease(task: TaskRow) -> None:
    backend = await repo.lease_backend()
    await backend.delete(str(task["task_id"]))


async def _readable(task: TaskRow) -> bool:
    if await _in_scope(task["org"], task["namespace"], write=False):
        return True
    return _is_poster(task, _caller_subject())
