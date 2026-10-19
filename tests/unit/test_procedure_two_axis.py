"""Precondition and goal are two factors: the pure arithmetic, the goal axis
in procedure.match, and
context.pack passing the query as the goal when it differs from the situation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.domain.procedure_evidence import (
    cosine,
    failure_lcb,
    failure_posterior,
    two_axis_score,
)


def test_failure_posterior_is_laplace_with_widest_sd_at_zero_runs() -> None:
    mean, sd = failure_posterior(0, 0)
    assert mean == pytest.approx(0.5)
    assert sd == pytest.approx((0.25 / 3.0) ** 0.5)
    mean10, sd10 = failure_posterior(10, 10)
    assert mean10 == pytest.approx(1 / 12)
    assert sd10 < sd


def test_failure_lcb_needs_established_failure() -> None:
    # one fail in three: mean 0.4, sd 0.2 → bound 0.2, well under a 0.25 margin
    assert failure_lcb(3, 2) == pytest.approx(0.2, abs=1e-6)
    # three fails in three: mean 0.8, sd ≈ 0.163 → bound ≈ 0.637
    assert failure_lcb(3, 0) == pytest.approx(0.8 - (0.16 / 6.0) ** 0.5, abs=1e-6)
    assert 0.0 <= failure_lcb(10, 10) < 0.05
    assert failure_lcb(10, 0) > 0.8
    assert failure_lcb(0, 0) == pytest.approx(0.5 - (0.25 / 3.0) ** 0.5)


def test_two_axis_score_is_geometric_mean_and_clamps_negatives() -> None:
    assert two_axis_score(0.8, None) == pytest.approx(0.8)
    assert two_axis_score(0.81, 0.49) == pytest.approx(0.63)
    assert two_axis_score(0.9, 0.0) == 0.0
    assert two_axis_score(0.9, -0.3) == 0.0
    assert two_axis_score(-0.2, None) == 0.0


def test_cosine_handles_incomparable_vectors() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [1.0]) is None
    assert cosine([], [1.0]) is None
    assert cosine([0.0, 0.0], [1.0, 0.0]) is None


# --- operation layer -------------------------------------------------------


def _value(name: str) -> dict[str, Any]:
    return {
        "situation": f"when {name}",
        "name": name,
        "steps": ["a", "b"],
        "tags": [],
        "metadata": {},
        "graph": None,
        "run_stats": {"total": 0, "ok": 0},
        "runs": [],
        "template": False,
        "revision": 1,
    }


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, dict[str, Any]],
    order: list[str],
    sims: dict[str, float],
) -> list[Any]:
    seen: list[Any] = []

    async def _search(inp: Any) -> Any:
        seen.append(inp)
        return NS(results=[NS(namespace="ns", memory_key=k, similarity=sims[k]) for k in order])

    async def _bulk(inp: Any) -> Any:
        return NS(
            results={k: NS(value=rows[k], sensitivity="") for k in inp.memory_keys if k in rows}
        )

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    monkeypatch.setattr("server.memory.app.query.handle_memory_bulk_get", _bulk)
    return seen


def test_goal_axis_sinks_a_precondition_match_with_the_wrong_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative-transfer case: same starting state, different
    objective. Single-axis matching ranks the strong situation match first;
    with a goal the geometric mean puts the procedure that fits both first."""
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app import procedure as proc

    rows = {"p:wrong_goal": _value("wrong_goal"), "p:right": _value("right")}
    seen = _stub(
        monkeypatch, rows, ["p:wrong_goal", "p:right"], {"p:wrong_goal": 0.9, "p:right": 0.6}
    )

    async def _embed(text: str) -> list[float]:
        return [1.0, 0.0]

    async def _keys(
        org: str, keys_by_ns: dict[str, list[str]]
    ) -> dict[tuple[str, str], list[float]]:
        assert keys_by_ns == {"ns": ["p:wrong_goal", "p:right"]}
        return {("ns", "p:wrong_goal"): [0.0, 1.0], ("ns", "p:right"): [1.0, 0.0]}

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: NS(embed=_embed))
    monkeypatch.setattr(proc, "_load_key_embeddings", _keys)

    out = asyncio.run(
        proc.handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", situation="x", goal="ship it")
        )
    )
    assert [m.memory_key for m in out.matches] == ["p:right", "p:wrong_goal"]
    assert out.matches[0].goal_similarity == pytest.approx(1.0)
    assert out.matches[0].score == pytest.approx(0.6**0.5, abs=1e-6)
    assert out.matches[1].goal_similarity == pytest.approx(0.0)
    assert out.matches[1].score == 0.0
    # the search ran with the key axis off so `similarity` is the situation cosine
    assert seen[-1].key_axis is False

    # no goal: key axis left to the server default, no goal similarity reported
    out1 = asyncio.run(
        proc.handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    assert [m.memory_key for m in out1.matches] == ["p:wrong_goal", "p:right"]
    assert seen[-1].key_axis is None
    assert all(m.goal_similarity is None for m in out1.matches)
    assert out1.matches[0].score == pytest.approx(0.9)


def test_goal_axis_composes_with_tool_overlap_and_status_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app import procedure as proc

    rows = {"p:mined": {**_value("mined"), "tags": ["mined"]}, "p:auth": _value("auth")}
    _stub(monkeypatch, rows, ["p:mined", "p:auth"], {"p:mined": 0.9, "p:auth": 0.5})

    async def _embed(text: str) -> list[float]:
        return [1.0, 0.0]

    async def _keys(
        org: str, keys_by_ns: dict[str, list[str]]
    ) -> dict[tuple[str, str], list[float]]:
        return {("ns", "p:mined"): [1.0, 0.0], ("ns", "p:auth"): [1.0, 0.0]}

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: NS(embed=_embed))
    monkeypatch.setattr(proc, "_load_key_embeddings", _keys)
    out = asyncio.run(
        proc.handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", situation="x", goal="g")
        )
    )
    # a candidate stays last even with a perfect goal match
    assert [m.memory_key for m in out.matches] == ["p:auth", "p:mined"]
    assert out.matches[1].score > out.matches[0].score


