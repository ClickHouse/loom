"""One change at a time per grant, claimed deployment-wide.

`loom.namespace_grants` keeps the newest row per (org, namespace, subject), so
an op that reads a grant and then writes it would write over a change that
landed in between: an approval read at writer could undo a promotion to admin,
or a revoke. Each such op holds `grant_change` for that key from its read to its
write, as a lease (`server.core.leases`) every replica sees, with or without a
Keeper.

Claims nest in one order: a sign-up's name claim (`self_signup.name_claim`) or
a personal namespace's (`personal-ns:`), which never wait, so they may come first;
account claims (several sorted by org and namespace) or a service-account name's,
then the names of the tokens its former holder made (parent before child,
`hand_down_lineage`); then an address's sign-up claim (`self_signup.email_claim`);
then an org's collection tree or a SCIM group; then a subject's repository claim
(`maintainers.repo_change`), the repository's admins and the namespaces'
admins, or the trusts; then a bot or a grant (several sorted by namespace).
Taken the other way round, two changes wait each other out and both answer
503.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from server.core.ch import own_write_reads, statement_timeout
from server.core.errors import BackendUnavailableError, GrantBusyError
from server.core.leases import Lease, LeaseUnavailable, release_shielded, take, work_left

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


def account_change(
    org: str, namespace: str, *, wait_s: float | None = None, statement_s: float = _STATEMENT_S
) -> AbstractAsyncContextManager[None]:
    """Hold the registry row of (org, namespace) from a read to its write: a
    release and a password set each rewrite the whole row, so one must not
    write back what it read over the other. GrantBusyError once another change
    has held it for `wait_s` (`WAIT_S` by default, 0: at once); `statement_s` bounds each statement,
    lower for a caller inside a shorter claim of its own."""
    return _claimed(*_account(org, namespace), wait_s=wait_s, statement_s=statement_s)


def accounts_change(org: str, namespaces: list[str]) -> AbstractAsyncContextManager[None]:
    """`account_change` for several namespaces of `org`, taken sorted within one wait,
    then one bound on the change, ending inside the first claim's TTL."""
    names = sorted(set(namespaces))
    keys = [_account(org, ns) for ns in names]
    return _claimed_all(
        keys, f"the accounts {', '.join(names)} of {org}", wait_s=WAIT_S, statement_s=_STATEMENT_S
    )


def _account(org: str, namespace: str) -> tuple[str, str]:
    return "account:" + json.dumps([org, namespace]), f"the account {org}/{namespace}"


def group_change(org: str, group_id: str) -> AbstractAsyncContextManager[None]:
    """Hold one SCIM group from its read to its write: two creates of one id
    must not both find it free, nor two patches each drop the other's members."""
    return _claimed("group:" + json.dumps([org, group_id]), f"the group {group_id!r}")


def collections_change(org: str) -> AbstractAsyncContextManager[None]:
    """Hold the org's collection tree from the read that checks a move to its
    write: two moves at once could each pass the cycle check and together close
    a cycle."""
    return _claimed("collections:" + json.dumps(org), f"the collections of {org}")


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


def model_calls_change(org: str) -> AbstractAsyncContextManager[None]:
    """Hold the org's model-call switch from its read to its write: an org
    admin's change must not write back the lock a platform admin set since."""
    return _claimed("model_calls:" + json.dumps(org), f"the model-call switch of {org}")


def trusts_change() -> AbstractAsyncContextManager[None]:
    """Hold every workload trust while one is created: its checks against the
    others (names, tokens both could take) and its save are one step."""
    return _claimed("trusts", "the workload trusts")


def _claimed(
    name: str, what: str, *, wait_s: float | None = None, statement_s: float = _STATEMENT_S
) -> AbstractAsyncContextManager[None]:
    wait = WAIT_S if wait_s is None else wait_s  # read at call time, as a test may set it
    return _claimed_all([(name, what)], what, wait_s=wait, statement_s=statement_s)


@asynccontextmanager
async def _claimed_all(
    keys: list[tuple[str, str]],
    what: str,
    *,
    wait_s: float,
    statement_s: float,
) -> AsyncIterator[None]:
    """Hold each (claim, what it guards) of `keys` in order, all within `wait_s` and
    each claim's statements within `statement_s`, then bound the change by what is left
    of the first claim's work share, so no claim is held past its TTL."""
    held: list[Lease] = []
    deadline = time.monotonic() + wait_s
    try:
        for name, guarded in keys:
            left = deadline - time.monotonic()
            if held and left <= 0:  # the wait is spent: another attempt would outlast it
                raise GrantBusyError(f"{guarded} could not be claimed in time; retry shortly")
            try:
                with statement_timeout(statement_s):
                    lease = await take(
                        name, ttl_s=TTL_S, wait_s=max(0.0, left), poll_s=0.05, poll_max_s=0.8
                    )
            except LeaseUnavailable as e:
                raise NotClaimed(
                    f"changes to {guarded} cannot be claimed; retry shortly",
                    operation="grant_change",
                ) from e
            if lease is None:
                raise GrantBusyError(f"another change to {guarded} is running; retry shortly")
            held.append(lease)
        async with _bounded(what, statement_s, min(CHANGE_S, work_left(held[0], TTL_S))):
            yield
    finally:
        for lease in reversed(held):
            await release_shielded(lease)


@asynccontextmanager
async def _bounded(what: str, statement_s: float, change_s: float) -> AsyncIterator[None]:
    """The change itself: cut off at `change_s`, each statement at `statement_s`
    server-side, so it ends well before its claim may be taken over. Its reads
    are sequentially consistent: the last holder may have written on another
    replica."""
    try:
        if change_s <= 0:
            raise TimeoutError
        async with asyncio.timeout(change_s):
            with own_write_reads(), statement_timeout(statement_s):
                yield
    except TimeoutError as e:
        raise BackendUnavailableError(
            f"the change to {what} did not finish in time; retry shortly", operation="grant_change"
        ) from e
