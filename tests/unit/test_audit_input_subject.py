"""An op that declares `audit` with no `audit_subject` gets an audit row whose subject
names what the call acted on and hashes every text value: `loom.audit_log` is not
covered by erasure, so it must never hold memory content."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

import server.observability.budgets as budgets
import server.transport._common as common
from server.memory.api.models_bulk import MemoryImportCuratedInput
from server.memory.api.models_decision import DecisionRecordInput
from server.memory.api.models_move import MemoryMoveInput
from server.observability.audit import input_subject
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import post_rows

SECRET = "the merger closes on 12 March at 40 dollars a share"


def _digest(text: str) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text)}


def _audited_subject(monkeypatch: pytest.MonkeyPatch, op_name: str, body: Any) -> dict[str, Any]:
    subjects: list[dict[str, Any]] = []

    async def _audit(**kw: Any) -> None:
        subjects.append(kw["subject"])

    monkeypatch.setattr(budgets, "effective_budget_ms", lambda op, b: b)
    monkeypatch.setattr(common, "emit_op_audit", _audit)
    asyncio.run(
        post_rows.post_handler_rows(
            OPERATIONS_BY_NAME[op_name],
            transport="http",
            request_id="r",
            org="o",
            namespace="n",
            agent="a",
            actor="human:x",
            input_obj=body,
            elapsed_ms=1,
            response_body={},
            idem=(None, None),
            replay_headers={},
        )
    )
    assert len(subjects) == 1
    return subjects[0]


def test_decision_record_audit_row_holds_no_decision_text(monkeypatch) -> None:
    body = DecisionRecordInput(
        org="o",
        namespace="n",
        agent="a",
        category="finance",
        scenario=SECRET,
        reasoning=SECRET,
        outcome=SECRET,
        alternatives=[SECRET],
        decision_maker="cfo",
        confidence=0.75,
    )
    subject = _audited_subject(monkeypatch, "decision.record", body)
    assert SECRET not in json.dumps(subject)
    assert subject["scenario"] == _digest(SECRET)
    assert subject["alternatives"] == _digest(json.dumps([SECRET]))
    assert subject["decision_maker"] == _digest("cfo")
    assert (subject["org"], subject["namespace"], subject["agent"]) == ("o", "n", "a")
    assert subject["confidence"] == 0.75
    # A Literal field is a vocabulary word, not content.
    assert subject["stage"] == "decided"


def test_memory_move_audit_row_keeps_the_keys_and_hashes_the_value(monkeypatch) -> None:
    body = MemoryMoveInput(
        org="o", namespace="n", agent="a", from_key="k1", to_key="k2", value=SECRET
    )
    subject = _audited_subject(monkeypatch, "memory.move", body)
    assert SECRET not in json.dumps(subject)
    assert (subject["from_key"], subject["to_key"]) == ("k1", "k2")
    assert subject["value"] == _digest(SECRET)


def test_import_curated_audit_row_holds_no_document(monkeypatch) -> None:
    body = MemoryImportCuratedInput(
        org="o", namespace="n", content=f"## Deal\n{SECRET}\n", source_uri="file://a.md"
    )
    subject = _audited_subject(monkeypatch, "memory.import_curated", body)
    assert SECRET not in json.dumps(subject)
    assert subject["source_uri"] == "file://a.md"
    # Empty values carry nothing to hide and stay as they are.
    assert subject["extra_tags"] == []


def test_input_subject_of_an_object_without_fields_hashes_every_text_value() -> None:
    class _Body:
        def model_dump(self, mode: str) -> dict[str, Any]:
            return {"org": "o", "note": SECRET, "n": 3}

    assert input_subject(_Body()) == {"org": "o", "note": _digest(SECRET), "n": 3}
