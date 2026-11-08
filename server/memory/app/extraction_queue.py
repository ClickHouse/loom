"""Deferred extraction queue for `memory.set_from_messages`.

The inline path pays LLM extraction + embedding on the request (p50 around
11–13 s). `defer: true` stores the raw request here in one insert and
returns; `server/memory/workers/extraction_queue.py` replays it through the same
handler later, so the deferred write lands identical memories — evidence
rows, conflict surfacing, blacklist, chunk projections — only later.

Row lifecycle on `loom.memory_extraction_queue` (ReplacingMergeTree(version)):
Worker transitions mirror full rows; explicit retries update failed versions in place.

    pending --claim--> running --done--> done
                          |--error--> pending (attempts+1, backoff) | failed (>= max)
                          |--unreachable--> pending (backoff, no attempt spent)
                          \\--held--> held (until the hold ends, no attempt spent)
    failed --retry_mine--> pending (attempts = max-1, in place, same version)

`lease_until` is when a row may be claimed (again): a `running` row whose lease
has passed is reclaimable, so a worker that died mid-extraction does not strand
the batch; a failed attempt waits 15 s, 1, 5, then 30 minutes; a `held` row (its
org switched off, or its identity refused, asked to wait or unread) waits for its
hold, stored as a running lease for older workers. The enqueuer's other requests
in that namespace and for that user are not claimed either. A request still waiting a day before the
table's 30-day TTL is dropped with a memory_drops record, never removed unseen.
Extraction is idempotent under the memory plane's hash dedup, so a rare double
replay writes no second copy.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, NamedTuple

from server.core.ch_names import split_table
from server.core.datetimes import UNIX_EPOCH
from server.core.datetimes import utc_now as _now
from server.core.errors import stored_caller_text
from server.identity.domain.read_scope import (
    READ_SCOPE_KEY,
    ReadFilters,
    replayed_read_scope,
    scope_and,
    stored_read_scope,
)
from server.llm import customer
from server.memory.api.models_bulk import MemorySetFromMessagesInput
from server.memory.store.rows import next_version
from server.settings import settings

TABLE = "loom.memory_extraction_queue"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_HELD = "held"  # reported state; stored as a running lease with a held reason
STATUS_DONE = "done"
STATUS_FAILED = "failed"
# The labels a `last_error` leads with: a held row (`claim_batch` and `status_of` match
# on it), an unreachable provider, and a request dropped unrun.
HELD_LABEL = "held: "
UNREACHABLE_LABEL = "unreachable: "
DROPPED_LABEL = "dropped: "
_NEVER_RUN = "never run"
_EARLIER_ERROR = "an earlier error"
# Not yet extracted: what `pending_count` counts and the TTL drop looks at.
WAITING = (STATUS_PENDING, STATUS_RUNNING)

# The wait before the next claim after a failed attempt, by attempts so far (the last
# repeats); after the model provider or gateway could not be reached, the wait the
# next attempt would have, no attempt spent.
_BACKOFF_S = (15, 60, 300, 1800)  # not a setting: 15 s, a minute, 5 minutes, 30 minutes
# The table's TTL (`enqueued_at + 30 days`), and a waiting request's last day in it,
# when it is dropped with a record instead of removed unseen.
TTL_DAYS = 30  # not a setting: the table's DDL (schemas/schema.sql) sets it
_LAST_DAY = TTL_DAYS - 1  # not a setting: follows the table's TTL
_DROP_PER_PASS = 500  # not a setting: the rest are dropped on the next passes

# Who queued a row: N-1 stores the enqueuer only in the payload; new rows use the column.
_ENQUEUER = "if(enqueued_by != '', enqueued_by, JSONExtractString(payload, 'enqueued_by'))"

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
    "enqueued_by",
)

_SYNC = {"async_insert": 0, "wait_for_async_insert": 1}


# The token-stamped principal (`kind:subject`) rides the replay payload and the
# claim's own column. `rebuild_input` strips server metadata before validation.
ENQUEUED_BY_KEY = "enqueued_by"
# The agent a filed assistant rule is for, resolved from the enqueuing token
# (`agent_rules.request_rule_agent`), so the replay files as the inline
# call would; '' (or a payload without it) files nothing.
RULE_AGENT_KEY = "rule_agent"


def payload_for(
    input: Any, *, scope: ReadFilters, enqueued_by: str | None = None, rule_agent: str = ""
) -> tuple[str, int, int]:
    """Serialize a MemorySetFromMessagesInput for storage: the request minus
    `defer` (the replay must run inline), plus the enqueuing principal
    (`enqueued_by`, else the current request's `writer_actor()`) and the
    request's resolved read `scope` and the agent a filed rule is for
    (`rule_agent`, '' files nothing). Returns (json, messages, chars)."""
    from server.memory.app.provenance import writer_actor

    data = input.model_dump(mode="json", exclude={"defer"})
    msgs = data.get("messages") or []
    chars = sum(len(str(m.get("content", ""))) for m in msgs)
    actor = writer_actor() if enqueued_by is None else str(enqueued_by)
    if actor:
        data[ENQUEUED_BY_KEY] = actor
    data[RULE_AGENT_KEY] = rule_agent
    data[READ_SCOPE_KEY] = stored_read_scope(scope)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")), len(msgs), chars


def enqueued_actor(row: dict[str, Any]) -> str:
    """The principal stored on a queue row's payload, '' when it names none
    (a row enqueued by a dev-mode server)."""
    try:
        return str(json.loads(row.get("payload") or "{}").get(ENQUEUED_BY_KEY) or "")
    except (ValueError, TypeError, AttributeError):
        return ""


async def enqueue(
    input: Any, *, scope: ReadFilters, rule_agent: str = "", ch: Any | None = None
) -> str:
    """Persist one deferred set_from_messages request, with the agent a rule it
    files is for ('' files nothing). Returns the queue_id the enqueuing principal
    polls with `memory.extraction_status_mine` and replays, once failed, with
    `memory.extraction_retry_mine`; `memory.extraction_status` reports it too."""
    from server.core.ch import get_client

    ch = ch or get_client()
    payload, n_msgs, chars = payload_for(input, scope=scope, rule_agent=rule_agent)
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
        "lease_until": UNIX_EPOCH,
        "last_error": "",
        "written": 0,
        "enqueued_at": now,
        "updated_at": now,
        "version": next_version(),
        "enqueued_by": str(json.loads(payload).get(ENQUEUED_BY_KEY) or ""),
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
        out["lease_until"] = UNIX_EPOCH
    for c in ("attempts", "written", "message_count", "content_chars"):
        if out.get(c) is None:
            out[c] = 0
    for c in ("last_error", "payload", "enqueued_by"):
        if out.get(c) is None:
            out[c] = ""
    return out


def backoff_s(attempts: int) -> int:
    """The wait before the claim after the `attempts`-th failed attempt."""
    return _BACKOFF_S[min(max(attempts, 1), len(_BACKOFF_S)) - 1]


async def claim_batch(
    *,
    limit: int,
    lease_seconds: int,
    ch: Any | None = None,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    exclude_orgs: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The oldest claimable rows (pending, lease-expired running, or held past
    their hold, each past its `lease_until`), rewritten `running` with a fresh
    lease. Returns the claimed rows (post-rewrite projection). `exclude_orgs`
    (orgs whose model calls are switched off) leaves their rows unclaimed, and so
    does a request held now its enqueuer's other requests in that namespace and
    for that user: the batch is other work, never rows that would only be held
    again.

    FINAL collapses the RMT so a row already marked done by another replica is
    not seen as pending. The claim write itself is not atomic across replicas;
    the lease + hash-dedup downstream make a double claim wasteful, not wrong.
    """
    from server.core.ch import get_client

    ch = ch or get_client()
    scope = ""
    params: dict[str, Any] = {
        "waiting": list(WAITING),
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
        WHERE status IN {{waiting:Array(String)}} AND lease_until < now64(3)
          -- an enqueuer, namespace and user with a request held now: none claimed
          AND (org, namespace, user_id, {_ENQUEUER}) NOT IN (
              SELECT org, namespace, user_id, {_ENQUEUER}
              FROM {TABLE} FINAL
              WHERE status = {{running:String}}
                AND startsWith(last_error, 'held: ') AND lease_until >= now64(3)
          )
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
        [_mirror(row, status=STATUS_DONE, written=int(written), lease_until=UNIX_EPOCH)],
        settings=_SYNC,
    )


async def mark_error(
    row: dict[str, Any],
    *,
    error: str,
    max_attempts: int,
    ch: Any | None = None,
) -> str:
    """Record a failed attempt. Back to `pending` while attempts remain, claimed
    again after `backoff_s(attempts)`; `failed` once `max_attempts` is reached.
    Returns the new status."""
    from server.core.ch import get_client

    ch = ch or get_client()
    attempts = int(row.get("attempts") or 0) + 1
    failed = attempts >= max(1, int(max_attempts))
    await ch.insert(
        TABLE,
        [
            _mirror(
                row,
                status=STATUS_FAILED if failed else STATUS_PENDING,
                attempts=attempts,
                last_error=str(error)[:2000],
                lease_until=UNIX_EPOCH
                if failed
                else _now() + timedelta(seconds=backoff_s(attempts)),
            )
        ],
        settings=_SYNC,
    )
    return STATUS_FAILED if failed else STATUS_PENDING


async def mark_unreachable(row: dict[str, Any], *, error: str, ch: Any | None = None) -> None:
    """The model provider or gateway could not be reached: back to `pending` with
    no attempt spent, claimed again after the wait the next attempt would have."""
    from server.core.ch import get_client

    ch = ch or get_client()
    wait = backoff_s(int(row.get("attempts") or 0) + 1)
    await ch.insert(
        TABLE,
        [
            _mirror(
                row,
                status=STATUS_PENDING,
                last_error=f"{UNREACHABLE_LABEL}{error}"[:2000],
                lease_until=_now() + timedelta(seconds=wait),
            )
        ],
        settings=_SYNC,
    )


async def mark_held(
    row: dict[str, Any], *, reason: str, seconds: float, ch: Any | None = None
) -> None:
    """The replay's model call was held (its org switched off, its identity refused,
    asked to wait or unread): `held` with the reason, no attempt spent, claimable
    once the hold ends; meanwhile no other request of its enqueuer, namespace and
    user is claimed (`claim_batch`)."""
    from server.core.ch import get_client

    ch = ch or get_client()
    await ch.insert(
        TABLE,
        [
            _mirror(
                row,
                status=STATUS_RUNNING,
                last_error=f"{HELD_LABEL}{reason}"[:2000],
                lease_until=_now() + timedelta(seconds=max(0.0, float(seconds))),
                # a row queued before the column existed carries it in its payload
                enqueued_by=str(row.get("enqueued_by") or "") or enqueued_actor(row),
            )
        ],
        settings=_SYNC,
    )


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
        "waiting": list(WAITING),
        "orgs": sorted(orgs),
        "days": int(older_than_days),
        "lim": settings.model_calls_off_drop_per_pass,
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
        WHERE status IN {{waiting:Array(String)}}
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
    why = f"{DROPPED_LABEL}model calls switched off, waited over {older_than_days} days"
    await ch.insert(
        TABLE,
        [
            _mirror(dict(r), status=STATUS_FAILED, last_error=why, lease_until=UNIX_EPOCH)
            for r in rows
        ],
        settings=_SYNC,
    )
    return len(rows)


async def drop_expiring(
    *,
    ch: Any | None = None,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
) -> int:
    """Drop the requests still waiting (pending, running or held) on their last day
    before the table's TTL would remove them, each with a memory_drops record
    (reason `held_past_ttl` for one held, `waited_past_ttl` for any other) written
    before it is marked `failed`. Returns how many."""
    from server.core.ch import get_client
    from server.memory.app.drops import write_drops

    ch = ch or get_client()
    scope = ""
    params: dict[str, Any] = {"waiting": list(WAITING), "days": _LAST_DAY, "lim": _DROP_PER_PASS}
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
        WHERE status IN {{waiting:Array(String)}}
          AND enqueued_at < now64(3) - toIntervalDay({{days:UInt32}})
          {scope}
        ORDER BY enqueued_at ASC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    if not rows:
        return 0
    for reason, held in (("held_past_ttl", True), ("waited_past_ttl", False)):
        items = [
            (str(r["org"]), str(r["namespace"]), str(r.get("queue_id") or ""))
            for r in rows
            if (status_of(r) == STATUS_HELD) is held
        ]
        if items:
            await write_drops(items, "extraction_queue", reason, ch=ch)
    why = (
        f"{DROPPED_LABEL}still waiting after {_LAST_DAY} days, a day before the {TTL_DAYS}-day TTL"
    )
    await ch.insert(
        TABLE,
        [
            _mirror(
                dict(r),
                status=STATUS_FAILED,
                last_error=f"{why} ({_prior_error(str(r.get('last_error') or ''))})"[:2000],
                lease_until=UNIX_EPOCH,
            )
            for r in rows
        ],
        settings=_SYNC,
    )
    return len(rows)


def _prior_error(last_error: str) -> str:
    """A dropped row's earlier `last_error` as its `dropped: ...` reason quotes it."""
    if not last_error:
        return _NEVER_RUN
    return caller_last_error(last_error) or _EARLIER_ERROR


def caller_last_error(last_error: str) -> str:
    """A batch's `last_error` as `memory.extraction_status(_mine)` shows it: the
    `held: `, `unreachable: ` or `dropped: ` label and `caller_text`'s shape. A build
    before that rule stored the error's text, which can carry a statement or a server
    address; it is cut here on every read."""
    if last_error.startswith(DROPPED_LABEL):
        why, sep, prior = last_error.partition(" (")
        if not sep:
            return why
        prior = prior.removesuffix(")")
        if prior == _NEVER_RUN:
            return f"{why} ({_NEVER_RUN})"
        return f"{why} ({caller_last_error(prior) or _EARLIER_ERROR})"
    for label in (HELD_LABEL, UNREACHABLE_LABEL):
        if last_error.startswith(label):
            error = stored_caller_text(last_error.removeprefix(label))
            if error:
                return f"{label}{error}"
            return label.removesuffix(": ")
    return stored_caller_text(last_error)


async def pending_count(
    *,
    org: str,
    namespace: str,
    agent: str | None = None,
    user_id: str | None = None,
    ch: Any | None = None,
) -> int:
    """Batches not yet extracted (pending, running or held) in the scope a reader
    is searching. `agent="*"` / None and `user_id` None widen the scope the
    way memory.search does."""
    from server.core.ch import get_client

    ch = ch or get_client()
    scope = ""
    params: dict[str, Any] = {"org": org, "ns": namespace, "waiting": list(WAITING)}
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
          AND status IN {{waiting:Array(String)}}
          {scope}
        """,
        params=params,
    )
    return int((row or {}).get("n") or 0)


# What a status read reports of a batch: its state, never its payload.
_STATUS_COLUMNS = (
    "queue_id, agent, user_id, session_id, status, attempts, message_count, content_chars, "
    "written, last_error, enqueued_at, updated_at"
)


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
        params["st"] = STATUS_RUNNING if status == STATUS_HELD else status
        if status in (STATUS_HELD, STATUS_RUNNING):
            where += " AND startsWith(last_error, 'held: ') = {held:Bool}"
            params["held"] = status == STATUS_HELD
    where += scope_and(scope, params)
    rows = await ch.query_all(
        f"""
        SELECT {_STATUS_COLUMNS}
        FROM {TABLE} FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          {where}
        ORDER BY enqueued_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return [_shown(r) for r in rows]


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    """A status row as a reader gets it: its exposed status and its cut `last_error`."""
    return {
        **row,
        "status": status_of(row),
        "last_error": caller_last_error(str(row.get("last_error") or "")),
    }


def status_of(row: dict[str, Any]) -> str:
    """Expose held work while storing a running lease that older workers understand."""
    if row["status"] == STATUS_RUNNING and str(row.get("last_error") or "").startswith(HELD_LABEL):
        return STATUS_HELD
    return str(row["status"])


async def _own_rows(
    ch: Any,
    columns: str,
    *,
    org: str,
    namespace: str,
    queue_ids: list[str],
    enqueued_by: str,
    scope: ReadFilters,
) -> list[dict[str, Any]]:
    """Read the requested batches in the caller's principal and read scope."""
    from server.core.ch import own_write_reads

    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "qids": list(queue_ids),
        "by": enqueued_by,
        "lim": len(queue_ids),
    }
    in_scope = scope_and(scope, params)
    # PREWHERE runs before FINAL: only sort-key columns here, so every version of a batch
    # filters alike, and the payload the enqueuer may be read from is read for the asked
    # ids only.
    with own_write_reads():
        rows = await ch.query_all(
            f"""
            SELECT {columns}
            FROM {TABLE} FINAL
            PREWHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND queue_id IN {{qids:Array(String)}}{in_scope}
            WHERE {_ENQUEUER} = {{by:String}}
            ORDER BY enqueued_at DESC
            LIMIT {{lim:UInt32}}
            """,
            params=params,
        )
    return [dict(r) for r in rows]


