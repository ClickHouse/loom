"""Whether the `tools/call` being served came through the assistant toolset.

Set once per call when its tool is resolved (`mcp_call._resolve_connection_tool`) and
read by the phases that behave differently on that connection: the namespace and agent
defaults, the session-run stamp and the result shape. A connection property, never the
self-reported `consumer` argument, so a caller on another toolset cannot claim it.
"""

from __future__ import annotations

from contextvars import ContextVar

_on_assistant: ContextVar[bool] = ContextVar("loom_assistant_call", default=False)
# The answer an assistant connection's request belongs to: its `X-Loom-Run-Id`, which
# the agent's MCP server sends with the answer's message id.
_answer_run: ContextVar[str] = ContextVar("loom_assistant_answer_run", default="")


def set_assistant_call(value: bool) -> None:
    _on_assistant.set(value)


def assistant_call() -> bool:
    """True while serving a call that came through the assistant toolset."""
    return _on_assistant.get()


def set_answer_run(value: str) -> None:
    _answer_run.set(value)


def answer_run() -> str:
    """The answer id an assistant connection's request carries ('' for none)."""
    return _answer_run.get()
