"""The reads behind `memory.inspect`: one namespace's live rows split by one identity
column, the subject's ledger calls, and the version counts of the rows it lists.

Every statement filters on (org, namespace), the prefix of each table's sort key. The
subject's column is one of three fixed names, never a caller's value; the id is bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from server.core.repo_base import _CHClient, _default_client

# The identity column each subject is matched on.
SUBJECT_COLUMN: dict[str, str] = {"user": "user_id", "agent": "agent", "session": "session_id"}

View = Literal["own", "shared"]

_LIVE = (
    "FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String} "
    "AND tombstoned_at IS NULL"
)


def _view(subject: str, view: View) -> str:
    col = SUBJECT_COLUMN[subject]
    return f"{col} = {{id:String}}" if view == "own" else f"{col} != {{id:String}}"


def _params(org: str, namespace: str, subject_id: str, **extra: Any) -> dict[str, Any]:
    return {"org": org, "ns": namespace, "id": subject_id, **extra}


async def view_counts(
    org: str,
    namespace: str,
    subject: str,
    subject_id: str,
    view: View,
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(memory_type, status, source_class, rows, first, last) over the view's rows."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT memory_type, toString(status) AS status, source_class, count() AS rows,
               min(updated_at) AS first_at, max(updated_at) AS last_at
        {_LIVE} AND {_view(subject, view)}
        GROUP BY memory_type, status, source_class
        """,
        params=_params(org, namespace, subject_id),
    )


async def view_tags(
    org: str,
    namespace: str,
    subject: str,
    subject_id: str,
    view: View,
    *,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The view's most used tags, (tag, rows)."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT tag, count() AS rows
        FROM (SELECT arrayJoin(tags) AS tag {_LIVE} AND {_view(subject, view)})
        GROUP BY tag ORDER BY rows DESC, tag LIMIT {{lim:UInt32}}
        """,
        params=_params(org, namespace, subject_id, lim=int(limit)),
    )


async def view_categories(
    org: str,
    namespace: str,
    subject: str,
    subject_id: str,
    view: View,
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(category_id, rows): the categories the view's rows are filed under."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT c.category_id AS category_id, count() AS rows
        FROM (SELECT memory_key, category_id FROM loom.memory_categories FINAL
              WHERE org = {{org:String}} AND namespace = {{ns:String}}
                AND tombstoned_at IS NULL) AS c
        INNER JOIN (SELECT memory_key {_LIVE} AND {_view(subject, view)}) AS m
          ON c.memory_key = m.memory_key
        GROUP BY category_id ORDER BY rows DESC
        """,
        params=_params(org, namespace, subject_id),
    )


async def view_newest(
    org: str,
    namespace: str,
    subject: str,
    subject_id: str,
    view: View,
    *,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The view's newest rows, identifiers and a 200-character excerpt (none for a
    high-sensitivity row). The id is selected as `id`: an alias named memory_id would
    shadow the column."""
    return await (ch or _default_client()).query_all(
        f"""
        SELECT memory_key, toString(memory_id) AS id, version,
               toString(content_sha256) AS content_sha256, memory_type,
               toString(status) AS status, source_class, agent, user_id, session_id, actor,
               updated_at,
               if(sensitivity = 'high', '',
                  substring(if(JSONType(value) = 'String', JSONExtractString(value), value),
                            1, 200)) AS excerpt
        {_LIVE} AND {_view(subject, view)}
        ORDER BY updated_at DESC, memory_key
        LIMIT {{lim:UInt32}}
        """,
        params=_params(org, namespace, subject_id, lim=int(limit)),
    )


async def version_counts(
    org: str, namespace: str, memory_ids: Sequence[str], *, ch: _CHClient | None = None
) -> dict[str, int]:
    """memory_id -> the version rows the history holds for it."""
    if not memory_ids:
        return {}
    rows = await (ch or _default_client()).query_all(
        """
        SELECT toString(memory_id) AS id, count() AS versions
        FROM loom.memory_versions
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_id IN {ids:Array(UUID)}
        GROUP BY memory_id
        """,
        params={"org": org, "ns": namespace, "ids": list(memory_ids)},
    )
    return {str(r["id"]): int(r["versions"]) for r in rows}


async def subject_calls(
    org: str,
    namespace: str,
    subject: str,
    subject_id: str,
    *,
    window_seconds: int,
    skip_consumers: Sequence[str],
    skip_ops: Sequence[str],
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(operation, calls, succeeded, first, last) for the subject's own calls in the
    window, leaving out the consumers that inspect a namespace on a person's behalf and
    the reads about the subject, which the ledger files under the identity they name."""
    col = SUBJECT_COLUMN[subject]
    return await (ch or _default_client()).query_all(
        f"""
        SELECT operation, count() AS calls, countIf(success) AS succeeded,
               min(ts) AS first_at, max(ts) AS last_at
        FROM loom.agent_events
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND ts >= now() - INTERVAL {{win:UInt32}} SECOND
          AND {col} = {{id:String}} AND kind != 'worker'
          AND consumer NOT IN {{skip:Array(String)}}
          AND operation NOT IN {{skip_ops:Array(String)}}
        GROUP BY operation ORDER BY calls DESC, operation
        LIMIT 200
        """,
        params=_params(
            org,
            namespace,
            subject_id,
            win=int(window_seconds),
            skip=list(skip_consumers),
            skip_ops=list(skip_ops),
        ),
    )
