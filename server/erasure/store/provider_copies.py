"""Rebuild copies cannot disappear from an erasure's residue accounting.

Admission defers erasures during a provider transition. Its coordinator removes
all copies before reopening. Direct/recovery censuses also fail closed while
any copy remains; they never claim deletion while a snapshot could resurrect it.
No copy is modified here: doing so would invalidate the frozen rebuild recipe.
"""

import re
from collections.abc import Sequence
from typing import Any

from server.core.repo_base import _CHClient

_COPY = re.compile(r"^_provider_rebuild_[0-9a-f]{32}_[a-z_]+_(source|target|publish|backup)$")


async def copy_residue(
    org: str, scope_type: str, scope_value: str | Sequence[str], *, ch: _CHClient
) -> dict[str, int]:
    copies = await ch.query_all(
        "SELECT t.name AS table, groupArray(c.name) AS columns FROM system.tables AS t "
        "INNER JOIN system.columns AS c ON t.database = c.database AND t.name = c.table "
        "WHERE t.database = currentDatabase() AND startsWith(t.name, '_provider_rebuild_') "
        "GROUP BY t.name"
    )
    residue = {}
    for copy in copies:
        name = str(copy["table"])
        if not _COPY.fullmatch(name):
            raise ValueError("Unknown rebuild copy prevents verified erasure")
        columns = set(copy["columns"])
        params: dict[str, Any] = {}
        predicates = []
        if "org" in columns:
            params["org"] = org
            predicates.append("org = {org:String}")
        if scope_type == "namespace" and "namespace" in columns:
            params["namespaces"] = (
                [scope_value] if isinstance(scope_value, str) else list(scope_value)
            )
            # a code.ask answer copy holds the memory it quoted under memory_namespace
            if "memory_namespace" in columns:
                predicates.append(
                    "(namespace IN {namespaces:Array(String)} "
                    "OR memory_namespace IN {namespaces:Array(String)})"
                )
            else:
                predicates.append("namespace IN {namespaces:Array(String)}")
        elif scope_type == "user_id" and "user_id" in columns:
            params["user_id"] = scope_value
            predicates.append("user_id = {user_id:String}")
        # Unscoped derived caches/rows count globally, conservatively. Count
        # physical rows, including tombstones and obsolete replacing versions.
        where = " AND ".join(predicates) or "1"
        row = await ch.query_one(f"SELECT count() AS n FROM loom.`{name}` WHERE {where}", params)
        if row is None or type(row.get("n")) is not int or row["n"] < 0:
            raise ValueError("Cannot establish rebuild-copy erasure residue")
        residue[f"loom.{name}"] = row["n"]
    return residue
