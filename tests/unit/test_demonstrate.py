"""Test-time learning is retrieval of labelled examples."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.domain import demonstrations as svc


def _d(key: str, label: str, sim: float, text: str = "t") -> svc.Demonstration:
    return svc.Demonstration(memory_key=key, text=text, label=label, similarity=sim)


def test_label_of_reads_the_prefixed_tag() -> None:
    assert svc.label_of(["example", "label:card_arrival", "x"], "label:") == "card_arrival"
    assert svc.label_of(["example", "label:"], "label:") == ""
    assert svc.label_of(["other"], "label:") == ""


def test_vote_is_similarity_weighted_and_normalised() -> None:
    v = svc.vote([_d("a", "x", 0.9), _d("b", "y", 0.5), _d("c", "x", 0.4), _d("d", "", 0.99)])
    assert v.predicted == "x" and v.votes == {"x": round(1.3 / 1.8, 6), "y": round(0.5 / 1.8, 6)}
    assert abs(v.margin - (1.3 - 0.5) / 1.8) < 1e-6
    assert svc.vote([]) == svc.Vote() and svc.vote([_d("a", "", 0.9)]).predicted == ""


def test_few_shot_block_orders_and_trims() -> None:
    demos = [
        _d("a", "x", 0.9, "first"),
        _d("b", "y", 0.8, "second"),
        _d("c", "", 0.7, "unlabelled"),
    ]
    block = svc.few_shot_block(demos)
    assert block == "Input: first\nLabel: x\n\nInput: second\nLabel: y"
    assert svc.few_shot_block(demos, max_chars=25) == "Input: first\nLabel: x"


@pytest.mark.asyncio
async def test_handler_searches_examples_reads_labels_and_votes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.search as ms
    from server.memory.api.models_demonstrate import MemoryDemonstrateInput
    from server.memory.app import demonstrate as h

    seen: dict[str, Any] = {}

    def _hit(key: str, text: str, sim: float, tags: list[str]) -> SimpleNamespace:
        return SimpleNamespace(memory_key=key, content_excerpt=text, similarity=sim, tags=tags)

    async def _search(inp: Any):
        seen["search"] = inp
        return SimpleNamespace(
            results=[
                _hit("e1", "I lost my card", 0.92, ["example", "label:lost_or_stolen_card"]),
                # e1 written in another session: still one vote
                _hit("e1", "I lost my card", 0.91, ["example", "label:lost_or_stolen_card"]),
                _hit("e2", "card not arrived", 0.81, ["example", "label:card_arrival"]),
                _hit("e3", "what is the fee", 0.40, ["example", "label:fees"]),
                _hit("plain", "no label here", 0.95, ["example"]),
            ]
        )

    monkeypatch.setattr(ms, "handle_memory_search", _search)
    out = await h.handle_memory_demonstrate(
        MemoryDemonstrateInput(org="o", namespace="n", query="my card was stolen", k=2)
    )
    # over-fetched so one row per example still leaves k*2 examples
    assert seen["search"].tags == ["example"] and seen["search"].top_k == 12
    assert seen["search"].bodies == "full"  # examples stay whole at any k
    # The labels are the hits' own row tags, read in each hit's namespace by the search
    assert seen["search"].search_mode == "semantic" and seen["search"].include_tags is True
    assert [d.memory_key for d in out.demonstrations] == ["e1", "e2"]  # k=2, unlabelled skipped
    assert out.predicted_label == "lost_or_stolen_card" and out.abstain is False
    assert out.examined == 4 and out.labelled == 2
    assert out.few_shot_block.startswith("Input: I lost my card\nLabel: lost_or_stolen_card")
    # A margin floor turns a close vote into an abstain with the prediction kept.
    out = await h.handle_memory_demonstrate(
        MemoryDemonstrateInput(org="o", namespace="n", query="q", k=2, min_margin=0.5)
    )
    assert out.abstain is True and out.abstain_reason == "low_margin" and out.predicted_label


@pytest.mark.asyncio
async def test_handler_abstains_without_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.search as ms
    from server.memory.api.models_demonstrate import MemoryDemonstrateInput
    from server.memory.app import demonstrate as h

    async def _empty(inp: Any):
        return SimpleNamespace(results=[])

    monkeypatch.setattr(ms, "handle_memory_search", _empty)
    out = await h.handle_memory_demonstrate(
        MemoryDemonstrateInput(org="o", namespace="n", query="q")
    )
    assert out.abstain and out.abstain_reason == "no_examples" and out.demonstrations == []


def test_registered_as_viewer_op() -> None:
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["memory.demonstrate"]
    assert op.required_role == Role.VIEWER and op.idempotent
