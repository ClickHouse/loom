"""loom.session_briefs / loom.session_brief_revisions.

`build_brief_row` is the single source of truth for the column set: every
write — a new revision, a model-written overlay, a tombstone — goes through
it, so a version row never zeros a column under FINAL
(`docs/architecture/clickhouse.md`). The revisions table is filled by
`session_brief_revisions_mv` from the same INSERT; nothing writes it
directly.

Point reads (`current`) are FINAL on the full sort key. Listings take the
newest version per brief with `LIMIT 1 BY brief_key` and filter afterwards:
a filter on a non-key column (project, tombstoned_at) must see the newest
version of a brief, never an older one that still matches.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from server.core.ch import own_write_reads
from server.core.datetimes import naive_utc_now as now
from server.identity.domain.read_scope import ReadFilters, scope_and, scope_where
from server.memory.store.rows import version_over

TABLE = "loom.session_briefs"
REVISIONS = "loom.session_brief_revisions"

# Every persisted column of loom.session_briefs except `version`, which the
# builder stamps. A unit test holds this tuple to the migration's DDL.
COLUMNS: tuple[str, ...] = (
    "org",
    "namespace",
    "brief_key",
    "agent",
    "user_id",
    "session_id",
    "source_sessions",
    "project",
    "repo",
    "branch",
    "head_sha",
    "revision",
    "trigger",
    "author",
    "goal",
    "sections",
    "rendered",
    "event_cursor",
    "events_total",
    "source_chars",
    "brief_chars",
    "dropped_ungrounded",
    "prompt_version",
    "llm_model",
    "tokens_in",
    "tokens_out",
    "actor",
    "source_class",
    "created_at",
    "updated_at",
    "tombstoned_at",
)

_DEFAULTS: dict[str, Any] = {
    "agent": "-",
    "user_id": "-",
    "session_id": "-",
    "source_sessions": [],
    "project": "",
    "repo": "",
    "branch": "",
    "head_sha": "",
    "revision": 1,
    "trigger": "",
    "author": "",
    "goal": "",
    "sections": "{}",
    "rendered": "",
    "event_cursor": "",
    "events_total": 0,
    "source_chars": 0,
    "brief_chars": 0,
    "dropped_ungrounded": 0,
    "prompt_version": "",
    "llm_model": "",
    "tokens_in": 0,
    "tokens_out": 0,
    "actor": "",
    "source_class": "derived",
    "tombstoned_at": None,
}

_SELECT = ", ".join(COLUMNS) + ", version"


def build_brief_row(
    prev: dict[str, Any] | None, *, version_after: int = 0, **changes: Any
) -> dict[str, Any]:
    """Merge the row's columns and stamp it above the observed head."""
    unknown = set(changes) - set(COLUMNS)
    if unknown:
        raise KeyError(f"not a loom.session_briefs column: {sorted(unknown)}")
    base: dict[str, Any] = {c: _DEFAULTS.get(c) for c in COLUMNS}
    if prev:
        base.update({c: prev[c] for c in COLUMNS if c in prev})
    base.update(changes)
    stamp = now()
    base["created_at"] = base.get("created_at") or stamp
    base["updated_at"] = changes.get("updated_at") or stamp
    for c in ("org", "namespace", "brief_key"):
        if not base.get(c):
            raise ValueError(f"session brief row needs {c}")
    base["version"] = version_over(max(version_after, int((prev or {}).get("version") or 0)))
    return base


async def insert(ch: Any, row: dict[str, Any]) -> None:
    await ch.insert(TABLE, [row])


async def current(
    ch: Any, org: str, namespace: str, brief_key: str, *, scope: ReadFilters
) -> dict[str, Any] | None:
    """The newest version of one brief, tombstoned or not (a save after a
    delete continues the revision count), inside the read `scope`. Read with
    sequential consistency: the previous save is often seconds old
    (PreCompact, then SessionEnd)."""
    params: dict[str, Any] = {"org": org, "ns": namespace, "key": brief_key}
    with own_write_reads():
        row = await ch.query_one(
            f"""
            SELECT {_SELECT}
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND brief_key = {{key:String}}{scope_and(scope, params)}
            LIMIT 1
            """,
            params=params,
        )
    return dict(row) if row else None


