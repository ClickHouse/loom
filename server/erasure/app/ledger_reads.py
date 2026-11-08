"""What other planes read of the erasure registry and ledger: the tables a namespace's
rows live in, which the in-use check of a new name reads, and the erasures still open
in an org, which the knowledge plane's move of originals to the bucket is fenced by."""

from __future__ import annotations

from typing import Any

from server.erasure.store import census, ledger, steps_pg


def namespace_tables() -> list[str]:
    """The census tables a namespace lookup reads (`census.namespace_tables`)."""
    return census.namespace_tables()


async def pg_holds_namespace(org: str, namespace: str) -> bool:
    """Whether Postgres holds a memory of (org, namespace) (`steps_pg.pg_holds_namespace`)."""
    return await steps_pg.pg_holds_namespace(org, namespace)


async def unfinished_since(
    org: str, since_s: int, margin_s: int, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """The org's erasures that had not finished by `since_s` (`ledger.unfinished_since`)."""
    return await ledger.unfinished_since(org, since_s, margin_s, limit=limit)