def test_goal_axis_degrades_to_single_axis_when_vectors_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app import procedure as proc

    rows = {"p:a": _value("a"), "p:b": _value("b")}
    _stub(monkeypatch, rows, ["p:a", "p:b"], {"p:a": 0.9, "p:b": 0.6})

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("no vectors")

    monkeypatch.setattr(proc, "_load_key_embeddings", _boom)

    async def _embed(text: str) -> list[float]:
        return [1.0]

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: NS(embed=_embed))
    out = asyncio.run(
        proc.handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", situation="x", goal="g")
        )
    )
    assert [m.memory_key for m in out.matches] == ["p:a", "p:b"]
    assert out.matches[0].score == pytest.approx(0.9)
    assert all(m.goal_similarity is None for m in out.matches)


def test_rows_without_a_key_vector_keep_their_situation_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app import procedure as proc

    rows = {"p:a": _value("a"), "p:b": _value("b")}
    _stub(monkeypatch, rows, ["p:a", "p:b"], {"p:a": 0.5, "p:b": 0.6})

    async def _embed(text: str) -> list[float]:
        return [1.0, 0.0]

    async def _keys(
        org: str, keys_by_ns: dict[str, list[str]]
    ) -> dict[tuple[str, str], list[float]]:
        return {("ns", "p:b"): [0.0, 1.0]}  # p:a has no key vector

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: NS(embed=_embed))
    monkeypatch.setattr(proc, "_load_key_embeddings", _keys)
    out = asyncio.run(
        proc.handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", situation="x", goal="g")
        )
    )
    by = {m.memory_key: m for m in out.matches}
    assert by["p:a"].goal_similarity is None and by["p:a"].score == pytest.approx(0.5)
    assert by["p:b"].goal_similarity == pytest.approx(0.0) and by["p:b"].score == 0.0


def test_context_pack_passes_the_query_as_goal_when_situation_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    calls: list[Any] = []

    async def _match(inp: Any) -> Any:
        calls.append(inp)
        return NS(matches=[])

    monkeypatch.setattr("server.memory.app.procedure.handle_procedure_match", _match)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())

    asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="ship the release",
                situation="tests green on main",
                include_knowledge=False,
                only_sections=["procedures"],
            )
        )
    )
    assert calls and calls[-1].situation == "tests green on main"
    assert calls[-1].goal == "ship the release"

    asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="ship the release",
                include_knowledge=False,
                only_sections=["procedures"],
            )
        )
    )
    assert calls[-1].situation == "ship the release" and calls[-1].goal is None
