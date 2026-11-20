"""Tool receipts produce bounded display steps without exposing protected calls."""

from __future__ import annotations

from server.assistant.domain.steps import STEP_TEXT, Step, steps_of
from server.assistant.domain.support import RUN_ROWS, CallRow
from server.operations.toolsets import MCP_ASSISTANT_OPS


def test_every_assistant_op_has_its_step_text() -> None:
    # An op added to the toolset fails here until it gets words.
    assert set(STEP_TEXT) == MCP_ASSISTANT_OPS


def test_a_step_says_what_was_read_and_a_failed_one_what_could_not_be() -> None:
    steps, cut = steps_of(
        [
            CallRow("namespace.audit", True),
            CallRow("memory.search", False),
        ]
    )
    assert steps == [
        Step("namespace.audit", "Read the audit", True),
        Step("memory.search", "Couldn't search memories", False),
    ]
    assert cut is False


def test_an_unknown_op_and_a_derived_row_are_no_step() -> None:
    steps, _ = steps_of(
        [
            CallRow("memory.ask", True),
            # The consistency check memory.ask records under its own row.
            CallRow("memory.ask.consistency", True),
            CallRow("memory.set", True),
        ]
    )
    assert [s.op for s in steps] == ["memory.ask"]


def test_a_cut_run_shows_its_first_rows_and_says_so() -> None:
    rows = [CallRow("memory.get", True) for i in range(RUN_ROWS + 1)]
    steps, cut = steps_of(rows)
    assert (len(steps), cut) == (RUN_ROWS, True)
