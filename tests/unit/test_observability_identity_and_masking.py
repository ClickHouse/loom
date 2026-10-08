"""Identity, erasability and masking.

Each of these pins a defect that would be silent: a dead join column, a
transport session that never reaches telemetry, two spellings of "anonymous", and
two backends that disagree about whether user content may leave the process.
"""

from __future__ import annotations

from typing import Any

import pytest


def test_anonymous_actor_has_exactly_one_spelling() -> None:
    """HTTP and MCP both say "anonymous".

    A second spelling would split unauthenticated traffic across two identities
    in every by-user view, and the split would encode the transport — which the
    `loom.transport` label already carries.
    """
    import ast
    from pathlib import Path

    from server.transport._common import ANONYMOUS_ACTOR

    assert ANONYMOUS_ACTOR == "anonymous"
    # AST, not a text search: a comment may name the other spelling, and a text search cannot tell a value from prose.
    root = Path(__file__).resolve().parents[2] / "server"
    offenders: list[str] = []
    for p in root.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Constant) and node.value == "mcp-anonymous":
                offenders.append(f"{p.relative_to(root).as_posix()}:{node.lineno}")
    assert not offenders, f"the second spelling is back in: {offenders}"


def test_agent_events_carries_the_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """`agent_events.trace_id` is set.

    It is the join key between loom's own cost/latency rows and the trace in
    ClickStack and Langfuse — the same 32-char hex Langfuse uses as its `traceId`.
    A row carrying '' cannot be joined.
    """
    from server.observability import ledger

    captured: list[Any] = []

    class _Emitter:
        def emit(self, ev: Any) -> None:
            captured.append(ev)

    monkeypatch.setattr(ledger, "get_emitter", lambda: _Emitter())

    class _Ctx:
        trace_id = 0x0123456789ABCDEF0123456789ABCDEF
        is_valid = True

    class _Span:
        def get_span_context(self) -> Any:
            return _Ctx()

    import opentelemetry.trace as _t

    monkeypatch.setattr(_t, "get_current_span", lambda: _Span())

    ledger.emit_agent_event(
        op_name="memory.search",
        identity={
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "u",
            "session_id": "s",
        },
        request_id="r",
        latency_ms=5,
        success=True,
        error_code="",
    )
    assert captured, "no event emitted"
    assert captured[0].trace_id == "0123456789abcdef0123456789abcdef"


def test_missing_trace_context_still_emits_the_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry enrichment must never cost us the billing row.

    `agent_events` feeds cost reporting; dropping an event because no span was
    active would lose real money data to an observability nicety.
    """
    from server.observability import ledger

    captured: list[Any] = []

    class _Emitter:
        def emit(self, ev: Any) -> None:
            captured.append(ev)

    monkeypatch.setattr(ledger, "get_emitter", lambda: _Emitter())

    import opentelemetry.trace as _t

    def _boom() -> Any:
        raise RuntimeError("no otel")

    monkeypatch.setattr(_t, "get_current_span", _boom)
    ledger.emit_agent_event(
        op_name="memory.get",
        identity={
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "u",
            "session_id": "s",
        },
        request_id="r",
        latency_ms=1,
        success=True,
        error_code="",
    )
    assert captured and captured[0].trace_id == ""


def test_mcp_session_id_is_the_fallback_session() -> None:
    """MCP traces carry a session even when the payload names none.

    `Mcp-Session-Id` (generated and cached to resolve the client name) reaches
    the observability layer, so an agent's whole conversation over MCP is
    navigable. The payload `session_id` still wins:
    that is the CONVERSATION, a loom concept that scopes memory, and is what an
    operator means. The transport session is the fallback.
    """
    from server.transport.mcp_outcome import _span_identity

    class _Op:
        name = "memory.search"

    class _In:
        org = "o"
        namespace = "n"
        agent = "a"
        user_id = "u"
        session_id = "-"  # the unset sentinel

    ident = _span_identity(_Op(), _In(), None, "req-1", "mcp-sess-abc")
    assert ident["session_id"] == "mcp-sess-abc"

    class _InWithSession(_In):
        session_id = "real-conversation"

    ident2 = _span_identity(_Op(), _InWithSession(), None, "req-2", "mcp-sess-abc")
    assert ident2["session_id"] == "real-conversation", (
        "the payload session is the conversation and must win over the transport connection id"
    )


def test_sentinel_session_never_becomes_a_literal_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "-" is loom's unset marker, not a session named "-"."""
    from server.transport.mcp_outcome import _span_identity

    class _Op:
        name = "memory.get"

    class _In:
        org = "o"
        namespace = "n"
        agent = "a"
        user_id = "u"
        session_id = "-"

    assert _span_identity(_Op(), _In(), None, "r", "")["session_id"] is None


def test_mcp_sets_span_identity_before_running_the_handler() -> None:
    """Provider spans are created INSIDE the handler.

    `set_loom_attributes` is the only caller of `genai.set_identity`, so calling it
    after `await op.handler(...)` would leave every `llm.*` and `embed.*` span of
    an MCP request with no loom.org/namespace/operation and no Langfuse session or
    user, on the transport that matters most (Claude Desktop, Cursor, Claude Code).

    Worse in a JSON-RPC batch: the ContextVar is task-scoped, so entry N's provider
    spans would be stamped with entry N-1's tenant — one tenant's LLM spend
    attributed to another.

    Asserted structurally because there is no unit-level seam here: the ordering
    is the whole contract, and a refactor that moves the call down would silently
    break it.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "server" / "transport" / "mcp_call.py").read_text()
    body = src.split("async def _call_tool(", 1)[1]
    handler_at = body.index("result = await op.handler(input_obj)")
    before = body[:handler_at]
    assert "set_loom_attributes(" in before, (
        "_call_tool must set span identity BEFORE awaiting the handler, or the "
        "provider spans created inside it are unattributed"
    )
    assert "_span_identity(" in before and "org=rl_org, namespace=rl_ns" in before
