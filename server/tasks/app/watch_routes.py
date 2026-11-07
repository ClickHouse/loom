"""``GET /v1/task/watch`` — server-sent events over the task inbox plane
(wake-up layer).

A wake-up hint, never the delivery: the stream replays ``loom.task_events``
rows after a cursor and then waits on the in-process wake-ups
(:mod:`server.tasks.app.wakeups`) between re-reads, so a receiver
learns within a re-read window that its inbox moved and pulls with
``task.pull`` (the claim is the pull); a sender learns that a task it
posted moved and reads ``task.updates``. The tables stay the source of
truth: a client that missed frames reconnects with ``Last-Event-ID`` and
gets everything after it.

GET-shaped like ``/v1/knowledge.download`` (bearer, ``verify_token``, the
handlers' ``_in_scope`` rule): the owner side (``inbox=``) needs writer on
the inbox's owner namespace; the poster side (``posted_by_me=1``) streams
the events of tasks the caller's subject posted. The stream is bounded
(``max_seconds``, ≤ 600) so the proxy's read timeout never cuts it mid
frame, and it writes no ledger row (it is not a registered operation; the
pulls it wakes are). An admitted watch of an inbox in a restricted namespace
leaves a ``namespace.restricted_access`` audit row for the inbox's owner
namespace, as the op gate does for the namespace a request names.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from server.settings import settings
from server.tasks.app import wakeups as task_wakeups
from server.tasks.app.common import _err
from server.tasks.domain import cursors as task_cursors
from server.tasks.store.rows import TaskEventRow

log = logging.getLogger(__name__)

watch_router = APIRouter()

RETRY_MS = 3000
DEFAULT_HEARTBEAT_SECONDS = 15
MAX_HEARTBEAT_SECONDS = 60
DEFAULT_MAX_SECONDS = 300
MAX_MAX_SECONDS = 600
READ_LIMIT = 200

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # nginx: do not buffer the stream; the proxy has proxy_buffering off too.
    "X-Accel-Buffering": "no",
    # GZipMiddleware leaves a response alone once Content-Encoding is set, so
    # frames leave the process as they are written instead of after 1 KiB.
    "Content-Encoding": "identity",
}

Reader = Callable[[int], Awaitable[list[TaskEventRow]]]
Waiter = Callable[[str, float], Awaitable[float]]


def frame_event(row: TaskEventRow) -> str:
    """One SSE frame for a ``task_events`` row: ``id`` is the row's ``seq``
    (the cursor a reconnect sends back as ``Last-Event-ID``)."""
    from server.tasks.app.common import _event_payload

    payload = _event_payload(row).model_dump(mode="json")
    return f"id: {int(row['seq'])}\ndata: {json.dumps(payload, default=str)}\n\n"


async def event_stream(
    *,
    key: str,
    since_seq: int,
    heartbeat_seconds: float,
    max_seconds: float,
    read: Reader,
    wait: Waiter = task_wakeups.wait,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """The frames: ``retry`` once, then every event after the cursor, a
    ``: keepalive`` comment when nothing happened for ``heartbeat_seconds``,
    and an end after ``max_seconds`` so the client reconnects with the last
    id. ``read(since_seq)`` is the cursor read; ``wait(key, s)`` blocks until a
    wake-up on ``key`` or ``s`` seconds (the helper caps it at its re-read
    window, so a write on another process is caught by the next read)."""
    yield f"retry: {RETRY_MS}\n\n"
    started = clock()
    deadline = started + max_seconds
    last_frame = started
    cursor = int(since_seq)
    while True:
        rows = await read(cursor)
        for row in rows:
            cursor = max(cursor, int(row["seq"]))
            yield frame_event(row)
        now = clock()
        if now >= deadline:
            return  # a busy inbox ends at max_seconds too; the client resumes from its cursor
        if rows:
            last_frame = now
            continue
        beat_in = heartbeat_seconds - (now - last_frame)
        if beat_in <= 0:
            yield ": keepalive\n\n"
            last_frame = now
            continue
        await wait(key, min(beat_in, deadline - now))


def _bearer_auth(request: Request) -> Any:
    """The verified principal — the HTTP transport's rule: a bearer that is
    present is verified whether or not the deployment requires one (a CI
    server runs with LOOM_AUTH_REQUIRED=false and its tests still mint
    tokens for strangers); a missing bearer is a 401 only when auth is on,
    and None (no principal, every scope check answers yes) otherwise."""
    from server.auth.bearer import missing_bearer, verify_bearer_header

    auth = verify_bearer_header(request.headers.get("Authorization"))
    if auth is None and settings.auth_actually_required():
        raise missing_bearer()
    return auth


def _cursor_from(request: Request, since_seq: int) -> int:
    """``Last-Event-ID`` (what the browser or a reconnecting client sends)
    wins over the query parameter; a non-numeric header is ignored."""
    header = request.headers.get("Last-Event-ID", "").strip()
    if header.isdigit():
        return int(header)
    return max(0, int(since_seq))


def _clamp(value: int, *, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


async def _resolve_key(
    org: str, inbox: str, posted_by_me: bool, from_subject: str, auth: Any
) -> tuple[str, str | None, str | None]:
    """(wake-up key, inbox filter, from_subject filter) for the side asked for,
    after the handlers' authorization rule; an admitted inbox watch leaves the
    restricted-access row for the inbox's owner namespace."""
    from server.auth.authz import may_act_in_org
    from server.identity.app.restricted_access import note_restricted_access
    from server.tasks.app.common import _caller_subject, _in_scope
    from server.tasks.store import repo as repo

    if auth is not None and not may_act_in_org(auth, org):
        raise _err(403, "AUTH_INSUFFICIENT", "token is for another org")
    if inbox:
        row = await repo.fetch_inbox_row(org, inbox)
        # Someone who cannot even read the owner namespace gets the unknown-
        # inbox 404 (as task.updates does), so the refusal names no inbox; a
        # reader short of writer already sees the inbox and gets the 403.
        if row is None or not await _in_scope(org, row["owner_namespace"], write=False):
            raise _err(404, "RESOURCE_NOT_FOUND", "inbox not found")
        if not await _in_scope(org, row["owner_namespace"], write=True):
            raise _err(403, "AUTH_INSUFFICIENT", "not a writer on the inbox's owner namespace")
        await note_restricted_access(
            auth, op="task.watch", org=org, namespace=row["owner_namespace"], transport="http"
        )
        return task_wakeups.inbox_key(org, inbox), inbox, None
    if not posted_by_me:
        raise _err(422, "VALIDATION_FAILED", "name inbox= or posted_by_me=1")
    subject = _caller_subject(from_subject)
    if not subject:
        raise _err(422, "VALIDATION_FAILED", "no caller subject; name from_subject in dev mode")
    return task_wakeups.poster_key(org, subject), None, subject


