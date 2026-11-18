"""A client's transport promise to stop automatic capture after mirrored reads."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from server.auth.answer_tokens import is_answer_token

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo


_declared: ContextVar[bool] = ContextVar("loom_session_protection", default=False)
_HEADER = b"x-loom-session-protection"


@contextmanager
def request_declaration(scope: Mapping[str, Any]) -> Iterator[None]:
    """Keep one declaration through middleware tasks, MCP entries and streaming."""
    values = [value for name, value in scope.get("headers", ()) if name.lower() == _HEADER]
    token = _declared.set(values == [b"v1"])
    try:
        yield
    finally:
        _declared.reset(token)


def protected_for(auth: AuthInfo | None) -> bool:
    """An answer uses its fixed caller promise, never its own connection's header."""
    if auth is not None and is_answer_token(auth):
        return auth.session_protection
    return _declared.get()
