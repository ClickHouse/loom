"""Repository for loom.org_model_calls: whether an org's data may go to a model.

One row per change, ReplacingMergeTree(version) keyed on org, so the newest
setting wins; no row means enabled.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client


async def insert_row(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """Write one setting, committed before it returns (the next read must see it)."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.org_model_calls",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def get(org: str, *, ch: _CHClient | None = None) -> dict[str, Any] | None:
    """The org's current setting; None when it was never set."""
    ch = ch or _default_client()
    row: dict[str, Any] | None = await ch.query_one(
        "SELECT enabled, reason, updated_by, updated_at FROM loom.org_model_calls FINAL "
        "WHERE org = {org:String} LIMIT 1",
        params={"org": org},
    )
    return row
