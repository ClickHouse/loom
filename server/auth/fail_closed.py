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


def note_unreadable() -> None:
    """A read the current check needs failed, and it carries on as if the
    read found nothing."""
    _unreadable.set(True)


def unreadable() -> bool:
    return _unreadable.get()


def marks_unverified[**P](check: Callable[P, Awaitable[None]]) -> Callable[P, Awaitable[None]]:
    """A PermissionError `check` raises after `note_unreadable` gets
    `unverified = True`."""

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
            _unreadable.reset(token)

    return checked
