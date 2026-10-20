"""Repository for loom.extraction_policies: the operator's extraction policy
per org (namespace '') and per namespace.

One row per change, ReplacingMergeTree(version) keyed on (org, namespace), so
the newest change wins; a cleared policy is a row with deleted = 1.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client


async def insert_row(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """Write one change, committed before it returns (the next read must see it)."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.extraction_policies",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def get(org: str, namespace: str, *, ch: _CHClient | None = None) -> dict[str, Any] | None:
    """The scope's live policy; None when it was never set or was cleared."""
    ch = ch or _default_client()
    row: dict[str, Any] | None = await ch.query_one(
        "SELECT policy, updated_by, updated_at "
        "FROM loom.extraction_policies FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND deleted = 0 LIMIT 1",
        params={"org": org, "ns": namespace},
    )
    return row


async def live_rows(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Every live policy of every org, one row per scope. Never cut short: a
    missing row would let an extraction run without its policy."""
    ch = ch or _default_client()
    rows: list[dict[str, Any]] = await ch.query_all(
        "SELECT org, namespace, policy FROM loom.extraction_policies FINAL WHERE deleted = 0"
    )
    return rows
