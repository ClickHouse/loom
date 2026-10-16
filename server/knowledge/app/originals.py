"""A namespace's originals claim, held by `loom blobs import` and by erasure.

The import re-reads a document and stores its original under the claim, and the
erasure step deletes a namespace's originals and documents under it, so an
erasure that finished after the import listed a document is seen, not undone.
The claim is renewed while held: the erasure step waits on mutations that can
outlast its TTL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from server.core import leases
from server.core.errors import BackendUnavailableError

CLAIM_TTL_S = 900
_POLL_S = 1.0


@asynccontextmanager
async def hold_originals(
    org: str, namespaces: Iterable[str], *, wait_s: float, ch: Any = None
) -> AsyncIterator[Callable[[], bool]]:
    """Hold the namespaces' originals claims for the block, waiting up to
    `wait_s` for another holder; BackendUnavailableError past it. The block is
    cancelled, and LeaseLost raised, once a claim is lost. Yields `held()`, the
    fence a writer checks before each write: a holder paused past the TTL sees
    it False before another can take the claim."""
    async with AsyncExitStack() as stack:
        held: list[leases.Lease] = []
        for ns in sorted(set(namespaces)):
            lease = await _take(f"document_originals:{org}/{ns}", wait_s, ch)
            held.append(await stack.enter_async_context(lease.renewed()))
        yield lambda: all(lease.held for lease in held)


async def _take(name: str, wait_s: float, ch: Any) -> leases.Lease:
    lease = await leases.take(
        name, ttl_s=CLAIM_TTL_S, wait_s=wait_s, poll_s=_POLL_S, poll_max_s=_POLL_S, ch=ch
    )
    if lease is None:
        raise BackendUnavailableError(f"{name} is held; retry shortly", operation="claim")
    return lease
