"""Refusals made because what decides them could not be read.

A check that cannot read a grant, an account or a namespace classification
refuses (fail closed). The caller still gets its refusal, but the ledger
counts it as the server's failure (`server.observability.sli.status_class`),
or an outage of those reads would leave the SLOs green.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from server.core import request_work

_unreadable: ContextVar[bool] = ContextVar("authz_unreadable", default=False)


def note_answered_unverified() -> None:
    """The request answered as if refused because a read the check needed
    failed (`row_in_scope`'s found=False): its row is the server's. Kept on
    the request's work bag, which the tasks a handler gathers share, rather
    than in a ContextVar they would copy."""
    bag = request_work.current()
    if bag is not None:
        bag.answered_unverified = True


def answered_unverified() -> bool:
    bag = request_work.current()
    return bag is not None and bag.answered_unverified


def reset() -> None:
    """A request starts with no failed read noted (a batch's earlier entry
    may have left one set)."""
    _unreadable.set(False)


def note_unreadable() -> None:
    """A read the current check needs failed, and it carries on as if the
    read found nothing."""
    _unreadable.set(True)


def unreadable() -> bool:
    return _unreadable.get()


def marks_unverified[**P](check: Callable[P, Awaitable[None]]) -> Callable[P, Awaitable[None]]:
    """A PermissionError `check` raises after `note_unreadable` gets
    `unverified = True`. A failed read stays noted for the enclosing check,
    which may refuse on its own account after this one returned."""

    @functools.wraps(check)
    async def checked(*args: P.args, **kwargs: P.kwargs) -> None:
        token = _unreadable.set(False)
        try:
            await check(*args, **kwargs)
        except PermissionError as e:
            if _unreadable.get():
                setattr(e, "unverified", True)  # noqa: B010 — PermissionError declares no field
            raise
        finally:
            if not _unreadable.get():
                _unreadable.reset(token)

    return checked


def refused_unverified() -> bool:
    """Whether a read the refusing check needed failed since the last `reset()`;
    if so, the request is marked the server's."""
    failed = unreadable()
    if failed:
        note_answered_unverified()
    return failed
