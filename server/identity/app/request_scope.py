"""The current request's read scope. It reads the request's auth context, which
the identity domain layer may not import, so it lives in app."""

from __future__ import annotations

from server.identity.domain.read_scope import (
    ReadFilters,
    request_read_filters,
    request_read_narrowed,
)


def request_scope() -> ReadFilters:
    """The read scope of the current request: a token bounded to one user (or
    session) never reads, resolves, changes or learns about another's row,
    whatever agent or identity the body names. The whole namespace with no auth
    and no scope bound (auth off, workers)."""
    # Lazy: server.auth.context pulls in authz → operations → handlers, and
    # the handlers import this module at load time.
    from server.auth.context import current_auth

    return request_read_filters(current_auth())


def request_narrowed() -> bool:
    """Whether the current request reads below the whole namespace (a scope
    that does not resolve counts as narrowed)."""
    from server.auth.context import current_auth

    return request_read_narrowed(current_auth())
