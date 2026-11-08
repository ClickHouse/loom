"""What other planes read of the erasure registry and ledger: the tables a namespace's
rows live in, which the in-use check of a new name reads; the erasures still open in an
org, which the knowledge plane's move of originals to the bucket is fenced by; and those
that may reach one namespace's rows, which a document restore checks."""

from __future__ import annotations

from typing import Any

from server.erasure.api.erasure import parse_purge_flag
from server.erasure.store import census, ledger, steps_pg

LEDGER_CLOCK_MARGIN_S = 60  # not a setting: a bound on clock skew, answered conservatively


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


async def unfinished_reaching(
    org: str, namespace: str, user_id: str | None, since_s: int, margin_s: int
) -> list[dict[str, Any]]:
    """The org's unfinished erasures that may reach `namespace` (`ledger.unfinished_reaching`)."""
    return await ledger.unfinished_reaching(org, namespace, user_id, since_s, margin_s)


def purges(row: dict[str, Any]) -> bool:
    """Whether the saga of this ledger row deletes rather than tombstones, read as the saga
    reads it: a user's from the column, a namespace list's from its scope, a namespace's never."""
    if row["scope_type"] == "user_id":
        return bool(row["purge"])
    if row["scope_type"] == "namespace_list":
        return parse_purge_flag(str(row["scope_value"]), default=bool(row["purge"]))
    return False
