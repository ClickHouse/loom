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

_unreadable: ContextVar[bool] = ContextVar("authz_unreadable", default=False)


# Set for the rest of a request that answered as if refused because a read
# could not be made (`row_in_scope`'s found=False): its row is the server's.
_answered_unverified: ContextVar[bool] = ContextVar("authz_answered_unverified", default=False)


def note_answered_unverified() -> None:
    _answered_unverified.set(True)


def answered_unverified() -> bool:
    return _answered_unverified.get()


def begin_request() -> None:
    """A request starts with nothing answered unverified."""
    _answered_unverified.set(False)


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
