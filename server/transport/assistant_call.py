"""Whether the `tools/call` being served came through the assistant toolset.

Set once per call when its tool is resolved (`mcp_call._resolve_connection_tool`) and read
by the phases that behave differently on that connection: the session-run stamp and the
result shape. A connection property, never the self-reported `consumer` argument, so a
caller on another toolset cannot claim it.
"""

from __future__ import annotations

from contextvars import ContextVar

_on_assistant: ContextVar[bool] = ContextVar("loom_assistant_call", default=False)


def set_assistant_call(value: bool) -> None:
    _on_assistant.set(value)


def assistant_call() -> bool:
    """True while serving a call that came through the assistant toolset."""
    return _on_assistant.get()
