"""The reads behind `namespace.tune`: which agents write a namespace and which of them
through extraction, how its keys are laid out, which plain tags it uses and their
spellings, and the rows a draft cites.

Every statement filters on (org, namespace), the prefix of `loom.memory`'s sort key,
takes the caller's read `scope`, and reads no value. A tag or key the caller names is
bound, never formatted into SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and

_LIVE = (
    "FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String} "
    "AND tombstoned_at IS NULL"
)
_IDENTITY = "agent, user_id, session_id, memory_key"
# Spellings of one name a category draft names.
MAX_SPELLINGS = 20  # not a setting: bounds one draft's text


async def writing_agents(
    org: str, namespace: str, *, scope: ReadFilters, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """(agent, rows, total): the agents that wrote the namespace's live rows, most rows
    first; `total` counts every one, past the limit."""
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": int(limit)}
    return await (ch or _default_client()).query_all(
        f"""
        SELECT agent, count() AS rows, count() OVER () AS total {_LIVE}{scope_and(scope, params)}
        GROUP BY agent ORDER BY rows DESC, agent LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


# The source trace memory.set_from_messages stamps on the rows its extraction writes.
EXTRACTION_TRACE = "pipeline:set_from_messages"


async def extraction_writers(
    org: str, namespace: str, *, scope: ReadFilters, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """(agent, rows, total, total_rows): the agents whose live rows memory.set_from_messages'
    extraction wrote, most rows first; `total` counts every such agent and `total_rows`
    every such row, past the limit."""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "trace": EXTRACTION_TRACE,
        "lim": int(limit),
    }
    return await (ch or _default_client()).query_all(
        f"""
        SELECT agent, count() AS rows, count() OVER () AS total,
               sum(count()) OVER () AS total_rows {_LIVE}
          AND startsWith(source_trace, {{trace:String}}){scope_and(scope, params)}
        GROUP BY agent ORDER BY rows DESC, agent LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def written_by_extraction(
    org: str,
    namespace: str,
    identities: Sequence[tuple[str, str, str, str]],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The rows among `identities` (agent, user_id, session_id, memory_key) whose live
    version memory.set_from_messages' extraction wrote."""
    if not identities:
        return []
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "trace": EXTRACTION_TRACE,
        "ids": [tuple(i) for i in identities],
        "lim": len(identities),
    }
    return await (ch or _default_client()).query_all(
        f"""
        SELECT {_IDENTITY} {_LIVE}
          AND startsWith(source_trace, {{trace:String}})
          AND ({_IDENTITY}) IN {{ids:Array(Tuple(String, String, String, String))}}
          {scope_and(scope, params)}
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def key_prefixes(
    org: str,
    namespace: str,
    *,
    scope: ReadFilters,
    min_rows: int,
    name_pattern: str,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(prefix, rows): the first path segment of the live rows' keys that have a path,
    holding at least `min_rows` rows and matching `name_pattern`, most rows first. A
    key with no `/` has no prefix. The filters run before the limit, so a guide is
    compared with every prefix that could be drafted, not only the most used."""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "min": int(min_rows),
        "pattern": name_pattern,
        "lim": int(limit),
    }
    return await (ch or _default_client()).query_all(
        f"""
        SELECT splitByChar('/', memory_key)[1] AS prefix, count() AS rows
        {_LIVE} AND position(memory_key, '/') > 0{scope_and(scope, params)}
        GROUP BY prefix
        HAVING rows >= {{min:UInt32}} AND match(prefix, {{pattern:String}})
        ORDER BY rows DESC, prefix LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def plain_tags(
    org: str,
    namespace: str,
    *,
    scope: ReadFilters,
    min_rows: int,
    name_pattern: str,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(tag, rows): the live rows' tags that match `name_pattern` and are on at least
    `min_rows` rows, most rows first. The filters run before the limit, so a plain tag
    is found however many provenance tags rank above it."""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "min": int(min_rows),
        "pattern": name_pattern,
        "lim": int(limit),
    }
    return await (ch or _default_client()).query_all(
        f"""
        SELECT tag, count() AS rows
        FROM (SELECT arrayJoin(tags) AS tag {_LIVE}{scope_and(scope, params)})
        GROUP BY tag
        HAVING rows >= {{min:UInt32}} AND match(tag, {{pattern:String}})
        ORDER BY rows DESC, tag LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def tag_spellings(
    org: str,
    namespace: str,
    names: Sequence[str],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """(tag, rows): every spelling of `names` (lowercase) on live rows, however few rows
    carry it, up to `MAX_SPELLINGS` per name."""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "names": list(names),
        "per": MAX_SPELLINGS,
    }
    return await (ch or _default_client()).query_all(
        f"""
        SELECT tag, count() AS rows
        FROM (SELECT arrayJoin(tags) AS tag {_LIVE}{scope_and(scope, params)})
        WHERE lower(tag) IN {{names:Array(String)}}
        GROUP BY tag ORDER BY rows DESC, tag
        LIMIT {{per:UInt32}} BY lower(tag)
        """,
        params=params,
    )


async def tagged_rows(
    org: str,
    namespace: str,
    tag: str,
    *,
    scope: ReadFilters,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The newest live rows carrying `tag`, by identity: the rows a category would hold."""
    params: dict[str, Any] = {"org": org, "ns": namespace, "tag": tag, "lim": int(limit)}
    return await (ch or _default_client()).query_all(
        f"""
        SELECT {_IDENTITY} {_LIVE} AND has(tags, {{tag:String}}){scope_and(scope, params)}
        ORDER BY updated_at DESC, {_IDENTITY} LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def tagged_in_other_categories(
    org: str, namespace: str, tag: str, *, scope: ReadFilters, ch: _CHClient | None = None
) -> int:
    """How many live rows with `tag` are already filed under some category."""
    params: dict[str, Any] = {"org": org, "ns": namespace, "tag": tag}
    row = await (ch or _default_client()).query_one(
        f"""
        SELECT count() AS rows
        FROM (SELECT memory_key {_LIVE} AND has(tags, {{tag:String}}){scope_and(scope, params)})
             AS m
        WHERE m.memory_key IN (
            SELECT memory_key FROM loom.memory_categories FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND tombstoned_at IS NULL)
        """,
        params=params,
    )
    return int(row["rows"]) if row else 0
