"""Reads over `loom.operator_edges` for the bundle.

The table is `ReplacingMergeTree(created_at)` keyed
`(org, namespace, from_key, to_key, relation)`, so a namespace's live edges
are one keyed range read, and the same tuple is the keyset cursor. The
`memory.export_evidence kind=edges` handler reads through here. The writers
(`memory.link`, the digest chain, trajectory notes) insert in their own modules.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client

EDGE_COLS = "from_key, to_key, relation, confidence, explanation, created_at"


async def list_live(
    org: str,
    namespace: str,
    *,
    limit: int,
    after: tuple[str, str, str] | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Up to `limit` live edges of the namespace ordered by the table key,
    starting strictly after the `(from_key, to_key, relation)` cursor."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": int(limit)}
    where = ["org = {org:String}", "namespace = {ns:String}", "tombstoned_at IS NULL"]
    if after is not None:
        where.append(
            "(from_key, to_key, relation) > ({c_from:String}, {c_to:String}, {c_rel:String})"
        )
        params["c_from"], params["c_to"], params["c_rel"] = after
    return await ch.query_all(
        f"""
        SELECT {EDGE_COLS}
        FROM loom.operator_edges FINAL
        WHERE {" AND ".join(where)}
        ORDER BY from_key ASC, to_key ASC, relation ASC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
