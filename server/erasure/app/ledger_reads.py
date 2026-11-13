"""What other planes read of the erasure registry and ledger: the tables a namespace's
rows live in, which the in-use check of a new name reads; the erasures still open in an
org, which the knowledge plane's move of originals to the bucket is fenced by; those
that may reach one namespace's rows, which a document restore checks; and the namespace
erasures that will still run, which a document source's add waits for."""

from __future__ import annotations

from typing import Any

from server.erasure.domain.scopes import applied_purge
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


async def open_namespace_erasures(org: str) -> list[dict[str, Any]]:
    """The org's namespace erasures that will still run (`ledger.open_namespace_erasures`):
    a source add waits for them."""
    return await ledger.open_namespace_erasures(org)


async def unfinished_reaching(
    org: str,
    namespace: str,
    user_id: str | None,
    since_s: int,
    margin_s: int,
    *,
    skip_abandoned: bool = False,
) -> list[dict[str, Any]]:
    """The org's unfinished erasures that may reach `namespace` (`ledger.unfinished_reaching`)."""
    return await ledger.unfinished_reaching(
        org, namespace, user_id, since_s, margin_s, skip_abandoned=skip_abandoned
    )


def purges(row: dict[str, Any]) -> bool:
    """Whether the saga of this ledger row deletes rather than tombstones, as it runs."""
    return applied_purge(str(row["scope_type"]), str(row["scope_value"]), bool(row["purge"]))
