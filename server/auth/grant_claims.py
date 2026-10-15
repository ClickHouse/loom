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
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from server.core.ch import get_client, own_write_reads, statement_timeout
from server.core.errors import BackendUnavailableError, GrantBusyError
from server.core.keeper_claims import Claim, KeeperClaims
from server.core.keyed_locks import KeyedLocks

# A change reads and writes one grant row, so a live one is done in seconds; the
# rest is margin for a slow insert and the replicas' clocks.
CLAIMS = KeeperClaims("grant_claims", stale_s=60, claim_timeout_s=5, release_timeout_s=5)
# How long a change waits for the one ahead of it before answering 503.
WAIT_S = 10.0
# How long a change may run, and each of its statements: well inside the 60 s
# after which its claim is taken over.
CHANGE_S = 20.0
_STATEMENT_S = 10
# The first wait for a claim held elsewhere, doubled up to `_POLL_MAX_S`.
_POLL_S = 0.05
_POLL_MAX_S = 0.8
# The subject `admins_change` claims under: no grant row has an empty subject.
ADMINS = ""
# Without a Keeper: changes of one key are kept apart within this process.
_local = KeyedLocks()


def claims_atomic() -> bool | None:
    """Whether grant changes are claimed in Keeper here; None = not asked yet."""
    return CLAIMS.atomic


@asynccontextmanager
async def grant_change(org: str, namespace: str, subject: str) -> AsyncIterator[None]:
    """Hold the grant row of (org, namespace, subject) from a read to its write."""
    key = json.dumps([org, namespace, subject])
    ch = get_client()
    deadline = time.monotonic() + WAIT_S
    poll = _POLL_S
    while True:
        try:
            outcome, holder = await CLAIMS.claim(ch, key)
        except Exception as e:  # a Keeper that cannot answer refuses retryably
            raise BackendUnavailableError(
                "grant changes cannot be claimed; retry shortly", operation="grant_change"
            ) from e
        if outcome is not Claim.TAKEN:
            break
        if time.monotonic() > deadline:
            raise _busy(org, namespace, subject)
        await asyncio.sleep(poll)
        poll = min(poll * 2, _POLL_MAX_S)
    if outcome is Claim.UNCLAIMED:
        async with AsyncExitStack() as held:
            try:
                await held.enter_async_context(
                    _local.hold(key, wait_s=max(0.0, deadline - time.monotonic()))
                )
            except TimeoutError as e:
                raise _busy(org, namespace, subject) from e
            await held.enter_async_context(_bounded())
            yield
        return
    try:
        async with _bounded():
            yield
    finally:
        await CLAIMS.release_shielded(ch, key, holder)


def admins_change(org: str, namespace: str) -> AbstractAsyncContextManager[None]:
    """Hold the namespace's set of admins. A change that may remove an admin
    takes it before the subject's own claim and counts the admins under it, so
    two removals at once cannot both see another admin left."""
    return grant_change(org, namespace, ADMINS)


def _busy(org: str, namespace: str, subject: str) -> GrantBusyError:
    return GrantBusyError(
        f"another change to {subject!r} on {org}/{namespace} is running; retry shortly"
    )


@asynccontextmanager
async def _bounded() -> AsyncIterator[None]:
    """The change itself: cut off at `CHANGE_S`, each statement at `_STATEMENT_S`
    server-side, so it ends well before its claim may be taken over. Its reads
    are sequentially consistent: the last holder may have written on another
    replica."""
    try:
        async with asyncio.timeout(CHANGE_S):
            with own_write_reads(), statement_timeout(_STATEMENT_S):
                yield
    except TimeoutError as e:
        raise BackendUnavailableError(
            "the grant change did not finish in time; retry shortly", operation="grant_change"
        ) from e
