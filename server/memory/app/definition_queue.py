"""Serialize private candidate versions with namespace erasure."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from server.core import lease_writes
from server.core.datetimes import as_aware_utc
from server.core.errors import ValidationError
from server.erasure.app import ledger_reads
from server.erasure.domain.scopes import may_cover
from server.memory.domain.definitions import DEFINITION_DRAFT_KINDS
from server.memory.store import learnings


async def persist(row: dict[str, Any]) -> None:
    if row.get("source_kind") not in DEFINITION_DRAFT_KINDS:
        await learnings.insert_row(row)
        return
    async with writing(row) as uncertain:
        uncertain(True)
        await learnings.insert_row(row)
        uncertain(False)


@asynccontextmanager
async def writing(row: dict[str, Any]) -> AsyncIterator[Callable[[bool], None]]:
    """Check the original evidence before any publication or queue side effect."""
    proposed_at = as_aware_utc(row.get("proposed_at"))
    if proposed_at is None:
        raise ValidationError("a private candidate must retain its original proposal time")
    org, namespace = str(row["org"]), str(row["namespace"])
    async with lease_writes.fenced(f"definition.writer/{org}") as uncertain:
        # A candidate read before deletion can reach this lease after erasure completes.
        erasures = await ledger_reads.unfinished_reaching(
            org, namespace, None, int(proposed_at.timestamp()), ledger_reads.LEDGER_CLOCK_MARGIN_S
        )
        if any(may_cover(str(r["scope_type"]), str(r["scope_value"]), namespace) for r in erasures):
            raise ValidationError("the candidate evidence may have been erased")
        yield uncertain
