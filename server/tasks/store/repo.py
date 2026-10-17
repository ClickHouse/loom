"""SQL for the task inbox plane.

Five tables, one shape each:

* ``loom.task_inboxes`` / ``loom.task_poster_policies`` — ReplacingMergeTree
  by ``version``; a write is a new version row carrying every column, a
  read is ``FINAL … is_deleted = 0`` under ``select_sequential_consistency``
  (an admin registers an inbox and sets its policy in the next request; on
  the Cloud service the second read can land on a replica without the row).
* ``loom.tasks`` — the current row per task, same discipline.
* ``loom.task_events`` — append-only; the audit trail, the activity feed and
  the cursor senders read.
* ``loom.task_leases`` (KeeperMap) — where a claim is arbitrated:
  :class:`KeeperLeases` is a compare-and-swap under ``keeper_map_strict_mode``.

Handlers own the state machine; this module owns the statements.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from server.core.grant_sql import LIVE_GRANT
from server.core.repo_base import _CHClient, _default_client
from server.tasks.store.rows import (
    GrantSubjectRow,
    InboxCountRow,
    InboxRow,
    PolicyRow,
    ServiceAccountRow,
    StaleTriageRow,
    SweepCandidates,
    TaskEventRow,
    TaskRow,
    WebhookCursorRow,
    WebhookInboxRow,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- columns

INBOX_COLS = (
    "org, inbox, owner_agent, owner_namespace, description, posters, "
    "accept_disclosure, default_lease_seconds, max_attempts, max_defers, "
    "triage_escalate_after_seconds, webhook_url, webhook_secret_ref, "
    "created_by, created_at, updated_at, version, is_deleted"
)

POLICY_COLS = (
    "org, inbox, poster, kind, mode, daily_cap, require_evidence, promote_after, "
    "require_human, streak, set_by, set_at, version, is_deleted"
)

TASK_COLS = (
    "org, inbox, namespace, task_id, kind, priority, disclosure, status, reason, "
    "blocked_on, not_before, resume_at, deadline, defer_count, title, brief, "
    "from_agent, from_subject, origin, parent_task_id, idempotency_key, owner, "
    "attempt, fence, lease_until, outcome, last_actor, created_at, updated_at, "
    "version, is_deleted"
)

EVENT_COLS = (
    "org, inbox, namespace, task_id, seq, event, from_status, to_status, actor, "
    "agent, from_subject, fence, version, reason, patch, note, ts"
)

TASK_FIELDS: tuple[str, ...] = tuple(c.strip() for c in TASK_COLS.split(","))

_SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def is_uuid(value: Any) -> bool:
    """A task id the statements can bind as ``{tid:UUID}``; anything else is
    "no such task", never a parse error out of ClickHouse (a well-formed body
    with a made-up id must read as not found, never as a 5xx)."""
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def next_version() -> UInt64:
    """A version / sequence number: microseconds since the epoch, strictly
    increasing inside one process so two writes in the same tick still order.
    Microseconds, not nanoseconds: the value rides the wire as a JSON number
    and a JavaScript client rounds anything above 2**53 (a nanosecond stamp
    is 1.8e18; a microsecond one stays below 2**53 until the year 2255), so
    the dashboard's ``if_match_version`` must survive JSON.parse intact."""
    global _last_version
    v = time.time_ns() // 1000
    if v <= _last_version:
        v = _last_version + 1
    _last_version = v
    return v


async def server_version(floor: int = 0, *, ch: _CHClient | None = None) -> UInt64:
    """A task version / event ``seq`` from the ClickHouse server's clock, in
    ``next_version``'s microseconds, above ``floor`` (the row's current
    version, so the new row wins under FINAL) and every one this process
    stamped. Event cursors order by it: replicas whose clocks disagree would
    otherwise stamp a later event below a cursor that already passed it."""
    global _last_server_version
    ch = ch or _default_client()
    row = await ch.query_one("SELECT toUnixTimestamp64Micro(now64(6)) AS v")
    v = max(int((row or {})["v"]), int(floor) + 1, _last_server_version + 1)
    _last_server_version = v
    return v


