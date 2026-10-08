"""memory.search(render="reader") returns the hits the way memory.ask reads
them: dated, keyed, typed, in question order, plus the
synthesizer's instructions. Pure; CH-free."""

from __future__ import annotations

from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput
from server.memory.app.ask.prompts import _SYNTH_SYSTEM
from server.memory.app.ask.reader_render import (
    READER_ABSTAIN_TEXT,
    reader_instructions,
    render_reader_block,
)

_HITS = [
    {
        "memory_key": "b_recent",
        "memory_type": "semantic",
        "content_excerpt": "Moved to Lisbon.",
        "valid_at": "2026-03-02T00:00:00",
        "created_at": "2026-03-02T10:00:00",
    },
    {
        "memory_key": "a_old",
        "memory_type": "semantic",
        "content_excerpt": "Lived in Porto.",
        "temporal_anchor": "2024-06-15T00:00:00",
        "created_at": "2026-03-01T10:00:00",
    },
    {"memory_key": "c_undated", "memory_type": "", "content_excerpt": "Likes espresso."},
]


def test_block_labels_date_key_and_type_and_keeps_rank_order_by_default() -> None:
    block = render_reader_block(_HITS, "what does the user like?")
    paras = block.split("\n\n")
    assert paras[0] == "[2026-03-02] [b_recent] (semantic) Moved to Lisbon."
    assert paras[1] == "[2024-06-15] [a_old] (semantic) Lived in Porto."
    assert paras[2] == "[undated] [c_undated] (memory) Likes espresso."


def test_temporal_question_orders_chronologically_with_undated_last() -> None:
    block = render_reader_block(_HITS, "when did the user move?")
    keys = [p.split("] [")[1].split("]")[0] for p in block.split("\n\n")]
    assert keys == ["a_old", "b_recent", "c_undated"]


def test_event_date_beats_valid_at_and_turn_time_is_kept() -> None:
    h = [
        {
            "memory_key": "k",
            "content_excerpt": "x",
            "temporal_anchor": "2023-07-13T09:09:00",
            "valid_at": "2023-07-13T00:00:00",
        }
    ]
    assert render_reader_block(h).startswith("[2023-07-13 09:09] [k]")
    assert render_reader_block([]) == ""


def test_instructions_are_the_synthesizer_prompt_and_fields_exist() -> None:
    assert reader_instructions().startswith(_SYNTH_SYSTEM)
    assert reader_instructions().endswith(READER_ABSTAIN_TEXT)
    assert (
        MemorySearchInput(org="o", namespace="n", agent="a", query="q", render="reader").render
        == "reader"
    )
    out = MemorySearchOutput(results=[], embedding_model="e")
    assert out.reader_block == "" and out.reader_instructions == ""
