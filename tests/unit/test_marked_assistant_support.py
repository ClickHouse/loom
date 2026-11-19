"""Protected ledger participation is unknown evidence for assistant support."""

from __future__ import annotations

import pytest

from server.assistant.domain import support as rule
from server.assistant.domain.steps import Step, steps_of
from server.assistant.domain.support import RUN_ROWS, CallRow
from server.assistant.store import ledger
from server.operations.toolsets import ASSISTANT_CITABLE_OPS, ASSISTANT_NO_NAMESPACE_DATA_OPS


def support_result(rows: list[CallRow]) -> str:
    return rule.support_of(
        [],
        text="An answer",
        calls=rule.derive(rows),
        citable=ASSISTANT_CITABLE_OPS,
        no_data=ASSISTANT_NO_NAMESPACE_DATA_OPS,
    )


def decoded(operation: str, success: bool, *, marked: bool) -> list[CallRow]:
    return ledger._rows(
        [
            {
                "operation": operation,
                "success": success,
                "out_found": "false",
                "out_hits": "0",
                "mirror_marked": int(marked),
            }
        ]
    )


@pytest.mark.parametrize("operation", ["knowledge.search", "knowledge.get", "insights.query"])
@pytest.mark.parametrize("success", [False, True])
def test_protected_calls_neither_claim_failure_nor_decide_support(
    operation: str, success: bool
) -> None:
    rows = decoded(operation, success, marked=True)
    assert steps_of(rows) == ([], False)
    assert support_result(rows) == "unchecked"


@pytest.mark.parametrize(
    ("operation", "success", "text", "support"),
    [
        ("knowledge.search", True, "Searched documents", "unsupported"),
        ("knowledge.search", False, "Couldn't search documents", "unchecked"),
        ("knowledge.get", True, "Read a document", "unsupported"),
        ("knowledge.get", False, "Couldn't read a document", "unchecked"),
    ],
)
def test_ordinary_empty_calls_keep_their_step_and_support(
    operation: str, success: bool, text: str, support: str
) -> None:
    rows = decoded(operation, success, marked=False)
    assert steps_of(rows) == ([Step(operation, text, success)], False)
    assert support_result(rows) == support


@pytest.mark.parametrize("protected_success", [False, True])
def test_empty_ordinary_evidence_cannot_make_protected_evidence_negative(
    protected_success: bool,
) -> None:
    rows = [
        *decoded("knowledge.search", True, marked=False),
        *decoded("knowledge.get", protected_success, marked=True),
    ]
    assert steps_of(rows) == ([Step("knowledge.search", "Searched documents", True)], False)
    assert support_result(rows) == "unchecked"


def test_protected_participation_keeps_the_run_bound_without_failure_steps() -> None:
    rows = decoded("knowledge.get", False, marked=True) * (RUN_ROWS + 1)
    assert steps_of(rows) == ([], True)
    assert support_result(rows) == "unchecked"
