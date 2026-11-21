"""Repository for loom.blacklist ("Refuse" patterns).

Three scopes: global (org='*', namespace='*'), org-wide (org=<org>,
namespace='*'), and namespace (both set). The list-by-scope SQL has
to encode all three lookup shapes; centralizing here keeps the
predicates in one place.

Soft-delete inserts a complete row with a newer created_at version and tombstoned_at.
The in-process cache and its refresh live in server.memory.app.blacklist_guard;
the statements it runs (active patterns and the version watermark) are here.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from server.core import ch_names
from server.core.ch import set_query_tag, statement_timeout
from server.core.ch_bounded import BoundedReadError, read_rows
from server.core.datetimes import fits_datetime_column, to_naive_utc_second
from server.core.repo_base import _CHClient, _default_client


async def insert_row(
    row: dict[str, Any],
    *,
    ch: _CHClient | None = None,
    deadline: float | None = None,
) -> None:
    """Insert one blacklist pattern. async_insert=0 so the next list
    sees the new row."""
    ch = ch or _default_client()
    if deadline is None:
        await ch.insert(
            "loom.blacklist", [row], settings={"async_insert": 0, "wait_for_async_insert": 1}
        )
    else:
        await _bounded_insert("loom.blacklist", row, ch=ch, deadline=deadline)


async def list_for_scope(
    org: str,
    namespace: str,
    *,
    scope: str | None = None,
    limit: int = 100,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """List blacklist patterns visible to (org, namespace).

    `scope=None` returns every pattern that applies (namespace +
    org-wide + global). Specific scope strings filter exact match.
    """
    ch = ch or _default_client()
    if scope is None:
        where = (
            "((org = {org:String} AND namespace = {ns:String}) "
            "OR (org = {org:String} AND namespace = '*') "
            "OR (org = '*' AND namespace = '*'))"
        )
    elif scope == "global":
        where = "scope = 'global'"
    elif scope == "org":
        where = "org = {org:String} AND scope = 'org'"
    else:
        where = "org = {org:String} AND namespace = {ns:String} AND scope = 'namespace'"
    return await ch.query_all(
        f"""
        SELECT pattern_id, org, namespace, pattern, pattern_type, scope,
               reason, created_by, hit_count, last_hit_at, created_at
        FROM loom.blacklist FINAL
        WHERE tombstoned_at IS NULL AND {where}
        ORDER BY created_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )


