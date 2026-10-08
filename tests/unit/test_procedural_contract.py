"""The procedural type keeps its contract at every door."""

from __future__ import annotations

import pytest

from server.memory.domain.procedural_contract import (
    RETYPE_TAG,
    RETYPE_TO,
    procedure_shape_reason,
    retype_for,
)


def test_shape_reason_names_the_gap() -> None:
    assert procedure_shape_reason("Verify test fixes on both configs") == "value is not an object"
    assert procedure_shape_reason({"steps": ["a", "b"]}) == "no situation"
    assert procedure_shape_reason({"situation": "  ", "steps": ["a", "b"]}) == "no situation"
    assert procedure_shape_reason({"situation": "s"}) == "no steps"
    assert procedure_shape_reason({"situation": "s", "steps": "a; b"}) == "no steps"
    assert procedure_shape_reason({"situation": "s", "steps": ["only one"]}) == "fewer than 2 steps"
    assert (
        procedure_shape_reason({"situation": "s", "steps": ["a", "  ", 3]}) == "fewer than 2 steps"
    )
    assert procedure_shape_reason({"situation": "s", "steps": ["a", "b"]}) is None
    # an evolved body whose live steps shrank to one keeps its graph, and the
    # graph says it is a procedure (the RMW re-save must not retype it)
    assert (
        procedure_shape_reason(
            {
                "situation": "s",
                "steps": ["only live step"],
                "graph": {"nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "edges": []},
            }
        )
        is None
    )
    assert (
        procedure_shape_reason(
            {
                "situation": "s",
                "steps": ["one"],
                "graph": {"nodes": [{"id": "a"}], "edges": []},
            }
        )
        == "fewer than 2 steps"
    )
    # a real procedure.add body passes with its graph and counters attached
    assert (
        procedure_shape_reason(
            {
                "situation": "when CI fails with UNKNOWN_DATABASE",
                "name": "fix_ports",
                "steps": ["check pool ports", "derive HTTP port from TCP"],
                "graph": {"nodes": []},
                "run_stats": {"total": 0, "ok": 0},
            }
        )
        is None
    )


def test_retype_only_touches_procedural_declarations() -> None:
    assert retype_for("semantic", "anything") is None
    assert retype_for("reflection", {"steps": ["a"]}) is None
    assert retype_for("procedural", {"situation": "s", "steps": ["a", "b"]}) is None
    r = retype_for("procedural", "a single lesson")
    assert r is not None
    assert r.memory_type == RETYPE_TO == "semantic"
    assert r.tags == [RETYPE_TAG] == ["retyped_from:procedural"]
    assert r.reason == "value is not an object"


def test_procedure_add_refuses_a_single_step() -> None:
    from pydantic import ValidationError

    from server.memory.api.models_procedure import ProcedureAddInput

    with pytest.raises(ValidationError):
        ProcedureAddInput(org="o", namespace="n", name="x", situation="s", steps=["only"])
    ok = ProcedureAddInput(org="o", namespace="n", name="x", situation="s", steps=["a", "b"])
    assert ok.steps == ["a", "b"]


def test_set_output_and_bulk_output_carry_the_retype() -> None:
    from server.memory.api.models_bulk import MemoryBulkSetOutput
    from server.memory.api.models_crud import MemorySetOutput

    assert MemorySetOutput(version=1).retyped_from == ""
    assert MemorySetOutput(version=1, retyped_from="procedural").retyped_from == "procedural"
    assert MemoryBulkSetOutput(written=[]).retyped == []