# JSON numbers a JavaScript client keeps exact.
JS_SAFE_INTEGER = 2**53 - 1


UInt64 = int
_last_version: int = 0
_last_server_version: int = 0


def _projection(fields: list[str] | None, all_cols: str, allowed: tuple[str, ...]) -> str:
    """The SELECT list a caller asked for (a list reads only the columns its
    rows show), or every column. Unknown names are dropped rather than
    interpolated; identity columns always ride along."""
    if not fields:
        return all_cols
    keep = [f for f in fields if f in allowed and _SAFE_IDENT.match(f)]
    for must in ("org", "inbox", "task_id", "status", "version"):
        if must in allowed and must not in keep:
            keep.append(must)
    return ", ".join(keep) if keep else all_cols


# ---------------------------------------------------------------- inboxes


async def fetch_inbox_row(org: str, inbox: str, *, ch: _CHClient | None = None) -> InboxRow | None:
    ch = ch or _default_client()
    return cast(
        "InboxRow | None",
        await ch.query_one(
            f"SELECT {INBOX_COLS} FROM loom.task_inboxes FINAL "
            "WHERE org = {org:String} AND inbox = {inbox:String} AND is_deleted = 0 LIMIT 1 "
            "SETTINGS select_sequential_consistency = 1",
            params={"org": org, "inbox": inbox},
        ),
    )