async def list_own_status(
    *,
    org: str,
    namespace: str,
    queue_ids: list[str],
    enqueued_by: str,
    scope: ReadFilters,
    ch: Any | None = None,
) -> list[dict[str, Any]]:
    """The batches among `queue_ids` that `enqueued_by` queued in (org, namespace),
    inside `scope`, newest first, payload excluded: `memory.extraction_status_mine`."""
    from server.core.ch import get_client

    rows = await _own_rows(
        ch or get_client(),
        _STATUS_COLUMNS,
        org=org,
        namespace=namespace,
        queue_ids=queue_ids,
        enqueued_by=enqueued_by,
        scope=scope,
    )
    return [_shown(r) for r in rows]


class Requeued(NamedTuple):
    """What `requeue_own_failed` did with each asked id the principal queued, in the
    order asked."""

    # Accepted for an asynchronous transition to `pending`.
    requeued: list[str]
    # Not `failed` (waiting or done): left alone.
    not_failed: list[str]
    # `failed`, but the org's model calls are switched off: left `failed`.
    model_calls_off: list[str]
    # `failed` and queued over `_LAST_DAY` days ago: `drop_expiring` would drop a retry.
    expired: list[str]


# Names the retry's mutation in system.mutations, so a repeated retry finds it.
_RETRY_TAG = "extraction_retry"


