"""One change at a time per grant, claimed deployment-wide.

`loom.namespace_grants` keeps the newest row per (org, namespace, subject), so
an op that reads a grant and then writes it would write over a change that
landed in between: an approval read at writer could undo a promotion to admin,
or a revoke. Each such op holds `grant_change` for that key from its read to its
write, as a lease (`server.core.leases`) every replica sees, with or without a
Keeper.

Claims nest in one order: account claims first (several sorted by org and
namespace) or a service-account name's; then a subject's repository claim
(`maintainers.repo_change`), the repository's admins and the namespaces'
admins, or the trusts; then a bot or a grant. Taken the other way round, two
changes wait each other out and both answer 503.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from server.core.ch import own_write_reads, statement_timeout
from server.core.errors import BackendUnavailableError, GrantBusyError
from server.core.leases import LeaseUnavailable, release_shielded, take

# A change reads and writes one grant row, so a live one is done in seconds; the
# rest is margin for a slow insert and the replicas' clocks.
TTL_S = 60
# How long a change waits for the one ahead of it before answering 503.
WAIT_S = 10.0
# How long a change may run, and each of its statements: well inside the TTL,
# after which its claim is taken over.
CHANGE_S = 20.0
_STATEMENT_S = 10


class NotClaimed(BackendUnavailableError):
    """The claim could not be taken: the change did not start."""


def grant_change(org: str, namespace: str, subject: str) -> AbstractAsyncContextManager[None]:
    """Hold the grant row of (org, namespace, subject) from a read to its write."""
    return _claimed(
        "grant:" + json.dumps([org, namespace, subject]), f"{subject!r} on {org}/{namespace}"
    )


def admins_change(org: str, namespace: str) -> AbstractAsyncContextManager[None]:
    """Hold the set of admins of `namespace` (or of a repository's branches, as
    `repo:<repo>`). A change that may take admin from someone takes it before the
    subject's own claim and counts the admins under it, so two such changes at
    once cannot both see another admin left. Its key has no subject, so no
    grant's key is the same."""
    return _claimed("grant:" + json.dumps([org, namespace]), f"the admins of {org}/{namespace}")


def account_change(org: str, namespace: str) -> AbstractAsyncContextManager[None]:
    """Hold the registry row of (org, namespace) from a read to its write: a
    release and a password set each rewrite the whole row, so one must not
    write back what it read over the other."""
    return _claimed("account:" + json.dumps([org, namespace]), f"the account {org}/{namespace}")


def service_account_change(org: str, name: str) -> AbstractAsyncContextManager[None]:
    """Hold a person's service-account name in `org` from the check that it is
    free to its row: a second create with the name would replace the first
    row, and its token, still live, would be listed nowhere."""
    return _claimed("service_account:" + json.dumps([org, name]), f"the token name {name!r}")


def bot_change(name: str) -> AbstractAsyncContextManager[None]:
    """Hold the bot `name` from the check that it exists to its save: two
    creates at once must not both find it free, the second key replacing the
    first one's."""
    return _claimed("bot:" + json.dumps(name), f"the bot {name}")


def trusts_change() -> AbstractAsyncContextManager[None]:
    """Hold every workload trust while one is created: its checks against the
    others (names, tokens both could take) and its save are one step."""
    return _claimed("trusts", "the workload trusts")


@asynccontextmanager
async def _claimed(name: str, what: str) -> AsyncIterator[None]:
    try:
        lease = await take(name, ttl_s=TTL_S, wait_s=WAIT_S, poll_s=0.05, poll_max_s=0.8)
    except LeaseUnavailable as e:
        raise NotClaimed(
            f"changes to {what} cannot be claimed; retry shortly", operation="grant_change"
        ) from e
    if lease is None:
        raise GrantBusyError(f"another change to {what} is running; retry shortly")
    try:
        async with _bounded(what):
            yield
    finally:
        await release_shielded(lease)


@asynccontextmanager
async def _bounded(what: str) -> AsyncIterator[None]:
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
            f"the change to {what} did not finish in time; retry shortly", operation="grant_change"
        ) from e
