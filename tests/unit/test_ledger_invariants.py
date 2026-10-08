"""A paid `agent_events` row names its model on BOTH transports, and the
scope knobs a read was made with ride the args map.

Without them, spend cannot be attributed to a model or a read scope. These
tests assert the invariant through the same two emit paths production uses:
`observability.ledger.emit_agent_event` (HTTP) and
`transport.mcp_tools._emit_mcp_event` (MCP)."""

from __future__ import annotations

from typing import Any

import pytest

from server.observability import cost_accumulator as _cost
from server.observability import ledger
from server.observability.events import AgentEvent


class _Emitter:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


@pytest.fixture
def emitter(monkeypatch: pytest.MonkeyPatch) -> _Emitter:
    em = _Emitter()
    monkeypatch.setattr(ledger, "get_emitter", lambda: em)

    return em


_ASK_ARGS: dict[str, Any] = {
    "org": "acme",
    "namespace": "team",
    "agent": "bot",
    "user_id": "u1",
    "session_id": "s1",
    "query": "what did we decide about the cache",
    "top_k": 30,
    "read_scope": "namespace",
    "decompose": "auto",
    "world_knowledge": "forbid",
}


def _paid_ask() -> None:
    acc = _cost.begin_op()
    acc.add_llm("gpt-4.1-mini", 1200, 150)
    acc.add_embed("text-embedding-3-large", 40)


class _Result:
    def model_dump(self) -> dict[str, Any]:
        return {
            "answer": "we chose an LRU",
            "source": "synthesized",
            "abstain": False,
            "abstain_reason": "",
            "route_tier": "cheap",
            "grounding": "memory",
        }


def test_http_row_names_model_and_scope(emitter: _Emitter) -> None:
    _paid_ask()
    ledger.emit_agent_event(
        op_name="memory.ask",
        identity={k: _ASK_ARGS[k] for k in ("org", "namespace", "agent", "user_id", "session_id")},
        request_id="r1",
        latency_ms=12,
        success=True,
        error_code="",
        raw_args=_ASK_ARGS,
        result=_Result(),
    )
    (ev,) = emitter.events
    assert ev.upstream_cost_usd > 0
    assert ev.model == "gpt-4.1-mini" and ev.llm_calls == 1
    assert ev.args["read_scope"] == "namespace"
    assert ev.args["top_k"] == "30"
    assert ev.args["decompose"] == "auto"
    assert ev.args["world_knowledge"] == "forbid"
    assert ev.args["out_source"] == "synthesized"
    assert ev.args["out_abstain"] == "false"
    assert ev.args["out_grounding"] == "memory"
    assert ev.args["out_route_tier"] == "cheap"
    # the question itself never lands on the row
    assert ev.args["query"].startswith("sha256:")


def test_mcp_row_is_the_same_row(emitter: _Emitter) -> None:
    from server.transport.mcp_tools import _emit_mcp_event

    _paid_ask()
    _emit_mcp_event(
        "memory.ask",
        dict(_ASK_ARGS),
        15,
        True,
        "",
        "mcp-r2",
        result=_Result(),
    )
    (ev,) = emitter.events
    assert ev.operation == "memory.ask" and ev.agent == "bot"
    assert ev.upstream_cost_usd > 0
    assert ev.model == "gpt-4.1-mini" and ev.llm_calls == 1
    assert ev.args["read_scope"] == "namespace" and ev.args["out_source"] == "synthesized"


def test_unpaid_row_has_no_model_and_that_is_the_invariant(emitter: _Emitter) -> None:
    """The alert `upstream_cost_usd > 0 AND model = ''` must be silent for
    an abstain that made no LLM call: cost 0, model ''."""
    acc = _cost.begin_op()
    acc.add_embed("text-embedding-3-large", 40)
    ledger.emit_agent_event(
        op_name="memory.ask",
        identity={k: _ASK_ARGS[k] for k in ("org", "namespace", "agent", "user_id", "session_id")},
        request_id="r3",
        latency_ms=5,
        success=True,
        error_code="",
        raw_args=_ASK_ARGS,
        result={"source": "abstained", "abstain": True, "abstain_reason": "no memories matched"},
    )
    (ev,) = emitter.events
    assert ev.model == "" and ev.llm_calls == 0
    assert ev.args["out_abstain"] == "true"
    assert "out_abstain_reason" not in ev.args  # free text stays off the row
    # embeddings are priced: the row is not free, but the model column is
    # for LLM attribution and stays empty — the alert must therefore test
    # llm_calls > 0, not cost > 0, when embeddings are priced.
    assert ev.embedding_tokens == 40


