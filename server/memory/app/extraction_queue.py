"""Deferred extraction queue for `memory.set_from_messages`.

The inline path pays LLM extraction + embedding on the request (p50 around
11–13 s). `defer: true` stores the raw request here in one insert and
returns; `server/memory/workers/extraction_queue.py` replays it through the same
handler later, so the deferred write lands identical memories — evidence
rows, conflict surfacing, blacklist, chunk projections — only later.

Row lifecycle on `loom.memory_extraction_queue` (ReplacingMergeTree(version),
every transition is a full mirrored row):

    pending --claim--> running --done--> done
                          \\--error--> pending (attempts+1)  | failed (>= max)

A `running` row whose `lease_until` has passed is reclaimable: a worker that
died mid-extraction does not strand the batch. Extraction is idempotent under
the memory plane's hash dedup, so a rare double replay writes no second copy.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from server.core.datetimes import utc_now as _now
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters, scope_and
from server.llm import customer
from server.memory.api.models_bulk import MemorySetFromMessagesInput
from server.memory.store.rows import next_version

TABLE = "loom.memory_extraction_queue"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Columns of the queue table, in insert order. A new column belongs here AND in
# `_mirror` — the RMT contract is the same as loom.memory's.
COLUMNS = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "queue_id",
    "payload",
    "message_count",
    "content_chars",
    "status",
    "attempts",
    "lease_until",
    "last_error",
    "written",
    "enqueued_at",
    "updated_at",
    "version",
)

_SYNC = {"async_insert": 0, "wait_for_async_insert": 1}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# Server-stamped key beside the request fields in `payload`:
# the enqueuing principal as `kind:subject`, decided by the token at enqueue
# time (never read from the body). It rides the payload rather than a column
# for the same reason `observation_date` does: the queue table needs no
# migration. `rebuild_input` strips it before validation.
ENQUEUED_BY_KEY = "enqueued_by"
# The request's resolved read scope, so the replay's lookups stay inside it as
# the inline call's would. Stamped by the server like `enqueued_by`.
READ_SCOPE_KEY = "read_filters"
# The agent a filed assistant rule is for, resolved from the enqueuing token
# (`assistant_learnings.request_learning_agent`), so the replay files as the inline
# call would; '' (or a payload without it) files nothing.
LEARNING_AGENT_KEY = "learning_agent"


def payload_for(
    input: Any, *, scope: ReadFilters, enqueued_by: str | None = None, learning_agent: str = ""
) -> tuple[str, int, int]:
    """Serialize a MemorySetFromMessagesInput for storage: the request minus
    `defer` (the replay must run inline), plus the enqueuing principal
    (`enqueued_by`, else the current request's `writer_actor()`) and the
    request's resolved read `scope`. Returns (json, messages, chars)."""
    from server.memory.app.provenance import writer_actor

    data = input.model_dump(mode="json", exclude={"defer"})
    msgs = data.get("messages") or []
    chars = sum(len(str(m.get("content", ""))) for m in msgs)
    actor = writer_actor() if enqueued_by is None else str(enqueued_by)
    if actor:
        data[ENQUEUED_BY_KEY] = actor
    data[LEARNING_AGENT_KEY] = learning_agent
    data[READ_SCOPE_KEY] = {
        "scope": scope.scope,
        "user_id": scope.user_id,
        "session_id": scope.session_id,
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")), len(msgs), chars


def enqueued_actor(row: dict[str, Any]) -> str:
    """The principal stored on a queue row's payload, '' when it names none
    (a row enqueued by a dev-mode server)."""
    try:
        return str(json.loads(row.get("payload") or "{}").get(ENQUEUED_BY_KEY) or "")
    except (ValueError, TypeError, AttributeError):
        return ""


async def enqueue(
    input: Any, *, scope: ReadFilters, learning_agent: str = "", ch: Any | None = None
) -> str:
    """Persist one deferred set_from_messages request, with the agent a rule it
    files is for ('' files nothing). Returns the queue_id the caller can poll with
    `memory.extraction_status`."""
    from server.core.ch import get_client

    ch = ch or get_client()
    payload, n_msgs, chars = payload_for(input, scope=scope, learning_agent=learning_agent)
    now = _now()
    qid = uuid.uuid4().hex
    row = {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "user_id": input.user_id,
        "session_id": input.session_id,
        "queue_id": qid,
        "payload": payload,
        "message_count": n_msgs,
        "content_chars": chars,
        "status": STATUS_PENDING,
        "attempts": 0,
        "lease_until": _EPOCH,
        "last_error": "",
        "written": 0,
        "enqueued_at": now,
        "updated_at": now,
        "version": next_version(),
    }
    await ch.insert(TABLE, [row], settings=_SYNC)
    return qid


def _mirror(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A new version of `row` with `changes` applied and every column carried
    forward — omitting one zeros it for readers under FINAL."""
    out = {c: row.get(c) for c in COLUMNS}
    out.update(changes)
    out["updated_at"] = _now()
    out["version"] = next_version()
    # Defaults for columns a partial projection may have left None.
    if out.get("lease_until") is None:
        out["lease_until"] = _EPOCH
    for c in ("attempts", "written", "message_count", "content_chars"):
        if out.get(c) is None:
            out[c] = 0
    for c in ("last_error", "payload"):
        if out.get(c) is None:
            out[c] = ""
    return out


async def claim_batch(
    *,
    limit: int,
    lease_seconds: int,
    ch: Any | None = None,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    exclude_orgs: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Oldest pending (or lease-expired running) rows, rewritten `running`
    with a fresh lease. Returns the claimed rows (post-rewrite projection).
    `exclude_orgs` (orgs whose model calls are switched off) leaves their rows
    pending and unclaimed.

    FINAL collapses the RMT so a row already marked done by another replica is
    not seen as pending. The claim write itself is not atomic across replicas;
    the lease + hash-dedup downstream make a double claim wasteful, not wrong.
    """
    from server.core.ch import get_client

    ch = ch or get_client()
    scope = ""
    params: dict[str, Any] = {
        "pending": STATUS_PENDING,
        "running": STATUS_RUNNING,
        "lim": int(limit),
    }
    if org_filter is not None:
        scope += " AND org = {org_f:String}"
        params["org_f"] = org_filter
    if namespace_filter is not None:
        scope += " AND namespace = {ns_f:String}"
        params["ns_f"] = namespace_filter
    if exclude_orgs:
        scope += " AND org NOT IN {xo:Array(String)}"
        params["xo"] = sorted(exclude_orgs)
    cols = ", ".join(COLUMNS)
    rows = await ch.query_all(
        f"""
        SELECT {cols}
        FROM {TABLE} FINAL
        WHERE (status = {{pending:String}}
               OR (status = {{running:String}} AND lease_until < now64(3)))
          {scope}
        ORDER BY enqueued_at ASC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    if not rows:
        return []
    lease = _now() + timedelta(seconds=max(1, int(lease_seconds)))
    claimed = [
        _mirror(dict(r), status=STATUS_RUNNING, lease_until=lease, last_error="") for r in rows
    ]
    await ch.insert(TABLE, claimed, settings=_SYNC)
    return claimed


async def mark_done(row: dict[str, Any], *, written: int, ch: Any | None = None) -> None:
    from server.core.ch import get_client

    ch = ch or get_client()
    await ch.insert(
        TABLE,
        [_mirror(row, status=STATUS_DONE, written=int(written), lease_until=_EPOCH)],
        settings=_SYNC,
    )


async def mark_error(
    row: dict[str, Any],
    *,
    error: str,
    max_attempts: int,
    ch: Any | None = None,
) -> str:
    """Record a failed attempt. Back to `pending` while attempts remain,
    `failed` once `max_attempts` is reached. Returns the new status."""
    from server.core.ch import get_client

    ch = ch or get_client()
    attempts = int(row.get("attempts") or 0) + 1
    status = STATUS_FAILED if attempts >= max(1, int(max_attempts)) else STATUS_PENDING
    await ch.insert(
        TABLE,
        [
            _mirror(
                row,
                status=status,
                attempts=attempts,
                last_error=str(error)[:2000],
                lease_until=_EPOCH,
            )
        ],
        settings=_SYNC,
    )
    return status


# How many waiting requests one pass drops: the rest go on the next pass.
_DROP_PER_PASS = 500


async def drop_waiting(
    orgs: frozenset[str],
    *,
    older_than_days: int,
    ch: Any | None = None,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
) -> int:
    """Drop the requests of `orgs` (switched off) that have waited longer than
    `older_than_days`, counted from when the request was queued or the org was
    switched off, whichever is later. Each gets a memory_drops record (reason
    `model_calls_off`), written before the request is marked `failed`: a
    record that cannot be written leaves the request pending. Returns how many."""
    from server.core.ch import get_client
    from server.memory.app.drops import write_drops

    if not orgs:
        return 0
    ch = ch or get_client()
    scope = ""
    params: dict[str, Any] = {
        "pending": STATUS_PENDING,
        "running": STATUS_RUNNING,
        "orgs": sorted(orgs),
        "days": int(older_than_days),
        "lim": _DROP_PER_PASS,
    }
    if org_filter is not None:
        scope += " AND org = {org_f:String}"
        params["org_f"] = org_filter
    if namespace_filter is not None:
        scope += " AND namespace = {ns_f:String}"
        params["ns_f"] = namespace_filter
    cols = ", ".join(COLUMNS)
    rows = await ch.query_all(
        f"""
        SELECT {cols}
        FROM {TABLE} FINAL
        WHERE status IN ({{pending:String}}, {{running:String}})
          AND has({{orgs:Array(String)}}, org)
          AND enqueued_at < now64(3) - toIntervalDay({{days:UInt32}})
          AND org IN (
              SELECT org FROM loom.org_model_calls FINAL
              WHERE enabled = 0 AND updated_at < now64(3) - toIntervalDay({{days:UInt32}})
          )
          {scope}
        ORDER BY enqueued_at ASC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    if not rows:
        return 0
    await write_drops(
        [(str(r["org"]), str(r["namespace"]), str(r.get("queue_id") or "")) for r in rows],
        "extraction_queue",
        "model_calls_off",
        ch=ch,
    )
    why = f"dropped: model calls switched off, waited over {older_than_days} days"
    await ch.insert(
        TABLE,
        [_mirror(dict(r), status=STATUS_FAILED, last_error=why, lease_until=_EPOCH) for r in rows],
        settings=_SYNC,
    )
    return len(rows)


async def pending_count(
    *,
    org: str,
    namespace: str,
    agent: str | None = None,
    user_id: str | None = None,
    ch: Any | None = None,
) -> int:
    """Batches not yet extracted (pending or running) in the scope a reader
    is searching. `agent="*"` / None and `user_id` None widen the scope the
    way memory.search does."""
    from server.core.ch import get_client

    ch = ch or get_client()
    scope = ""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "pending": STATUS_PENDING,
        "running": STATUS_RUNNING,
    }
    if agent and agent != "*":
        scope += " AND agent = {agent:String}"
        params["agent"] = agent
    if user_id and user_id != "-":
        scope += " AND user_id = {uid:String}"
        params["uid"] = user_id
    row = await ch.query_one(
        f"""
        SELECT count() AS n
        FROM {TABLE} FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND status IN ({{pending:String}}, {{running:String}})
          {scope}
        """,
        params=params,
    )
    return int((row or {}).get("n") or 0)


async def list_status(
    *,
    org: str,
    namespace: str,
    scope: ReadFilters,
    agent: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    queue_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    ch: Any | None = None,
) -> list[dict[str, Any]]:
    """Queue rows for `memory.extraction_status`, newest first, payload
    excluded (it is the caller's own transcript; the op reports state)."""
    from server.core.ch import get_client

    ch = ch or get_client()
    where = ""
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": int(limit)}
    if agent and agent != "*":
        where += " AND agent = {agent:String}"
        params["agent"] = agent
    if user_id and user_id != "-":
        where += " AND user_id = {uid:String}"
        params["uid"] = user_id
    if session_id and session_id != "-":
        where += " AND session_id = {sid:String}"
        params["sid"] = session_id
    if queue_id:
        where += " AND queue_id = {qid:String}"
        params["qid"] = queue_id
    if status:
        where += " AND status = {st:String}"
        params["st"] = status
    where += scope_and(scope, params)
    rows = await ch.query_all(
        f"""
        SELECT queue_id, agent, user_id, session_id, status, attempts,
               message_count, content_chars, written, last_error,
               enqueued_at, updated_at
        FROM {TABLE} FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          {where}
        ORDER BY enqueued_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return [dict(r) for r in rows]


__all__ = [
    "COLUMNS",
    "ENQUEUED_BY_KEY",
    "LEARNING_AGENT_KEY",
    "READ_SCOPE_KEY",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "TABLE",
    "claim_batch",
    "enqueue",
    "enqueued_actor",
    "list_status",
    "mark_done",
    "mark_error",
    "payload_for",
    "pending_count",
]


def rebuild_input(row: dict[str, Any]) -> Any:
    """The MemorySetFromMessagesInput stored at enqueue time, forced inline."""
    data = json.loads(row["payload"])
    data["defer"] = False
    data.pop(ENQUEUED_BY_KEY, None)  # server stamps, not request fields
    data.pop(READ_SCOPE_KEY, None)
    data.pop(LEARNING_AGENT_KEY, None)
    return MemorySetFromMessagesInput.model_validate(data)


def replay_scope(row: dict[str, Any]) -> ReadFilters:
    """The read scope the replay runs under: the one stored at enqueue time.
    A row queued before scopes were stored replays over the whole namespace, as
    it always did (no write was scoped then). A stored scope that is not a
    complete one raises, so the row fails rather than replaying wider."""
    stored = json.loads(row["payload"]).get(READ_SCOPE_KEY)
    if stored is None:
        return WHOLE_NAMESPACE
    scope = ReadFilters(
        scope=str(stored.get("scope") or ""),
        user_id=stored.get("user_id"),
        session_id=stored.get("session_id"),
    )
    complete = (
        scope.scope == "namespace"
        or (scope.scope == "own_user" and scope.user_id)
        or (scope.scope == "own_session" and scope.user_id and scope.session_id)
    )
    if not complete:
        raise ValueError(f"queued read scope is incomplete: {stored!r}")
    return scope


def replay_actor(row: dict[str, Any]) -> str:
    """The principal the replay writes as: the enqueuer the
    payload names, else the worker's own `process:extraction-worker`. Never
    empty — the worker has no auth context of its own."""
    from server.memory.domain import provenance as _prov

    return enqueued_actor(row) or _prov.EXTRACTION_WORKER_ACTOR


def queued_learning_agent(row: dict[str, Any]) -> str:
    """The learning agent stored at enqueue time; '' (nothing is filed) for a row
    that stored none."""
    return str(json.loads(row["payload"]).get(LEARNING_AGENT_KEY) or "")


async def replay_row(row: dict[str, Any]) -> int:
    """Run one queued request through the inline handler under the enqueuer's
    principal and read scope; returns memories written."""
    from server.identity.domain.read_scope import read_filters_bound
    from server.memory.app.extract.handler import handle_memory_set_from_messages
    from server.memory.domain import provenance as _prov

    # The worker has no auth bound, so the scope is bound for "no auth".
    with (
        _prov.pipeline_actor(replay_actor(row)),
        read_filters_bound(None, replay_scope(row)),
        customer.for_org(str(row.get("org") or "")),
    ):
        out = await handle_memory_set_from_messages(
            rebuild_input(row), learning_agent=queued_learning_agent(row)
        )
    return len(out.written)
