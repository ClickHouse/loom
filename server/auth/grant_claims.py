"""One change at a time per grant, claimed in Keeper.

`loom.namespace_grants` keeps the newest row per (org, namespace, subject), so
an op that reads a grant and then writes it would write over a change that
landed in between: an approval read at writer could undo a promotion to admin,
or a revoke. Each such op holds `grant_change` for that key from its read to its
write. Without a Keeper the migration skipped the table and changes are kept
apart within this process only, as /v1/health says.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from server.core.ch import get_client
from server.core.errors import GrantBusyError
from server.core.keeper_claims import Claim, KeeperClaims

# A change reads and writes one grant row, so a live one is done in seconds; the
# rest is margin for a slow insert and the replicas' clocks.
CLAIMS = KeeperClaims("grant_claims", stale_s=60, claim_timeout_s=5, release_timeout_s=5)
# How long a change waits for the one ahead of it before answering 503.
WAIT_S = 10.0
_POLL_S = 0.05
# Without a Keeper: one lock per key, the idle ones dropped once many are tracked.
_local: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_MAX_TRACKED = 10_000


def _local_lock(key: str) -> asyncio.Lock:
    if len(_local) >= _MAX_TRACKED:
        for k in [k for k, lock in _local.items() if not lock.locked()]:
            del _local[k]
    return _local[key]


def claims_atomic() -> bool | None:
    """Whether grant changes are claimed in Keeper here; None = not asked yet."""
    return CLAIMS.atomic


@asynccontextmanager
async def grant_change(org: str, namespace: str, subject: str) -> AsyncIterator[None]:
    """Hold the grant row of (org, namespace, subject) from a read to its write."""
    key = json.dumps([org, namespace, subject])
    ch = get_client()
    deadline = time.monotonic() + WAIT_S
    while True:
        outcome, holder = await CLAIMS.claim(ch, key)
        if outcome is not Claim.TAKEN:
            break
        if time.monotonic() > deadline:
            raise GrantBusyError(
                f"another change to {subject!r} on {org}/{namespace} is running; retry shortly"
            )
        await asyncio.sleep(_POLL_S)
    if outcome is Claim.UNCLAIMED:
        async with _local_lock(key):
            yield
        return
    try:
        yield
    finally:
        await CLAIMS.release_shielded(ch, key, holder)