def test_sweep_pass_leaves_a_worker_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.expiry import ExpiryResult, emit_sweep_event
    from server.observability import events as ev_mod

    em = _Emitter()
    monkeypatch.setattr(ev_mod, "get_emitter", lambda: em)
    res = ExpiryResult(scanned=12, tombstoned=7, demoted=3, anonymized=1, locked_skipped=1)
    emit_sweep_event(res, latency_ms=88)
    (ev,) = em.events
    assert ev.kind == "worker" and ev.operation == "memory.sweep_expired"
    assert ev.success and ev.error_code == ""
    assert ev.args["scanned"] == "12" and ev.args["tombstoned"] == "7"
    assert ev.args["demoted"] == "3" and ev.args["anonymized"] == "1"
    assert ev.args["locked_skipped"] == "1" and ev.args["failed"] == "0"
    assert ev.args["dry_run"] == "false" and ev.args["source"] == "worker"

    em.events.clear()
    emit_sweep_event(ExpiryResult(scanned=2, failures=["boom"]), latency_ms=3)
    (ev,) = em.events
    assert not ev.success and ev.error_code == "PARTIAL" and ev.args["failed"] == "1"


# --- an error row names its exception ---------------------------------------


def test_error_row_names_its_exception_bounded(emitter: _Emitter) -> None:
    _paid_ask()
    boom = KeyError("status")
    ledger.emit_agent_event(
        op_name="memory.set_from_messages",
        identity={k: _ASK_ARGS[k] for k in ("org", "namespace", "agent", "user_id", "session_id")},
        request_id="r2",
        latency_ms=9000,
        success=False,
        error_code="INTERNAL_ERROR",
        raw_args=_ASK_ARGS,
        error=boom,
    )
    (ev,) = emitter.events
    assert ev.args["error_class"] == "KeyError"
    # the quoted span is redacted: the key name a caller sent never lands on the row
    assert ev.args["error"] == "'…'"
    assert ev.success is False and ev.error_code == "INTERNAL_ERROR"


def test_error_message_is_first_line_redacted_and_bounded() -> None:
    from server.observability.event_args import bounded_error

    cls, msg = bounded_error(ValueError('bad value "Lisbon 2021" for key\nsecond line'))
    assert cls == "ValueError" and msg == "bad value '…' for key"
    cls, msg = bounded_error(RuntimeError("x" * 500))
    assert cls == "RuntimeError" and len(msg) == 200 and msg.endswith("…")
    assert bounded_error("plain text") == ("str", "plain text")


def test_identifier_errors_keep_their_identifiers_and_nothing_else() -> None:
    """The quoted span of a NameError / UnboundLocalError /
    ImportError is a Python name, not caller text; redacting it would make the
    row useless (an UnboundLocalError reading `'…'`)."""
    from server.observability.event_args import bounded_error

    cls, msg = bounded_error(
        UnboundLocalError("cannot access local variable 'summary_hits' where it is not associated")
    )
    assert cls == "UnboundLocalError" and "'summary_hits'" in msg
    cls, msg = bounded_error(ImportError("cannot import name 'fetch_x' from 'server.memory.app.y'"))
    assert msg == "cannot import name 'fetch_x' from 'server.memory.app.y'"
    # a quoted span that is not identifier-shaped is still redacted, even on these classes
    cls, msg = bounded_error(AttributeError("'Lisbon 2021' object has no attribute 'foo'"))
    assert msg == "'…' object has no attribute 'foo'"
    # other classes keep the full redaction
    cls, msg = bounded_error(KeyError("user_secret"))
    assert msg == "'…'"


def test_success_row_carries_no_error_keys(emitter: _Emitter) -> None:
    ledger.emit_agent_event(
        op_name="memory.get",
        identity={k: _ASK_ARGS[k] for k in ("org", "namespace", "agent", "user_id", "session_id")},
        request_id="r3",
        latency_ms=5,
        success=True,
        error_code="",
        raw_args=_ASK_ARGS,
        error=RuntimeError("ignored on success"),
    )
    (ev,) = emitter.events
    assert "error_class" not in ev.args and "error" not in ev.args


def test_paid_error_reaches_the_counter(emitter: _Emitter, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import metrics

    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        metrics,
        "record_paid_error",
        lambda op, *, namespace, error_code: seen.append((op, namespace, error_code)),
    )
    _paid_ask()
    ledger.emit_agent_event(
        op_name="memory.set_from_messages",
        identity={k: _ASK_ARGS[k] for k in ("org", "namespace", "agent", "user_id", "session_id")},
        request_id="r4",
        latency_ms=9000,
        success=False,
        error_code="RATE_LIMITED",
        raw_args=_ASK_ARGS,
    )
    assert seen == [("memory.set_from_messages", "team", "RATE_LIMITED")]
    # an unpaid failure is not a paid error (fresh accumulator, nothing spent)
    _cost.begin_op()
    ledger.emit_agent_event(
        op_name="memory.get",
        identity={k: _ASK_ARGS[k] for k in ("org", "namespace", "agent", "user_id", "session_id")},
        request_id="r5",
        latency_ms=3,
        success=False,
        error_code="AUTH_INSUFFICIENT",
        raw_args=_ASK_ARGS,
    )
    assert len(seen) == 1
