"""The MCP connection names the checkout's code namespace.

The plugin's headers helper runs in the project directory and sends
``X-Loom-Code-Scope: <org>/<namespace>`` — the repository the checkout
resolved to (the shunt's cache, else one lookup). The transport publishes it
on a per-request ContextVar; ``tools/call`` applies it to every ``code.*``
call that names no namespace, so the model does not have to remember the
session primer's repository line. Explicit arguments win, and authorization
then runs on the named namespace exactly as it would for an explicit one — a header
naming a namespace the token cannot read is refused, never widened. The
ContextVar lives below the transport because loom_search's repository gate
(code/app/repo_scope) reads it too.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from typing import Any

from server.core.naming import present

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCOPE: ContextVar[tuple[str, str] | None] = ContextVar("loom_code_scope", default=None)


def parse_code_scope(value: object) -> tuple[str, str] | None:
    """``org/namespace`` → ``(org, namespace)``; None for anything else
    (missing, no slash, empty parts, characters outside [A-Za-z0-9._-])."""
    text = str(value or "").strip()
    if "/" not in text:
        return None
    org, namespace = text.split("/", 1)
    if not _PART.match(org) or not _PART.match(namespace):
        return None
    return org, namespace


def set_code_scope(value: object) -> Token[tuple[str, str] | None]:
    """Publish the request's scope (parsed; None when absent or malformed)."""
    return _SCOPE.set(parse_code_scope(value))


def reset_code_scope(token: Token[tuple[str, str] | None]) -> None:
    _SCOPE.reset(token)


def current_code_scope() -> tuple[str, str] | None:
    return _SCOPE.get()


def apply_code_scope(
    op_name: str, arguments: dict[str, Any], scope: tuple[str, str] | None
) -> bool:
    """Fill ``namespace`` (and ``org`` when absent) on a ``code.*`` call that
    names no namespace (missing, blank or "-"), the ``code.repo.*`` writes
    included: the scope names the checkout's repository. Returns True when the
    scope was applied. Other ops and explicit namespaces are untouched."""
    if not scope or not op_name.startswith("code.") or present(arguments.get("namespace")):
        return False
    org, namespace = scope
    arguments["namespace"] = namespace
    if not present(arguments.get("org")):
        arguments["org"] = org
    return True