async def fetch_inbox(
    org: str, inbox: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """The inbox row as a plain dict, for readers outside the tasks package."""
    row = await fetch_inbox_row(org, inbox, ch=ch)
    return dict(row) if row is not None else None


async def list_inboxes(
    org: str,
    *,
    namespace: str | None = None,
    owner_agent: str | None = None,
    limit: int = 200,
    ch: _CHClient | None = None,
) -> list[InboxRow]:
    ch = ch or _default_client()
    where = ["org = {org:String}", "is_deleted = 0"]
    params: dict[str, Any] = {"org": org, "limit": int(limit)}
    if namespace:
        where.append("owner_namespace = {ns:String}")
        params["ns"] = namespace
    if owner_agent:
        where.append("owner_agent = {agent:String}")
        params["agent"] = owner_agent
    return cast(
        "list[InboxRow]",
        await ch.query_all(
            f"SELECT {INBOX_COLS} FROM loom.task_inboxes FINAL WHERE {' AND '.join(where)} "
            "ORDER BY inbox LIMIT {limit:UInt32} SETTINGS select_sequential_consistency = 1",
            params=params,
        ),
    )


async def insert_inbox_version(row: InboxRow, *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(
        "loom.task_inboxes", [dict(row)], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )


# ---------------------------------------------------------------- principals


async def list_service_accounts(
    org: str, *, ch: _CHClient | None = None
) -> list[ServiceAccountRow]:
    """Live service accounts of the org: the bots a poster picker can name."""
    ch = ch or _default_client()
    return cast(
        "list[ServiceAccountRow]",
        await ch.query_all(
            "SELECT name, sa_subject, namespaces, roles, ns_roles, kind, client, created_by, "
            "created_at FROM loom.service_accounts FINAL "
            "WHERE org = {org:String} AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY name",
            params={"org": org},
        ),
    )


async def service_account_creator(org: str, sa_subject: str, *, ch: _CHClient | None = None) -> str:
    """Who created a live service account: the one-hop link
    from a bot's subject to the principal that minted it. "" when unknown,
    revoked or expired."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT created_by FROM loom.service_accounts FINAL "
        "WHERE org = {org:String} AND sa_subject = {sub:String} AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1",
        params={"org": org, "sub": sa_subject},
    )
    return str((row or {}).get("created_by") or "")


async def list_grant_subjects(org: str, *, ch: _CHClient | None = None) -> list[GrantSubjectRow]:
    """Every live grant of the org (the authorization gate's `LIVE_GRANT`):
    (subject, namespace, role) — people appear here under their login subject
    (an email), bots under ``sa:`` names."""
    ch = ch or _default_client()
    return cast(
        "list[GrantSubjectRow]",
        await ch.query_all(
            "SELECT subject, namespace, role FROM loom.namespace_grants FINAL "
            f"WHERE org = {{org:String}} AND {LIVE_GRANT} "
            "ORDER BY subject, namespace",
            params={"org": org},
        ),
    )


async def subjects_last_seen(org: str, *, ch: _CHClient | None = None) -> dict[str, datetime]:
    """When each subject last called loom (the ledger roll-up)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT subject, maxMerge(last_seen) AS last_seen FROM loom.subject_last_seen "
        "WHERE org = {org:String} GROUP BY subject",
        params={"org": org},
    )
    return {str(r["subject"]): r["last_seen"] for r in rows}


# ---------------------------------------------------------------- policies


async def fetch_policies(org: str, inbox: str, *, ch: _CHClient | None = None) -> list[PolicyRow]:
    ch = ch or _default_client()
    return cast(
        "list[PolicyRow]",
        await ch.query_all(
            f"SELECT {POLICY_COLS} FROM loom.task_poster_policies FINAL "
            "WHERE org = {org:String} AND inbox = {inbox:String} AND is_deleted = 0 "
            "ORDER BY poster, kind SETTINGS select_sequential_consistency = 1",
            params={"org": org, "inbox": inbox},
        ),
    )


def resolve_policy(
    policies: list[PolicyRow], *, poster_subject: str, poster_agent: str, kind: str
) -> PolicyRow | None:
    """Most specific row wins: (subject|agent, kind) > (subject|agent, '*') >
    ('*', kind) > ('*', '*'). None when no row applies (the caller decides
    the default — ``review`` for a foreign poster, ``auto`` for the owner)."""
    by_key = {(p["poster"], p["kind"]): p for p in policies}
    for poster in (poster_subject, poster_agent):
        if not poster:
            continue
        for k in (kind, "*"):
            hit = by_key.get((poster, k))
            if hit is not None:
                return hit
    for k in (kind, "*"):
        hit = by_key.get(("*", k))
        if hit is not None:
            return hit
    return None


async def insert_policy_version(row: PolicyRow, *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(
        "loom.task_poster_policies",
        [dict(row)],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


# ---------------------------------------------------------------- tasks


async def fetch_task(org: str, task_id: str, *, ch: _CHClient | None = None) -> TaskRow | None:
    """Id-addressed read. The row is the authority for authorization: the
    handler compares the caller with ``namespace`` / ``from_subject`` after
    this returns and answers ``found = false`` on refusal."""
    if not is_uuid(task_id):
        return None
    ch = ch or _default_client()
    return cast(
        "TaskRow | None",
        await ch.query_one(
            f"SELECT {TASK_COLS} FROM loom.tasks FINAL "
            "WHERE org = {org:String} AND task_id = {tid:UUID} AND is_deleted = 0 "
            "LIMIT 1 SETTINGS select_sequential_consistency = 1",
            params={"org": org, "tid": task_id},
        ),
    )


async def fetch_task_by_idempotency_key(
    org: str, inbox: str, key: str, *, ch: _CHClient | None = None
) -> TaskRow | None:
    ch = ch or _default_client()
    return cast(
        "TaskRow | None",
        await ch.query_one(
            f"SELECT {TASK_COLS} FROM loom.tasks FINAL "
            "WHERE org = {org:String} AND inbox = {inbox:String} "
            "AND idempotency_key = {key:String} AND is_deleted = 0 "
            "ORDER BY created_at, task_id "
            "LIMIT 1 SETTINGS select_sequential_consistency = 1",
            params={"org": org, "inbox": inbox, "key": key},
        ),
    )


async def insert_task_version(
    row: TaskRow, *, dedup_token: str = "", ch: _CHClient | None = None
) -> None:
    """One new version row. Every persisted column rides along (the RMT rule
    in CLAUDE.md); storage drops a second insert with the same ``dedup_token``."""
    ch = ch or _default_client()
    settings: dict[str, Any] = {"async_insert": 0, "wait_for_async_insert": 1}
    if dedup_token:
        settings["insert_deduplication_token"] = dedup_token
    await ch.insert("loom.tasks", [dict(row)], settings=settings)


async def insert_created_task(row: TaskRow, *, ch: _CHClient | None = None) -> TaskRow:
    """Insert a new task that has an idempotency key, and answer the key's task:
    this row when it landed, else the task another create stored first.

    The key's lease keeps a second create out. A create whose lease ran out before
    its insert can still race the next one, so both insert under the key's dedup
    token and storage drops the later row. An erased task's token can outlive it
    and drop this row too: with no task under the key, it goes in under a fresh
    token. The previous build's insert carries a random token whatever its caller
    asked for (its `dedup_settings` overwrote every caller's token with one), so a
    create of each build can both land (docs/architecture/rolling-upgrades.md, rule 10)."""
    org, inbox, key = row["org"], row["inbox"], row["idempotency_key"]
    token = f"task.create:{org}:{inbox}:{key}"
    await insert_task_version(row, dedup_token=token, ch=ch)
    if await fetch_task(org, str(row["task_id"]), ch=ch) is not None:
        return row
    # The token dropped this row.
    stored = await fetch_task_by_idempotency_key(org, inbox, key, ch=ch)
    if stored is not None:
        return stored
    await insert_task_version(row, dedup_token=f"{token}:{uuid.uuid4().hex}", ch=ch)
    return row


async def list_tasks(
    org: str,
    *,
    inbox: str | None = None,
    namespace: str | None = None,
    statuses: list[str] | None = None,
    kinds: list[str] | None = None,
    from_subject: str | None = None,
    owner: str | None = None,
    limit: int = 100,
    fields: list[str] | None = None,
    newest_first: bool = False,
    ch: _CHClient | None = None,
) -> list[TaskRow]:
    ch = ch or _default_client()
    where = ["org = {org:String}", "is_deleted = 0"]
    params: dict[str, Any] = {"org": org, "limit": int(limit)}
    if inbox:
        where.append("inbox = {inbox:String}")
        params["inbox"] = inbox
    if namespace:
        where.append("namespace = {ns:String}")
        params["ns"] = namespace
    if statuses:
        where.append("status IN {statuses:Array(String)}")
        params["statuses"] = list(statuses)
    if kinds:
        where.append("kind IN {kinds:Array(String)}")
        params["kinds"] = list(kinds)
    if from_subject:
        where.append("(from_subject = {subj:String} OR from_agent = {subj:String})")
        params["subj"] = from_subject
    if owner:
        where.append("owner = {owner:String}")
        params["owner"] = owner
    cols = _projection(fields, TASK_COLS, TASK_FIELDS)
    # The work-queue order (priority, oldest first) hides the
    # task just posted behind a LIMIT once old ones pile up; a reader asks
    # for newest first.
    order = "created_at DESC, task_id" if newest_first else "priority ASC, created_at ASC, task_id"
    return cast(
        "list[TaskRow]",
        await ch.query_all(
            f"SELECT {cols} FROM loom.tasks FINAL WHERE {' AND '.join(where)} "
            f"ORDER BY {order} LIMIT {{limit:UInt32}} "
            "SETTINGS select_sequential_consistency = 1",
            params=params,
        ),
    )


async def claimable_candidates(
    org: str,
    inbox: str,
    *,
    kinds: list[str] | None,
    now: datetime,
    limit: int,
    ch: _CHClient | None = None,
) -> list[TaskRow]:
    """``todo`` rows a pull may try to claim: ``not_before`` passed, ordered
    by priority then age. Read with sequential consistency so a task created
    a moment ago on another replica is offered."""
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "inbox = {inbox:String}",
        "is_deleted = 0",
        "status = 'todo'",
        "(not_before IS NULL OR not_before <= {now:DateTime})",
    ]
    params: dict[str, Any] = {"org": org, "inbox": inbox, "now": now, "limit": int(limit)}
    if kinds:
        where.append("kind IN {kinds:Array(String)}")
        params["kinds"] = list(kinds)
    return cast(
        "list[TaskRow]",
        await ch.query_all(
            f"SELECT {TASK_COLS} FROM loom.tasks FINAL WHERE {' AND '.join(where)} "
            "ORDER BY priority ASC, created_at ASC, task_id LIMIT {limit:UInt32} "
            "SETTINGS select_sequential_consistency = 1",
            params=params,
        ),
    )


async def sweep_candidates(
    *, now: datetime, limit: int, ch: _CHClient | None = None
) -> SweepCandidates:
    """What the sweeper acts on, org-wide: leases past ``lease_until``,
    deferred tasks past ``resume_at``, triage rows older than their inbox's
    escalation threshold (joined here so the worker makes one read)."""
    ch = ch or _default_client()
    expired = cast(
        "list[TaskRow]",
        await ch.query_all(
            f"SELECT {TASK_COLS} FROM loom.tasks FINAL WHERE is_deleted = 0 "
            "AND status = 'in_progress' AND lease_until IS NOT NULL "
            "AND lease_until < {now:DateTime} ORDER BY lease_until LIMIT {limit:UInt32} "
            "SETTINGS select_sequential_consistency = 1",
            params={"now": now, "limit": int(limit)},
        ),
    )
    resume = cast(
        "list[TaskRow]",
        await ch.query_all(
            f"SELECT {TASK_COLS} FROM loom.tasks FINAL WHERE is_deleted = 0 "
            "AND status = 'deferred' AND resume_at IS NOT NULL "
            "AND resume_at <= {now:DateTime} ORDER BY resume_at LIMIT {limit:UInt32} "
            "SETTINGS select_sequential_consistency = 1",
            params={"now": now, "limit": int(limit)},
        ),
    )
    stale_triage = cast(
        "list[StaleTriageRow]",
        await ch.query_all(
            "SELECT t.org AS org, t.inbox AS inbox, t.namespace AS namespace, "
            "t.task_id AS task_id, t.from_subject AS from_subject, t.created_at AS created_at, "
            "i.triage_escalate_after_seconds AS after_s "
            "FROM (SELECT * FROM loom.tasks FINAL WHERE is_deleted = 0 AND status = 'triage') AS t "
            "INNER JOIN (SELECT * FROM loom.task_inboxes FINAL WHERE is_deleted = 0) AS i "
            "ON t.org = i.org AND t.inbox = i.inbox "
            "WHERE t.created_at + toIntervalSecond(i.triage_escalate_after_seconds)"
            " < {now:DateTime} "
            "AND NOT JSONHas(t.outcome, 'escalated_at') "
            "ORDER BY t.created_at LIMIT {limit:UInt32}",
            params={"now": now, "limit": int(limit)},
        ),
    )
    return {"expired": expired, "resume": resume, "stale_triage": stale_triage}


async def inbox_counts(
    org: str,
    *,
    namespace: str | None = None,
    inbox: str | None = None,
    ch: _CHClient | None = None,
) -> list[InboxCountRow]:
    """Depth per inbox and status — the bell and the board header."""
    ch = ch or _default_client()
    where = ["org = {org:String}", "is_deleted = 0"]
    params: dict[str, Any] = {"org": org}
    if namespace:
        where.append("namespace = {ns:String}")
        params["ns"] = namespace
    if inbox:
        where.append("inbox = {inbox:String}")
        params["inbox"] = inbox
    return cast(
        "list[InboxCountRow]",
        await ch.query_all(
            "SELECT inbox, status, blocked_on, count() AS n, min(created_at) AS oldest "
            f"FROM loom.tasks FINAL WHERE {' AND '.join(where)} GROUP BY inbox, status, blocked_on",
            params=params,
        ),
    )


# ---------------------------------------------------------------- events


async def insert_event(row: TaskEventRow, *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(
        "loom.task_events", [dict(row)], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )


async def task_history(
    org: str, task_id: str, *, limit: int = 500, ch: _CHClient | None = None
) -> list[TaskEventRow]:
    if not is_uuid(task_id):
        return []
    ch = ch or _default_client()
    return cast(
        "list[TaskEventRow]",
        await ch.query_all(
            f"SELECT {EVENT_COLS} FROM loom.task_events "
            "WHERE org = {org:String} AND task_id = {tid:UUID} ORDER BY seq LIMIT {limit:UInt32}",
            params={"org": org, "tid": task_id, "limit": int(limit)},
        ),
    )


async def events_since(
    org: str,
    *,
    since_seq: int,
    lag_s: int,
    inbox: str | None = None,
    from_subject: str | None = None,
    limit: int = 200,
    ch: _CHClient | None = None,
) -> list[TaskEventRow]:
    """The cursor read. ``seq`` and ``ts`` are stamped from the server's clock
    (``server_version``); only rows older than ``lag_s`` by the server's clock
    are served, so a row still landing on another replica is not skipped for
    ever. Sequentially consistent: every acknowledged insert is in the read."""
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "seq > {since:UInt64}",
        "ts <= now64(3) - toIntervalSecond({lag:Int32})",
    ]
    params: dict[str, Any] = {
        "org": org,
        "since": int(since_seq),
        "lag": int(lag_s),
        "limit": int(limit),
    }
    if inbox:
        where.append("inbox = {inbox:String}")
        params["inbox"] = inbox
    if from_subject:
        where.append("from_subject = {subj:String}")
        params["subj"] = from_subject
    return cast(
        "list[TaskEventRow]",
        await ch.query_all(
            f"SELECT {EVENT_COLS} FROM loom.task_events WHERE {' AND '.join(where)} "
            "ORDER BY seq LIMIT {limit:UInt32} SETTINGS select_sequential_consistency = 1",
            params=params,
        ),
    )


# ---------------------------------------------------------------- webhooks

WEBHOOK_CURSOR_COLS = "org, inbox, last_seq, attempts, next_try_at, last_status, version"


async def list_webhook_inboxes(*, ch: _CHClient | None = None) -> list[WebhookInboxRow]:
    """Every live inbox with a webhook_url, org-wide — what the delivery
    worker fans out over. The secret is never here: ``webhook_secret_ref``
    names the environment variable the worker reads it from."""
    ch = ch or _default_client()
    return cast(
        "list[WebhookInboxRow]",
        await ch.query_all(
            "SELECT org, inbox, owner_namespace, webhook_url, webhook_secret_ref "
            "FROM loom.task_inboxes FINAL WHERE is_deleted = 0 AND webhook_url != '' "
            "ORDER BY org, inbox"
        ),
    )


async def fetch_webhook_cursors(*, ch: _CHClient | None = None) -> list[WebhookCursorRow]:
    ch = ch or _default_client()
    return cast(
        "list[WebhookCursorRow]",
        await ch.query_all(f"SELECT {WEBHOOK_CURSOR_COLS} FROM loom.task_webhook_cursor FINAL"),
    )


async def insert_webhook_cursor(row: WebhookCursorRow, *, ch: _CHClient | None = None) -> None:
    """One new version row per delivery attempt (every column rides along)."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.task_webhook_cursor",
        [dict(row)],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


# ---------------------------------------------------------------- leases


@dataclass(frozen=True)
class ClaimResult:
    won: bool
    fence: int
    owner: str


class KeeperLeases:
    """Claims arbitrated in ClickHouse Keeper.

    One row per live task. The conditional ``ALTER … UPDATE`` under
    ``keeper_map_strict_mode = 1`` is executed as a Keeper multi-op with a
    version check, so of two concurrent claimers exactly one row change
    lands: the loser either raises ``KEEPER_EXCEPTION`` ("Bad version") or
    matches zero rows because the winner landed first. Either way the caller
    reads the row back and owns the task only if it says so. The three
    statements below are the only mutations in this module; they address a
    Keeper znode, not a MergeTree part (mutation inventory: task_leases).
    """

    name = "keeper"

    def __init__(self, ch: _CHClient | None = None) -> None:
        self._ch = ch

    @property
    def ch(self) -> _CHClient:
        return self._ch or _default_client()

    async def ensure_row(self, task_id: str, org: str, inbox: str, fence: int) -> None:
        """Insert the lease row for a new task (owner empty). A retry that
        finds the key present is fine: the row is the same."""
        try:
            await self.ch.execute(
                "INSERT INTO loom.task_leases "
                "(task_id, org, inbox, owner, lease_until, fence, status, updated_at) "
                "SETTINGS keeper_map_strict_mode = 1 "
                "VALUES ({tid:String}, {org:String}, {inbox:String}, '', toDateTime(0), "
                "{fence:UInt32}, 'todo', now())",
                params={"tid": task_id, "org": org, "inbox": inbox, "fence": int(fence)},
            )
        except Exception as e:  # the key exists: a retried create
            if "Node exists" not in str(e):
                raise

    async def try_claim(
        self, task_id: str, claimant: str, *, expected_fence: int, lease_until: datetime
    ) -> ClaimResult:
        try:
            await self.ch.execute(
                "ALTER TABLE loom.task_leases UPDATE "
                "owner = {who:String}, lease_until = {until:DateTime}, "
                "fence = fence + 1, status = 'in_progress', updated_at = now() "
                "WHERE task_id = {tid:String} AND fence = {fence:UInt32} "
                "AND (owner = '' OR lease_until < now()) "
                "SETTINGS keeper_map_strict_mode = 1",
                params={
                    "who": claimant,
                    "until": lease_until,
                    "tid": task_id,
                    "fence": int(expected_fence),
                },
            )
        except Exception as e:
            if "KEEPER_EXCEPTION" in str(e) or "Bad version" in str(e):
                log.info("task lease race lost task=%s claimant=%s", task_id, claimant)
            else:
                raise
        row = await self.ch.query_one(
            "SELECT owner, fence FROM loom.task_leases WHERE task_id = {tid:String}",
            params={"tid": task_id},
        )
        if row is None:
            return ClaimResult(False, int(expected_fence), "")
        won = row["owner"] == claimant and int(row["fence"]) == int(expected_fence) + 1
        return ClaimResult(won, int(row["fence"]), str(row["owner"]))

    async def set_owner(
        self, task_id: str, *, fence: int, owner: str, lease_until: datetime, status: str
    ) -> bool:
        """Heartbeat (same owner, later lease) or release (owner '' on
        expiry, defer, block, reassign). Gated on the fence so a stale writer
        changes nothing; the fence itself does not move."""
        try:
            await self.ch.execute(
                "ALTER TABLE loom.task_leases UPDATE "
                "owner = {who:String}, lease_until = {until:DateTime}, "
                "status = {status:String}, updated_at = now() "
                "WHERE task_id = {tid:String} AND fence = {fence:UInt32} "
                "SETTINGS keeper_map_strict_mode = 1",
                params={
                    "who": owner,
                    "until": lease_until,
                    "status": status,
                    "tid": task_id,
                    "fence": int(fence),
                },
            )
        except Exception as e:
            if "KEEPER_EXCEPTION" in str(e) or "Bad version" in str(e):
                return False
            raise
        row = await self.ch.query_one(
            "SELECT owner, fence FROM loom.task_leases WHERE task_id = {tid:String}",
            params={"tid": task_id},
        )
        return row is not None and int(row["fence"]) == int(fence) and row["owner"] == owner

    async def delete(self, task_id: str) -> None:
        """A terminal transition drops the key so the table holds live work only."""
        await self.ch.execute(
            "ALTER TABLE loom.task_leases DELETE WHERE task_id = {tid:String}",
            params={"tid": task_id},
        )


_backend: KeeperLeases | None = None


async def lease_backend(*, ch: _CHClient | None = None) -> KeeperLeases:
    """The arbitration backend (``loom.task_leases`` in Keeper), cached for the process."""
    global _backend
    if _backend is None:
        _backend = KeeperLeases(ch)
    return _backend


def reset_lease_backend() -> None:
    """Tests swap the client between cases."""
    global _backend
    _backend = None