async def requeue_own_failed(
    *,
    org: str,
    namespace: str,
    queue_ids: list[str],
    enqueued_by: str,
    scope: ReadFilters,
    max_attempts: int,
    ch: Any | None = None,
) -> Requeued:
    """Submit an in-place retry with one attempt left; status changes asynchronously.
    A batch whose earlier retry is still unapplied is answered `requeued` again, with
    no second mutation."""
    from server.core.ch import get_client

    ch = ch or get_client()
    rows = await _own_rows(
        ch,
        "queue_id, agent, user_id, session_id, status, version, "
        # drop_expiring's cutoff, on the same clock.
        f"enqueued_at < now64(3) - toIntervalDay({_LAST_DAY}) AS past_last_day",
        org=org,
        namespace=namespace,
        queue_ids=queue_ids,
        enqueued_by=enqueued_by,
        scope=scope,
    )
    states = {str(r["queue_id"]): r for r in rows}
    failed = [qid for qid in queue_ids if states.get(qid, {}).get("status") == STATUS_FAILED]
    waiting_or_done = [qid for qid in queue_ids if qid in states and qid not in failed]
    expired = [qid for qid in failed if int(states[qid]["past_last_day"])]
    retryable = [qid for qid in failed if qid not in expired]
    # An earlier retry still to apply goes pending whatever the switch says now.
    submitted = (
        await _retries_unapplied(ch, [(q, int(states[q]["version"])) for q in retryable])
        if retryable
        else set()
    )
    fresh = [q for q in retryable if q not in submitted]
    off = fresh if fresh and await customer.switched_off(org) else []
    if fresh and not off:
        to_submit = [states[q] for q in fresh]
        await _submit_retry(ch, to_submit, org, namespace, enqueued_by, scope, max_attempts)
    return Requeued(
        requeued=[q for q in retryable if q not in off],
        not_failed=waiting_or_done,
        model_calls_off=off,
        expired=expired,
    )