@watch_router.get("/v1/task/watch")
async def task_watch(
    request: Request,
    org: str,
    inbox: str = "",
    posted_by_me: int = 0,
    since_seq: int = 0,
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
    max_seconds: int = DEFAULT_MAX_SECONDS,
    from_subject: str = "",
) -> StreamingResponse:
    """Server-sent events: ``task_events`` rows after a cursor for one inbox
    the caller owns, or for the tasks the caller posted. The principal and
    the scope check are resolved here, before the response exists, so a
    refusal is a plain 403 (an unknown inbox, or one whose owner namespace
    the caller cannot read, a 404) with a JSON detail; the
    generator assumes authorization is done."""
    from server.auth.context import set_current_auth
    from server.core.ch import set_query_tag
    from server.tasks.store import repo as repo

    # Not a registry op, so no transport tags this route's reads; the stream's
    # iteration inherits the tag.
    set_query_tag("task.watch")
    auth = _bearer_auth(request)
    set_current_auth(auth)
    key, inbox_filter, subject = await _resolve_key(
        org, inbox, bool(posted_by_me), from_subject, auth
    )

    async def read(cursor: int) -> list[TaskEventRow]:
        return await repo.events_since(
            org,
            since_seq=cursor,
            lag_s=task_cursors.LAG_SECONDS,
            inbox=inbox_filter,
            from_subject=subject,
            limit=READ_LIMIT,
        )

    frames = event_stream(
        key=key,
        since_seq=_cursor_from(request, since_seq),
        heartbeat_seconds=_clamp(heartbeat_seconds, low=1, high=MAX_HEARTBEAT_SECONDS),
        max_seconds=_clamp(max_seconds, low=1, high=MAX_MAX_SECONDS),
        read=read,
    )
    return StreamingResponse(frames, media_type="text/event-stream", headers=dict(SSE_HEADERS))


def attach(app: Any) -> None:
    app.include_router(watch_router)
