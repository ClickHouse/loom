"""``task.*`` reads: get, list, counts, history and the updates cursor."""

from __future__ import annotations

import time

from server.tasks.api.models_task import (
    InboxCount,
    TaskCountsInput,
    TaskCountsOutput,
    TaskGetInput,
    TaskGetOutput,
    TaskHistoryInput,
    TaskHistoryOutput,
    TaskListInput,
    TaskListOutput,
    TaskUpdatesInput,
    TaskUpdatesOutput,
)
from server.tasks.app import wakeups as task_wakeups
from server.tasks.app.common import (
    _caller_subject,
    _event_payload,
    _invalid,
    _load_readable_inbox,
    _loads,
    _payload,
    _readable,
)
from server.tasks.domain import cursors as task_cursors
from server.tasks.store import repo as repo


async def handle_task_get(input: TaskGetInput) -> TaskGetOutput:
    task = await repo.fetch_task(input.org, input.task_id)
    if task is None or not await _readable(task):
        return TaskGetOutput(found=False, task=None)
    return TaskGetOutput(found=True, task=_payload(task))


async def handle_task_list(input: TaskListInput) -> TaskListOutput:
    if input.posted_by_me:
        subject = _caller_subject()
        if not subject:
            return TaskListOutput(tasks=[], truncated=False)
        rows = await repo.list_tasks(
            input.org,
            poster=subject,
            statuses=list(input.statuses) or None,
            kinds=input.kinds or None,
            limit=input.limit + 1,
            fields=input.fields or None,
            newest_first=input.newest_first,
        )
    else:
        if not input.inbox:
            raise _invalid("name an inbox or set posted_by_me")
        await _load_readable_inbox(input.org, input.inbox)
        rows = await repo.list_tasks(
            input.org,
            inbox=input.inbox,
            statuses=list(input.statuses) or None,
            kinds=input.kinds or None,
            from_subject=input.posted_by,
            owner=input.owner,
            limit=input.limit + 1,
            fields=input.fields or None,
            newest_first=input.newest_first,
        )
    truncated = len(rows) > input.limit
    rows = rows[: input.limit]
    out = []
    for r in rows:
        if input.fields:
            d = dict(r)
            for k in ("brief", "outcome"):
                if k in d:
                    d[k] = _loads(d[k], {})
            d["task_id"] = str(d.get("task_id", ""))
            out.append(d)
        else:
            out.append(_payload(r).model_dump(mode="json"))
    return TaskListOutput(tasks=out, truncated=truncated)


async def handle_task_counts(input: TaskCountsInput) -> TaskCountsOutput:
    """Count tasks by status for one inbox or every inbox of a namespace."""

    if input.inbox:
        await _load_readable_inbox(input.org, input.inbox)
        rows = await repo.inbox_counts(input.org, inbox=input.inbox)
    else:
        rows = await repo.inbox_counts(input.org, namespace=input.namespace)
    return TaskCountsOutput(
        counts=[
            InboxCount(
                inbox=r["inbox"],
                status=str(r["status"]),
                blocked_on=str(r.get("blocked_on") or ""),
                n=int(r["n"]),
                oldest=r.get("oldest"),
            )
            for r in rows
        ]
    )


async def handle_task_history(input: TaskHistoryInput) -> TaskHistoryOutput:
    """Return a task's event history (posted, accepted, claimed, progress, finished, ...)."""

    task = await repo.fetch_task(input.org, input.task_id)
    if task is None or not await _readable(task):
        return TaskHistoryOutput(found=False, events=[])
    rows = await repo.task_history(input.org, input.task_id, limit=input.limit)
    return TaskHistoryOutput(found=True, events=[_event_payload(r) for r in rows])


async def handle_task_updates(input: TaskUpdatesInput) -> TaskUpdatesOutput:
    """The sender's cursor (events on tasks this caller posted), or with
    ``inbox`` the owner's stream. Long-polls like ``task.pull``."""

    if input.inbox:
        await _load_readable_inbox(input.org, input.inbox)
        key = task_wakeups.inbox_key(input.org, input.inbox)
        subject = None
    else:
        subject = _caller_subject(input.from_subject)
        if not subject:
            raise _invalid("no caller subject; name from_subject in dev mode")
        key = task_wakeups.poster_key(input.org, subject)
    started = time.monotonic()
    deadline = started + input.wait_seconds
    while True:
        rows = await repo.events_since(
            input.org,
            since_seq=input.since_seq,
            lag_s=task_cursors.LAG_SECONDS,
            inbox=input.inbox,
            from_subject=subject,
            limit=input.limit,
        )
        remaining = deadline - time.monotonic()
        if rows or remaining <= 0:
            break
        await task_wakeups.wait(key, remaining)
    next_seq = int(rows[-1]["seq"]) if rows else input.since_seq
    return TaskUpdatesOutput(
        events=[_event_payload(r) for r in rows],
        next_seq=next_seq,
        waited_ms=int((time.monotonic() - started) * 1000),
    )