async def _submit_retry(
    ch: Any,
    rows: list[dict[str, Any]],
    org: str,
    namespace: str,
    enqueued_by: str,
    scope: ReadFilters,
    max_attempts: int,
) -> None:
    """The in-place update of `rows`' exact failed versions, not waited for."""
    batches = [
        (r["agent"], r["user_id"], r["session_id"], r["queue_id"], int(r["version"])) for r in rows
    ]
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "batches": batches,
        "by": enqueued_by,
        "attempts": max(0, int(max_attempts) - 1),
        "tag": _RETRY_TAG,
        # Bound, not now64(): a replicated table refuses a non-deterministic mutation.
        "updated_ms": (_now() - UNIX_EPOCH) // timedelta(milliseconds=1),
    }
    in_scope = scope_and(scope, params)
    # Updating stored rows cannot restore a payload deleted while the request was in flight.
    await ch.execute(
        f"""
        ALTER TABLE loom.memory_extraction_queue
        UPDATE status = 'pending', attempts = {{attempts:UInt32}},
               lease_until = toDateTime64(0, 3, 'UTC'),
               updated_at = fromUnixTimestamp64Milli({{updated_ms:Int64}}, 'UTC')
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND ({{tag:String}} = {{tag:String}})
          AND (agent, user_id, session_id, queue_id, version)
              IN {{batches:Array(Tuple(String, String, String, String, UInt64))}}
          AND status = 'failed'
          AND {_ENQUEUER} = {{by:String}}
          {in_scope}
        SETTINGS mutations_sync = 0
        """,
        params=params,
    )


