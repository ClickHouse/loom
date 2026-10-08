"""A procedure's rank carries what its runs proved.

The utility term is a pure function of the folded run_stats, enters the
fused match score only through a weight that defaults to 0, and the shown
side of the ledger (which procedures a pack injected) rides the pack output
and its agent_events projection.
"""

from __future__ import annotations

import asyncio

import pytest

from server.memory.api.models_context import ContextPackOutput
from server.memory.domain.procedure_graph import utility_from_stats
from server.observability.event_args import project_result
from tests.unit.test_procedure_ops import _stub_search_and_bulk, _value


@pytest.mark.parametrize(
    "total, ok",
    [(0, 0), (1, 1), (1, 0), (10, 9), (10, 1), (100, 100)],
)
def test_utility_is_laplace_success_rate_minus_prior(total: int, ok: int) -> None:
    expected = (ok + 1) / (total + 2) - 0.5
    assert utility_from_stats(total, ok) == pytest.approx(expected, abs=1e-6)
    if total == 0:
        assert utility_from_stats(total, ok) == 0.0  # unrun: exactly neutral


def test_utility_is_bounded_and_clamps_bad_counts() -> None:
    assert -0.5 < utility_from_stats(1000, 0) < utility_from_stats(1000, 1000) < 0.5
    assert utility_from_stats(3, 9) == utility_from_stats(3, 3)  # ok never exceeds total
    assert utility_from_stats(-4, -2) == 0.0
    assert utility_from_stats(4, 2, prior=0.6) == pytest.approx(0.5 - 0.6)


def test_match_score_is_similarity_when_the_weight_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    rows = {
        "p:proven": _value("proven", total=20, ok=19),
        "p:flaky": _value("flaky", total=20, ok=4),
    }
    # flaky is more similar (first in search order)
    _stub_search_and_bulk(monkeypatch, rows, ["p:flaky", "p:proven"])
    out = asyncio.run(
        handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    assert [m.memory_key for m in out.matches] == ["p:flaky", "p:proven"]
    assert out.matches[0].score == out.matches[0].similarity
    # utility is still reported so a caller can see what the ledger says
    assert out.matches[0].utility < 0 < out.matches[1].utility


def test_match_weight_lets_the_proven_procedure_overtake(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    rows = {
        "p:proven": _value("proven", total=20, ok=19),
        "p:flaky": _value("flaky", total=20, ok=4),
    }
    _stub_search_and_bulk(monkeypatch, rows, ["p:flaky", "p:proven"])
    out = asyncio.run(
        handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", situation="x", utility_weight=1.0)
        )
    )
    assert [m.memory_key for m in out.matches] == ["p:proven", "p:flaky"]
    proven, flaky = out.matches
    assert proven.score == pytest.approx(proven.similarity + proven.utility, abs=1e-6)
    assert flaky.score == pytest.approx(flaky.similarity + flaky.utility, abs=1e-6)


def test_unrun_procedure_keeps_its_similarity_under_any_weight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    rows = {"p:new": _value("new", total=0, ok=0)}
    _stub_search_and_bulk(monkeypatch, rows, ["p:new"])
    out = asyncio.run(
        handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", situation="x", utility_weight=3.0)
        )
    )
    assert out.matches[0].utility == 0.0 and out.matches[0].score == out.matches[0].similarity


def test_pack_output_names_the_procedures_it_showed_and_the_event_projects_them() -> None:
    out = ContextPackOutput(
        rendered="",
        sections={},
        section_count=0,
        token_budget=100,
        procedure_keys=["procedure_deploy", "procedure_rollback"],
    )
    projected = project_result(out)
    assert projected["out_procedure_keys"] == "procedure_deploy,procedure_rollback"
    assert (
        ContextPackOutput(rendered="", sections={}, section_count=0, token_budget=1).procedure_keys
        == []
    )
