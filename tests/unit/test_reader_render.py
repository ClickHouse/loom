"""memory.search(render="reader") returns the hits the way memory.ask reads
them: dated, keyed, typed, in question order, plus the
synthesizer's instructions. Pure; CH-free."""

from __future__ import annotations

from typing import Any

from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput
from server.memory.app.ask.prompts import _SYNTH_SYSTEM
from server.memory.app.ask.reader_render import (
    READER_ABSTAIN_TEXT,
    reader_instructions,
    render_reader_block,
)
from server.memory.domain import code_change

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


def _code_hit(key: str, **code: Any) -> dict[str, Any]:
    return {"memory_key": key, "memory_type": "semantic", "content_excerpt": "x", **code}


# what a block says once when a hit it serves holds a link
_INDEXED = code_change.compared(["fresh"])


def test_a_line_whose_code_changed_or_went_names_the_state_and_its_commit() -> None:
    block = render_reader_block(
        [
            _code_hit("a", code_state="changed", code_changed_since="c0ffee" * 7),
            _code_hit("b", code_state="unresolved"),
            _code_hit("c", code_state="missing", code_changed_since="dead" * 10),
        ]
    )
    assert block.split("\n\n") == [
        _INDEXED,
        "[undated] [a] (semantic) (code_state=changed) (its code changed since it was written,"
        " commit c0ffeec0ffee: check before relying on it) x",
        "[undated] [b] (semantic) (code_state=unresolved) (its code was renamed or removed and"
        " could not be followed: check it) x",
        "[undated] [c] (semantic) (code_state=missing) (the code it is about is gone, commit"
        " deaddeaddead: check before relying on it) x",
    ]


def test_a_line_that_names_code_says_its_state_its_anchor_commit_and_its_write_day() -> None:
    for code in ({}, {"code_state": ""}):
        assert render_reader_block([_code_hit("k", **code)]) == "[undated] [k] (semantic) x"
    # the day in the note is the write day, whatever date the line opens with
    anchored = {"code_rev": "ba5e" * 10, "created_at": "2026-04-01T08:00:00"}
    dated = {"temporal_anchor": "2026-03-02T00:00:00"}
    for code, said in (
        (
            {"code_state": "fresh", **anchored},
            "[2026-04-01 08:00] [k] (semantic) (code_state=fresh rev=ba5eba5eba5e"
            " written=2026-04-01) x",
        ),
        # a commit beside a state no note names is never printed
        (
            {"code_state": "moved", "code_changed_since": "c0ffee" * 7, **anchored, **dated},
            "[2026-03-02] [k] (semantic) (code_state=moved rev=ba5eba5eba5e written=2026-04-01) x",
        ),
    ):
        # a linked hit's state and commit are the index's, which the block says once
        assert render_reader_block([_code_hit("k", **code)]) == f"{_INDEXED}\n\n{said}"


def test_an_unlinked_line_says_so_once_and_keeps_the_text_it_was_written_with() -> None:
    text = "The cause is in Printable (src/core/print_helpers.py:8), fixed at lines 940-944."
    unlinked = _code_hit("u", code_state="unlinked", content_excerpt=text)
    fresh = _code_hit("f", code_state="fresh", content_excerpt=text)
    assert render_reader_block([unlinked, unlinked | {"memory_key": "v"}]).split("\n\n") == [
        code_change.compared(["unlinked"]),
        f"[undated] [u] (semantic) (code_state=unlinked) {text}",
        f"[undated] [v] (semantic) (code_state=unlinked) {text}",
    ]
    assert render_reader_block([unlinked, fresh]).split("\n\n") == [
        code_change.compared(["unlinked", "fresh"]),
        f"[undated] [u] (semantic) (code_state=unlinked) {text}",
        f"[undated] [f] (semantic) (code_state=fresh) {text}",
    ]


def test_a_line_whose_code_moved_names_where_it_is_now() -> None:
    link = {
        "code_namespace": "code-a",
        "path": "src/reader.py",
        "symbol": "reader::load_rows",
        "start_line": 12,
        "end_line": 40,
        "state": "moved",
        "was_path": "src/reader.py",
        "was_symbol": "reader::fetch_rows",
    }
    now = "`reader::fetch_rows` is now `reader::load_rows` at src/reader.py:12-40"
    block = render_reader_block(
        [
            _code_hit("a", code_state="moved", code_moved=[link]),
            _code_hit(
                "b", code_state="changed", code_changed_since="c0ffee" * 7, code_moved=[link]
            ),
            # re-anchored by the `revise` policy: fresh, and its text still says the old name
            _code_hit("c", code_state="fresh", code_moved=[link]),
        ]
    )
    assert block.split("\n\n") == [
        _INDEXED,
        f"[undated] [a] (semantic) (code_state=moved) (code moved: {now}) x",
        "[undated] [b] (semantic) (code_state=changed) (its code changed since it was written,"
        f" commit c0ffeec0ffee: check before relying on it; {now}) x",
        f"[undated] [c] (semantic) (code_state=fresh) (code moved: {now}) x",
    ]


def test_a_hit_model_renders_its_relocated_links_as_a_wire_dict_does() -> None:
    from server.memory.api.models_crud import CodeLink
    from server.memory.api.models_search import _MemorySearchHit

    link = CodeLink(
        code_namespace="code-a",
        path="src/io.py",
        symbol="reader::fetch_rows",
        start_line=3,
        end_line=9,
        state="moved",
        was_path="src/reader.py",
        was_symbol="reader::fetch_rows",
    )
    hit = _MemorySearchHit(
        memory_key="k",
        memory_type="semantic",
        content_excerpt="x",
        distance=0.0,
        similarity=1.0,
        code_state="moved",
        code_moved=[link],
    )
    line = (
        f"{_INDEXED}\n\n[undated] [k] (semantic) (code_state=moved)"
        " (code moved: `reader::fetch_rows` is now at src/io.py:3-9) x"
    )
    assert render_reader_block([hit]) == line
    assert render_reader_block([hit.model_dump()]) == line


def test_instructions_are_the_synthesizer_prompt_and_fields_exist() -> None:
    assert reader_instructions().startswith(_SYNTH_SYSTEM)
    assert reader_instructions().endswith(READER_ABSTAIN_TEXT)
    assert (
        MemorySearchInput(org="o", namespace="n", agent="a", query="q", render="reader").render
        == "reader"
    )
    out = MemorySearchOutput(results=[], embedding_model="e")
    assert out.reader_block == "" and out.reader_instructions == ""