async def _retries_unapplied(ch: Any, versions: list[tuple[str, int]]) -> set[str]:
    """The ids among `versions` whose failed version an earlier retry's unfinished
    mutation targets: the command keeps each batch tuple, ending `'<id>', <version>)`."""
    db, name = split_table(TABLE)
    rows = await ch.query_all(
        """
        SELECT DISTINCT b.1 AS qid
        FROM system.mutations
        ARRAY JOIN {versions:Array(Tuple(String, UInt64))} AS b
        WHERE database = {db:String} AND table = {t:String} AND is_done = 0
          AND position(command, {pat:String}) > 0
          AND position(command, concat(char(39), b.1, char(39), ', ', toString(b.2), ')')) > 0
        """,
        params={"db": db, "t": name, "versions": versions, "pat": f"'{_RETRY_TAG}'"},
    )
    return {str(r["qid"]) for r in rows}


__all__ = [
    "COLUMNS",
    "ENQUEUED_BY_KEY",
    "READ_SCOPE_KEY",
    "RULE_AGENT_KEY",
    "Requeued",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_HELD",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "TABLE",
    "TTL_DAYS",
    "WAITING",
    "backoff_s",
    "claim_batch",
    "drop_expiring",
    "enqueue",
    "enqueued_actor",
    "list_own_status",
    "list_status",
    "mark_done",
    "mark_error",
    "mark_held",
    "mark_unreachable",
    "payload_for",
    "pending_count",
    "requeue_own_failed",
]


