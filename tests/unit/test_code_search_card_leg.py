"""The card leg — the weekly file cards vote for their files,
whose skeleton chunks join the fusion as a capped RRF list (the history
leg's shape), and a unit only the cards proposed says `via: card`."""

from __future__ import annotations

from typing import Any

from server.code.app.search.fusion import _RRF_K, _rrf_fuse, leg_provenance


def _row(cid: str, path: str = "src/a.cpp") -> dict[str, Any]:
    return {
        "chunk_id": cid,
        "path": path,
        "scope": "",
        "kind": "function",
        "start_line": 1,
        "end_line": 2,
        "content": cid,
        "symbol_ids": [],
        "context_description": "",
    }


def test_card_rows_enter_the_fusion_below_the_content_legs_top() -> None:
    lexical = [_row("L1"), _row("L2")]
    vector = [_row("V1"), _row("L1")]
    cards = [_row("C1", "src/card.cpp"), _row("C2", "src/card.cpp")]
    fused = _rrf_fuse(lexical, vector, None, card_rows=cards, card_cap=1.0)
    scores = {r["chunk_id"]: s for r, s in fused}
    # both content legs found L1: it stays on top
    assert max(scores, key=scores.get) == "L1"
    # the card-only rows are in the pool, each worth at most the two-leg top
    top = scores["L1"]
    assert scores["C1"] == min(1.0 / (_RRF_K + 1), top)
    assert scores["C2"] < scores["C1"]
    assert set(scores) == {"L1", "L2", "V1", "C1", "C2"}


def test_card_cap_bounds_a_card_row_and_a_card_vote_lifts_a_content_hit() -> None:
    lexical = [_row("L1"), _row("X")]
    vector = [_row("V1")]
    cards = [_row("X"), _row("C1")]
    fused = dict(
        (r["chunk_id"], s) for r, s in _rrf_fuse(lexical, vector, card_rows=cards, card_cap=0.5)
    )
    # X was second in the lexical list and first among the cards: the card
    # vote adds at most half of the two-leg top (L1 = 1/(k+1))
    assert fused["X"] == 1.0 / (_RRF_K + 2) + min(1.0 / (_RRF_K + 1), 0.5 / (_RRF_K + 1))
    assert fused["C1"] == min(1.0 / (_RRF_K + 2), 0.5 / (_RRF_K + 1))


def test_history_and_cards_are_capped_against_the_same_two_leg_top() -> None:
    lexical = [_row("L1")]
    vector = [_row("L1")]
    top = 2.0 / (_RRF_K + 1)
    fused = dict(
        (r["chunk_id"], s)
        for r, s in _rrf_fuse(
            lexical,
            vector,
            [_row("H1")],
            history_cap=0.25,
            card_rows=[_row("C1")],
            card_cap=0.5,
        )
    )
    assert fused["H1"] == min(1.0 / (_RRF_K + 1), 0.25 * top)
    assert fused["C1"] == min(1.0 / (_RRF_K + 1), 0.5 * top)
    assert fused["L1"] == top


def test_provenance_leaves_card_only_units_to_the_card_label() -> None:
    lexical = [_row("L1")]
    vector = [_row("V1")]
    prov = leg_provenance(lexical, vector)
    cards = [_row("C1"), _row("L1")]
    card_only = {r["chunk_id"] for r in cards} - prov.keys()
    assert card_only == {"C1"}  # L1 keeps its lexical provenance
