"""Successful tool receipts produce fixed display steps in receipt order."""

from __future__ import annotations

from server.assistant.domain.steps import STEP_TEXT, Step, steps_of
from server.operations.toolsets import MCP_ASSISTANT_OPS


def test_every_assistant_op_has_its_step_text() -> None:
    # An op added to the toolset fails here until it gets words.
    assert set(STEP_TEXT) == MCP_ASSISTANT_OPS


def test_a_step_says_what_was_read() -> None:
    assert steps_of(["namespace.audit", "memory.search"]) == [
        Step("namespace.audit", "Read the audit", True),
        Step("memory.search", "Searched memories", True),
    ]


def test_an_unknown_op_and_a_derived_row_are_no_step() -> None:
    steps = steps_of(["memory.ask", "memory.ask.consistency", "memory.set"])
    assert [s.op for s in steps] == ["memory.ask"]