def rebuild_input(row: dict[str, Any]) -> Any:
    """The MemorySetFromMessagesInput stored at enqueue time, forced inline."""
    data = json.loads(row["payload"])
    data["defer"] = False
    data.pop(ENQUEUED_BY_KEY, None)  # server stamps, not request fields
    data.pop(READ_SCOPE_KEY, None)
    data.pop(RULE_AGENT_KEY, None)
    return MemorySetFromMessagesInput.model_validate(data)


def replay_actor(row: dict[str, Any]) -> str:
    """The principal the replay writes as: the enqueuer the
    payload names, else the worker's own `process:extraction-worker`. Never
    empty — the worker has no auth context of its own."""
    from server.memory.domain import provenance as _prov

    return enqueued_actor(row) or _prov.EXTRACTION_WORKER_ACTOR


def queued_rule_agent(row: dict[str, Any]) -> str:
    """The rule agent stored at enqueue time; '' (nothing is filed) for a row
    that stored none."""
    return str(json.loads(row["payload"]).get(RULE_AGENT_KEY) or "")


async def replay_row(row: dict[str, Any]) -> int:
    """Run one queued request through the inline handler under the enqueuer's
    principal and read scope; returns memories written."""
    from server.identity.domain.read_scope import read_filters_bound
    from server.memory.app.extract.handler import handle_memory_set_from_messages
    from server.memory.domain import provenance as _prov
    from server.memory.domain.trust import subject_of

    # The worker has no auth bound; replay the stored scope and name the enqueuer
    # for every model call.
    scope = replayed_read_scope(json.loads(row["payload"]).get(READ_SCOPE_KEY))
    with (
        _prov.pipeline_actor(replay_actor(row)),
        read_filters_bound(None, scope),
        customer.for_org(
            str(row.get("org") or ""),
            namespace=str(row.get("namespace") or ""),
            subject=subject_of(enqueued_actor(row)),
            user_id=str(row.get("user_id") or ""),
        ),
    ):
        out = await handle_memory_set_from_messages(
            rebuild_input(row), rule_agent=queued_rule_agent(row)
        )
    return len(out.written)
