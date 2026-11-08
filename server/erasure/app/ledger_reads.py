"""What other planes read of the erasure ledger: the erasures still open in an org, which
the knowledge plane's move of originals to the bucket is fenced by."""

from __future__ import annotations

from typing import Any

from server.erasure.store import ledger


async def unfinished_since(
    org: str, since_s: int, margin_s: int, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """The org's erasures that had not finished by `since_s` (`ledger.unfinished_since`)."""
    return await ledger.unfinished_since(org, since_s, margin_s, limit=limit)
