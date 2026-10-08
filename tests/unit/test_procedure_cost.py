"""A run costs something, and the ledger says what."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.domain.procedure_graph import RevisionCost, RunTrace, cost_verdict


def test_cost_verdict_reads_the_first_dimension_both_sides_reported() -> None:
    lean = RevisionCost(samples=5, p50_steps=5, p50_duration_ms=1000)
    heavy = RevisionCost(samples=5, p50_steps=9, p50_duration_ms=1100)
    # duration first: 1100 vs 1000 is within 25%, so steps are not consulted
    assert cost_verdict(lean, heavy) is None
    # steps only when duration is unreported on a side
    heavy_no_dur = RevisionCost(samples=5, p50_steps=9)
    assert cost_verdict(lean, heavy_no_dur) == "costlier"
    assert cost_verdict(heavy_no_dur, lean) == "cheaper"
    # too few costed successes: nothing to say
    assert cost_verdict(RevisionCost(samples=2, p50_steps=5), heavy_no_dur) is None
    assert cost_verdict(lean, RevisionCost(samples=1, p50_steps=90)) is None
    # nothing reported on either side
    assert cost_verdict(RevisionCost(samples=5), RevisionCost(samples=5)) is None
    # tolerance is a knob
    assert (
        cost_verdict(lean, RevisionCost(samples=5, p50_duration_ms=1100), tolerance=0.05)
        == "costlier"
    )


def test_run_trace_carries_cost_with_zero_as_unreported() -> None:
    t = RunTrace(path=["a", "b"], ok=True)
    assert (t.steps, t.duration_ms, t.tokens) == (0, 0, 0)
    t2 = RunTrace(path=["a"], ok=True, steps=7, duration_ms=1200, tokens=900)
    assert t2.model_dump()["duration_ms"] == 1200


def _row() -> dict[str, Any]:
    return {
        "situation": "s",
        "name": "ship",
        "steps": ["a", "b"],
        "tags": [],
        "metadata": {},
        "graph": None,
        "run_stats": {"total": 0, "ok": 0},
        "runs": [],
        "template": False,
        "revision": 1,
    }


def test_record_run_defaults_steps_to_the_path_and_writes_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureRecordRunInput
    from server.memory.app import procedure_ledger
    from server.memory.app.procedure import handle_procedure_record_run

    row = _row()
    rec: list[dict[str, Any]] = []

    async def _get(inp: Any) -> Any:
        return NS(value=row, content_sha256="h")

    async def _record(**kw: Any) -> Any:
        rec.append(kw)

    async def _count(**kw: Any) -> tuple[int, int]:
        return len(rec), sum(1 for r in rec if r["ok"])

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    monkeypatch.setattr(procedure_ledger, "record", _record)
    monkeypatch.setattr(procedure_ledger, "count_since", _count)

    asyncio.run(
        handle_procedure_record_run(
            ProcedureRecordRunInput(
                org="o",
                namespace="ns",
                memory_key="p:ship",
                path=["n1", "n2", "n3"],
                ok=True,
                duration_ms=4200,
                tokens=800,
            )
        )
    )
    assert (
        rec[-1]["steps_taken"] == 3 and rec[-1]["duration_ms"] == 4200 and rec[-1]["tokens"] == 800
    )

    asyncio.run(
        handle_procedure_record_run(
            ProcedureRecordRunInput(
                org="o", namespace="ns", memory_key="p:ship", path=[], ok=False, steps_taken=11
            )
        )
    )
    assert rec[-1]["steps_taken"] == 11 and rec[-1]["duration_ms"] == 0


def _pending_row(prior_rev: int = 1, cur_rev: int = 2) -> dict[str, Any]:
    row = _row()
    row["revision"] = cur_rev
    row["graph"] = {
        "nodes": [{"id": "n1", "text": "a"}, {"id": "n2", "text": "b"}],
        "edges": [{"src": "n1", "dst": "n2"}],
    }
    row["pending_revision"] = {
        "prior_revision": prior_rev,
        "graph": row["graph"],
        "steps": ["a", "b"],
        "reason": "trim",
        "by": "agent",
    }
    row["revisions"] = [{"revision": cur_rev, "outcome": "pending"}]
    return row


def _evolve_fixture(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, Any],
    stats: dict[int, tuple[int, int]],
    cost: dict[int, dict[str, float]],
) -> None:
    from server.memory.app import procedure_ledger

    async def _get(inp: Any) -> Any:
        return NS(value=row, content_sha256="h")

    async def _set(inp: Any) -> Any:
        row.clear()
        row.update(inp.value)
        return NS(memory_id="m", version=2)

    async def _unfolded(**kw: Any) -> list[dict[str, Any]]:
        return []

    async def _stats(**kw: Any) -> dict[int, tuple[int, int]]:
        return stats

    async def _cost(**kw: Any) -> dict[int, dict[str, float]]:
        return cost

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set)
    monkeypatch.setattr(procedure_ledger, "unfolded", _unfolded)
    monkeypatch.setattr(procedure_ledger, "stats_by_revision", _stats)
    monkeypatch.setattr(procedure_ledger, "cost_by_revision", _cost)


def test_gate_reverts_an_accepted_revision_that_costs_more(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _pending_row()
    # equal success (10/10 each): the SPRT accepts; the cost check reverts
    _evolve_fixture(
        monkeypatch,
        row,
        {1: (10, 10), 2: (10, 10)},
        {1: {"samples": 10, "p50_steps": 5}, 2: {"samples": 10, "p50_steps": 9}},
    )
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=0)
        )
    )
    assert out.gate == "revert" and out.gate_reason == "cost"
    assert out.gate_cost_before["p50_steps"] == 5 and out.gate_cost_after["p50_steps"] == 9
    assert row["rejected"][-1]["gate_reason"] == "cost"
    assert "pending_revision" not in row and row["revision"] == 3


def test_gate_accepts_when_cost_is_within_tolerance_or_unreported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _pending_row()
    _evolve_fixture(
        monkeypatch,
        row,
        {1: (10, 10), 2: (10, 10)},
        {1: {"samples": 10, "p50_steps": 5}, 2: {"samples": 10, "p50_steps": 6}},
    )
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=0)
        )
    )
    assert out.gate == "accept" and out.gate_reason == ""

    row = _pending_row()
    _evolve_fixture(monkeypatch, row, {1: (10, 10), 2: (10, 10)}, {})  # no run reported a cost
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=0)
        )
    )
    assert out.gate == "accept" and out.gate_cost_after.get("samples", 0) == 0

    row = _pending_row()
    _evolve_fixture(
        monkeypatch,
        row,
        {1: (10, 10), 2: (10, 10)},
        {1: {"samples": 10, "p50_steps": 5}, 2: {"samples": 10, "p50_steps": 9}},
    )
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(
                org="o",
                namespace="ns",
                memory_key="p:ship",
                strike_limit=0,
                gate_cost_tolerance=0.0,
            )
        )
    )
    assert out.gate == "accept"  # cost check disabled


def test_success_gate_still_reverts_on_a_drop_with_reason_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _pending_row()
    _evolve_fixture(
        monkeypatch,
        row,
        {1: (20, 19), 2: (12, 3)},
        {1: {"samples": 19, "p50_steps": 9}, 2: {"samples": 3, "p50_steps": 5}},
    )
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", strike_limit=0)
        )
    )
    assert out.gate == "revert" and out.gate_reason == "success"