def snapshot_id(row: dict[str, Any]) -> str:
    """Pin the commit and brief content even if two writes share a version."""
    digest = sha256()
    for name in ("head_sha", "rendered", "sections"):
        data = row[name].encode("utf-8")
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
    return f"{row['version']}.{digest.hexdigest()}"


async def by_revision(
    ch: Any,
    org: str,
    namespace: str,
    brief_key: str,
    revision: int,
    *,
    scope: ReadFilters,
    snapshot: tuple[int, str] | None = None,
) -> dict[str, Any] | None:
    """Read a revision written after the last delete. Version orders writes
    even when concurrent saves reuse or move the revision number backwards.
    """
    params: dict[str, Any] = {"org": org, "ns": namespace, "key": brief_key, "rev": int(revision)}
    snapshot_clause = ""
    if snapshot is not None:
        params["snapshot"], params["digest"] = snapshot
        # Match the UTF-8 byte framing in snapshot_id.
        snapshot_clause = (
            " AND version = {snapshot:UInt64}"
            " AND SHA256(concat(toString(length(head_sha)), ':', head_sha, "
            "toString(length(rendered)), ':', rendered, "
            "toString(length(sections)), ':', sections)) = unhex({digest:String})"
        )
    with own_write_reads():
        row = await ch.query_one(
            f"""
            SELECT brief_key, revision, agent, user_id, session_id, head_sha, trigger,
                   author, sections, rendered, events_total, source_chars, brief_chars,
                   actor, updated_at, tombstoned_at, version
            FROM {REVISIONS}
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND brief_key = {{key:String}} AND revision = {{rev:UInt32}}{snapshot_clause}
              AND version > (
                  SELECT max(version) FROM {REVISIONS}
                  WHERE org = {{org:String}} AND namespace = {{ns:String}}
                    AND brief_key = {{key:String}} AND tombstoned_at IS NOT NULL
              )
              {scope_and(scope, params)}
            ORDER BY version DESC
            LIMIT 1
            """,
            params=params,
        )
    return dict(row) if row else None


async def revision_count(ch: Any, org: str, namespace: str, brief_key: str) -> int:
    row = await ch.query_one(
        f"""
        SELECT uniqExact(revision) AS n
        FROM {REVISIONS}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND brief_key = {{key:String}}
        """,
        params={"org": org, "ns": namespace, "key": brief_key},
    )
    return int((row or {}).get("n") or 0)


def _latest_sql(where_after: list[str], limit_param: bool) -> str:
    after = " AND ".join(["tombstoned_at IS NULL", *where_after])
    limit = "LIMIT {lim:UInt32}" if limit_param else "LIMIT 1"
    return f"""
        SELECT {_SELECT}
        FROM (
            SELECT {_SELECT}
            FROM {TABLE}
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
            ORDER BY brief_key, version DESC
            LIMIT 1 BY brief_key
        )
        WHERE {after}
        ORDER BY updated_at DESC
        {limit}
    """


async def listing(
    ch: Any,
    org: str,
    namespace: str,
    *,
    project: str = "",
    agent: str = "",
    from_session: str = "",
    limit: int = 20,
    scope: ReadFilters,
    key_prefix: str = "",
) -> list[dict[str, Any]]:
    """Live briefs of a namespace inside the read `scope` whose key starts with
    `key_prefix` and holds no further `~` (an own_user prefix must not match
    that user's own_session briefs), most recently saved first."""
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": int(limit)}
    where: list[str] = scope_where(scope, params)
    if key_prefix:
        where.append("startsWith(brief_key, {key_prefix:String})")
        where.append("position(brief_key, '~', length({key_prefix:String}) + 1) = 0")
        params["key_prefix"] = key_prefix
    if project:
        where.append("project = {project:String}")
        params["project"] = project
    if agent and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if from_session:
        where.append("has(source_sessions, {sid:String})")
        params["sid"] = from_session
    with own_write_reads():
        rows = await ch.query_all(_latest_sql(where, True), params=params)
    return [dict(r) for r in rows or []]