async def find_active_pattern(
    pattern_id: str,
    *,
    org: str | None = None,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """Look up a live pattern by id, optionally restricted to one org.

    Returns the row's org, namespace and scope with the id: the caller's
    authority to remove or sweep a pattern is decided from the row's own
    scope, never from which lookup found it. Global rows carry org='*';
    pass ``org="*"`` to look up only those.
    """
    ch = ch or _default_client()
    where = ["pattern_id = {pid:UUID}", "tombstoned_at IS NULL"]
    params: dict[str, Any] = {"pid": pattern_id}
    if org is not None:
        where.append("org = {org:String}")
        params["org"] = org
    return await ch.query_one(
        f"""
        SELECT pattern_id, org, namespace, scope, pattern, pattern_type
        FROM loom.blacklist FINAL
        WHERE {" AND ".join(where)}
        LIMIT 1
        """,
        params=params,
    )


async def tombstone_pattern(
    pattern_id: str,
    when: datetime,
    *,
    ch: _CHClient | None = None,
    selected: tuple[str, str, str] | None = None,
) -> bool:
    """Insert a complete tombstone past the selected row's DateTime version."""
    client = ch or _default_client()
    deadline = time.monotonic() + 30.0
    await check_bounded_storage(ch=client, deadline=deadline)
    predicate = "pattern_id = {pid:UUID}"
    params: dict[str, Any] = {"pid": pattern_id, "limit": 3}
    if selected is not None:
        predicate += (
            " AND org = {org:String} AND namespace = {ns:String} AND scope = {scope:String}"
        )
        params.update(org=selected[0], ns=selected[1], scope=selected[2], limit=2)
    rows = await read_rows(
        "SELECT "
        + _ROW_PROJECTION
        + " FROM loom.blacklist FINAL WHERE "
        + predicate
        + " LIMIT {limit:UInt32} FORMAT JSONEachRow",
        params,
        ch=client,
        deadline=deadline,
        row_bytes=32768,
        total_bytes=65536,
        max_rows=2 if selected is None else 1,
        tag="blacklist.remove.selected",
    )
    if len(rows) > 1:
        raise BoundedReadError("The refusal identity is ambiguous")
    if not rows:
        return False
    row = _decode_bounded_row(rows[0])
    if row["tombstoned_at"] is not None:
        return False
    epoch = datetime(1970, 1, 1)
    created_at: datetime = row["created_at"]
    stored = (created_at - epoch).days * 86400 + (created_at - epoch).seconds
    if stored == 2**32 - 1:
        raise BoundedReadError("A refusal successor version cannot be represented")
    if not fits_datetime_column(when):
        raise BoundedReadError("Invalid refusal removal time")
    clocks = await read_rows(
        "SELECT partition_key, toUnixTimestamp(now()) AS now FROM system.tables "
        "WHERE database = {database:String} AND name = 'blacklist' LIMIT 2 FORMAT JSONEachRow",
        {"database": ch_names.database()},
        ch=client,
        deadline=deadline,
        row_bytes=1024,
        total_bytes=1024,
        max_rows=1,
        tag="blacklist.remove.successor",
    )
    if (
        len(clocks) != 1
        or set(clocks[0]) != {"partition_key", "now"}
        or clocks[0]["partition_key"] != ""
        or type(clocks[0]["now"]) is not int
        or not 0 <= clocks[0]["now"] <= 2**32 - 1
    ):
        raise BoundedReadError("The refusal replacement storage could not be established")
    clock: int = clocks[0]["now"]
    successor = epoch + timedelta(seconds=max(clock, stored + 1))
    await insert_row(
        {**row, "created_at": successor, "tombstoned_at": to_naive_utc_second(when)},
        ch=client,
        deadline=deadline,
    )
    return True


async def active_patterns(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Every non-tombstoned pattern, oldest first, ties by id, so every replica reads
    them in one order (the cache orders them by scope precedence)."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT pattern_id, org, namespace, pattern, pattern_type, scope, reason
        FROM loom.blacklist FINAL
        WHERE tombstoned_at IS NULL
        ORDER BY created_at ASC, pattern_id ASC
        """,
    )


async def scope_regexes(
    scope: str, org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[str]:
    """The live regex patterns stored at exactly this scope."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT pattern FROM loom.blacklist FINAL
        WHERE tombstoned_at IS NULL AND pattern_type = 'regex'
          AND scope = {scope:String} AND org = {org:String} AND namespace = {ns:String}
        """,
        params={"scope": scope, "org": org, "ns": namespace},
    )
    return [str(r["pattern"]) for r in rows]


async def version_watermark(*, ch: _CHClient | None = None) -> tuple[int, int]:
    """(rows, sum of versions) over the watermark table's unmerged rows. Every
    add/remove inserts a row with a fresh version, so the pair moves on every
    change whichever writer's clock is ahead; a background merge collapsing
    rows moves it too, which only costs one extra refresh."""
    ch = ch or _default_client()
    row = await ch.query_one("SELECT count() AS n, sum(version) AS s FROM loom.blacklist_version")
    if not row:
        return (0, 0)
    return (int(row.get("n") or 0), int(row.get("s") or 0))


async def insert_version(
    version: int,
    *,
    updated_by: str,
    ch: _CHClient | None = None,
    deadline: float | None = None,
) -> None:
    """Append one watermark row. No read first: two concurrent bumps write two
    distinct rows."""
    ch = ch or _default_client()
    row = {"singleton": 0, "version": int(version), "updated_by": updated_by}
    if deadline is None:
        await ch.insert("loom.blacklist_version", [row])
    else:
        await _bounded_insert("loom.blacklist_version", row, ch=ch, deadline=deadline)


async def _bounded_insert(table: str, row: dict[str, Any], *, ch: Any, deadline: float) -> None:
    remaining = min(2.0, deadline - time.monotonic())
    if remaining <= 0:
        raise BoundedReadError("Blacklist write allowance exhausted")
    set_query_tag("cell.each.blacklist_write")
    async with asyncio.timeout(remaining):
        with statement_timeout(remaining):
            await ch.insert(
                table,
                [row],
                settings={
                    "async_insert": 0,
                    "wait_for_async_insert": 1,
                    "max_memory_usage": 128 * 1024**2,
                },
            )


_COLUMN_TYPES = {
    "pattern_id": "UUID",
    "org": "LowCardinality(String)",
    "namespace": "LowCardinality(String)",
    "pattern": "String",
    "pattern_type": "Enum8('substring'=1,'regex'=2)",
    "scope": "Enum8('namespace'=1,'org'=2,'global'=3)",
    "reason": "String",
    "created_by": "String",
    "hit_count": "UInt64",
    "created_at": "DateTime",
    "tombstoned_at": "Nullable(DateTime)",
    "last_hit_at": "Nullable(DateTime)",
}
_ROW_PROJECTION = (
    "pattern_id, org, namespace, pattern, pattern_type, scope, "
    "reason, created_by, hit_count, toUnixTimestamp(created_at) AS created_at, "
    "toUnixTimestamp(tombstoned_at) AS tombstoned_at, "
    "toUnixTimestamp(last_hit_at) AS last_hit_at"
)


async def check_bounded_storage(*, ch: Any, deadline: float) -> None:
    import re

    tables = await read_rows(
        "SELECT engine, engine_full, sorting_key FROM system.tables "
        "WHERE database = {database:String} AND name = 'blacklist' LIMIT 2 FORMAT JSONEachRow",
        {"database": ch_names.database()},
        ch=ch,
        deadline=deadline,
        row_bytes=16384,
        total_bytes=16384,
        max_rows=1,
        tag="cell.each.blacklist_shape",
    )
    if len(tables) != 1 or set(tables[0]) != {"engine", "engine_full", "sorting_key"}:
        raise BoundedReadError("Unsupported blacklist storage")
    table = tables[0]
    # Engine arguments end before ORDER BY/SETTINGS; quoted Keeper paths can hold parentheses.
    definition = re.match(
        rf"{re.escape(str(table['engine']))}\(((?:[^'()]|'(?:[^'\\]|\\.|'')*')*)\)(?=\s|$)",
        table["engine_full"] if type(table["engine_full"]) is str else "",
    )
    if (
        table["engine"]
        not in {"ReplacingMergeTree", "ReplicatedReplacingMergeTree", "SharedReplacingMergeTree"}
        or type(table["engine_full"]) is not str
        or type(table["sorting_key"]) is not str
        or not table["engine_full"].startswith(table["engine"] + "(")
        or definition is None
        or re.search(r"(?:^|,)\s*created_at\s*$", definition[1]) is None
        or table["sorting_key"].replace(" ", "").strip("()") != "org,namespace,pattern_id"
    ):
        raise BoundedReadError("Unsupported blacklist storage")
    columns = await read_rows(
        "SELECT name, type FROM system.columns "
        "WHERE database = {database:String} AND table = 'blacklist' "
        "ORDER BY position LIMIT 13 FORMAT JSONEachRow",
        {"database": ch_names.database()},
        ch=ch,
        deadline=deadline,
        row_bytes=1024,
        total_bytes=16384,
        max_rows=12,
        tag="cell.each.blacklist_columns",
    )
    found: dict[str, str] = {}
    for column in columns:
        if (
            set(column) != {"name", "type"}
            or type(column["name"]) is not str
            or type(column["type"]) is not str
            or column["name"] in found
        ):
            raise BoundedReadError("Unsupported blacklist columns")
        found[column["name"]] = column["type"].replace(" ", "")
    if found != _COLUMN_TYPES:
        raise BoundedReadError("Unsupported blacklist columns")


def _decode_bounded_row(row: dict[str, Any]) -> dict[str, Any]:
    if set(row) != set(_COLUMN_TYPES):
        raise BoundedReadError("Malformed blacklist row")
    for key in (
        "pattern_id",
        "org",
        "namespace",
        "pattern",
        "pattern_type",
        "scope",
        "reason",
        "created_by",
    ):
        if type(row[key]) is not str:
            raise BoundedReadError("Malformed blacklist field")
    try:
        if str(UUID(row["pattern_id"])) != row["pattern_id"]:
            raise ValueError
    except ValueError as error:
        raise BoundedReadError("Malformed blacklist identity") from error
    if (
        row["scope"] not in {"global", "org", "namespace"}
        or row["pattern_type"] not in {"substring", "regex"}
        or type(row["hit_count"]) is not int
        or not 0 <= row["hit_count"] <= 2**64 - 1
    ):
        raise BoundedReadError("Malformed blacklist row")
    for key in ("created_at", "last_hit_at", "tombstoned_at"):
        value = row[key]
        if value is None and key != "created_at":
            continue
        if type(value) is not int or not 0 <= value <= 2**32 - 1:
            raise BoundedReadError("Malformed blacklist timestamp")
        row[key] = datetime(1970, 1, 1) + timedelta(seconds=value)
    return row


async def collision_bounded(pattern_id: str, *, ch: Any, deadline: float) -> bool:
    rows = await read_rows(
        "SELECT " + _ROW_PROJECTION + " FROM loom.blacklist FINAL "
        "WHERE pattern_id = {pid:UUID} LIMIT 3 FORMAT JSONEachRow",
        {"pid": pattern_id},
        ch=ch,
        deadline=deadline,
        row_bytes=32768,
        total_bytes=65536,
        max_rows=2,
        tag="cell.each.blacklist_collision",
    )
    for row in rows:
        _decode_bounded_row(row)
    return bool(rows)


async def global_list_bounded(limit: int, *, ch: Any, deadline: float) -> list[dict[str, Any]]:
    if not 1 <= limit <= 50:
        raise BoundedReadError("Unsupported blacklist display limit")
    rows = await read_rows(
        "SELECT " + _ROW_PROJECTION + " FROM loom.blacklist FINAL "
        "WHERE org = '*' AND namespace = '*' AND scope = 'global' AND tombstoned_at IS NULL "
        "ORDER BY created_at DESC, pattern_id LIMIT {limit:UInt32} FORMAT JSONEachRow",
        {"limit": limit + 1},
        ch=ch,
        deadline=deadline,
        row_bytes=32768,
        total_bytes=1792 * 1024,
        max_rows=limit + 1,
        tag="cell.each.blacklist_list",
    )
    return [_decode_bounded_row(row) for row in rows]


async def global_regexes_bounded(*, ch: Any, deadline: float) -> list[str]:
    rows = await read_rows(
        "SELECT pattern FROM loom.blacklist FINAL WHERE org = '*' AND namespace = '*' "
        "AND scope = 'global' AND pattern_type = 'regex' AND tombstoned_at IS NULL "
        "LIMIT 1025 FORMAT JSONEachRow",
        {},
        ch=ch,
        deadline=deadline,
        row_bytes=2048,
        total_bytes=1024**2,
        max_rows=1024,
        tag="cell.each.blacklist_regexes",
    )
    if any(set(row) != {"pattern"} or type(row["pattern"]) is not str for row in rows):
        raise BoundedReadError("Malformed global regex programme")
    return [row["pattern"] for row in rows]
