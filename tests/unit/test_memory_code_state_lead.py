"""What a read says of a memory that names code: its state, the commit its link was
anchored at and its date; `unlinked` for one that names a file and holds no link; one
sentence for unlinked and changed memories; no line numbers unless its links are fresh."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.memory.app import code_link_reads, path_lessons
from server.memory.domain import code_change

# a record of a past fix as an agent wrote it: a line beside the file, a line phrase and
# the change as diff text
_RECORD = (
    "Root Cause: The issue was in the Printable class (src/core/print_helpers.py:8), which"
    " is a parent class of Basic. The fix adds two lines at lines 940-944:"
    " @@ -940,2 +940,4 @@ class Printable: __slots__ = ()"
)
_SHOWN = (
    "Root Cause: The issue was in the Printable class (src/core/print_helpers.py), which"
    " is a parent class of Basic. The fix adds two lines: @@ class Printable: __slots__ = ()"
)


def test_the_lead_names_the_state_then_the_revision_and_date_it_is_given() -> None:
    assert code_change.lead("") == code_change.lead("", "ba5e" * 10, "2026-03-02") == ""
    assert code_change.lead("unlinked") == " (code_state=unlinked)"
    assert code_change.lead("fresh", "ba5e" * 10) == " (code_state=fresh rev=ba5eba5eba5e)"
    assert code_change.lead("changed", "ba5e" * 10, "2026-03-02T10:00:00") == (
        " (code_state=changed rev=ba5eba5eba5e date=2026-03-02)"
    )


def test_a_hit_note_reads_a_model_as_its_dump_and_dates_a_line_that_shows_none() -> None:
    hit = _MemorySearchHit(
        memory_key="k",
        memory_type="semantic",
        content_excerpt="x",
        distance=0.0,
        similarity=1.0,
        valid_at="2026-03-02T00:00:00",
        created_at="2026-04-01T00:00:00",
        code_state="changed",
        code_changed_since="c0ffee" * 7,
        code_rev="ba5e" * 10,
    )
    note = (
        " (code_state=changed rev=ba5eba5eba5e date=2026-03-02) (its code changed since it was"
        " written, commit c0ffeec0ffee: check before relying on it)"
    )
    assert code_change.hit_note(hit) == code_change.hit_note(hit.model_dump()) == note
    assert code_change.hit_note(hit, dated=False) == note.replace(" date=2026-03-02", "")
    assert code_change.hit_note({"memory_key": "plain", "created_at": "2026-04-01"}) == ""


def test_one_sentence_is_said_for_unlinked_and_changed_memories_only() -> None:
    assert code_change.unchecked(["fresh", "unlinked"]) == code_change.UNCHECKED
    assert code_change.unchecked(["", "changed"]) == code_change.UNCHECKED
    # a fresh or moved link was compared; a lost one carries its own note
    assert code_change.unchecked(["", "fresh", "moved", "unresolved", "missing"]) == ""
    assert "\n" not in code_change.UNCHECKED and code_change.UNCHECKED.count(".") == 1


def test_a_text_is_shown_without_its_line_numbers_unless_its_links_are_fresh() -> None:
    assert code_change.without_coordinates(_RECORD) == _SHOWN
    for state in ("unlinked", "moved", "changed", "unresolved", "missing"):
        assert code_change.shown(_RECORD, state) == _SHOWN
    assert code_change.shown(_RECORD, "fresh") == code_change.shown(_RECORD, "") == _RECORD


@pytest.mark.parametrize(
    ("written", "shown"),
    [
        ("see src/reader.py:12-20 and handler.py#L3-L9", "see src/reader.py and handler.py"),
        ("`server/core/ch.py:44:7` raises", "`server/core/ch.py` raises"),
        ("readRow (line 495) loops", "readRow loops"),
        ("guarded on line 12 and in lines 40 to 44.", "guarded and."),
        # what is not a line of a file stays: a port, a time, a version, a count
        ("api.example.com:8080 at 12:30, v1.2:3", "api.example.com:8080 at 12:30, v1.2:3"),
        ("it adds 3 lines to reader.py", "it adds 3 lines to reader.py"),
        # a line that is a sentence's subject or object stays: the sentence needs it
        ("Line 3 shows it. Changed line 23 from x", "Line 3 shows it. Changed line 23 from x"),
    ],
)
def test_only_a_line_number_given_for_code_is_left_out(written: str, shown: str) -> None:
    assert code_change.without_coordinates(written) == shown


def test_a_text_names_a_file_by_a_path_with_a_directory() -> None:
    assert code_change.names_file("the cause is in src/core/print_helpers.py:8")
    for text in ("saved as notes.txt", "see README.md", "https://example.com/a/b.py", "and/or"):
        assert not code_change.names_file(text)


def test_the_revision_is_the_anchor_of_the_first_link_in_the_worst_state() -> None:
    links = [
        {"state": "changed", "anchored_commit": "a2", "path": "b.py", "qualified_name": "f"},
        {"state": "changed", "anchored_commit": "a1", "path": "a.py", "qualified_name": "g"},
        {"state": "fresh", "anchored_commit": "a3", "path": "0.py", "qualified_name": "h"},
    ]
    assert code_change.anchored_commit(links) == "a1"
    assert code_change.anchored_commit(links[2:]) == "a3"
    assert code_change.anchored_commit([]) == code_change.anchored_commit([{"state": "pending"}])
    assert code_change.anchored_commit([{"state": "fresh"}]) == ""


def _hit(key: str, text: str = "x", *, row: bool = True) -> Any:
    hit = _MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt=text, distance=0.0, similarity=1.0
    )
    hit._row_identity = ("a", "-", "-") if row else None
    return hit


@pytest.mark.asyncio
async def test_a_hit_whose_text_names_a_file_and_holds_no_link_is_unlinked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where no repository is indexed nothing is read, and a hit that names a file still
    says nothing compared it; naming a file moves no hit."""
    monkeypatch.setattr(code_link_reads.code, "code_namespaces", lambda: [])
    hits = [
        _hit("past_fix", _RECORD),
        _hit("chunk", _RECORD, row=False),  # not a memory's own row: it carries no state
        _hit("plain_fact"),
        _hit("bare_name", "saved as notes.txt"),
    ]
    search = MemorySearchInput(org="o", namespace="agent-a", agent="a", query="q")
    await code_link_reads.annotate_hits(search, hits)
    assert [h.code_state for h in hits] == ["unlinked", "", "", ""]
    assert [h.code_rev for h in hits] == [""] * 4
    assert not any(h._code_ranked_down for h in hits)
    out = MemorySearchOutput(results=hits, embedding_model="test").model_dump()
    assert out["code_note"] == code_change.UNCHECKED
    assert MemorySearchOutput(results=hits[1:], embedding_model="test").model_dump()[
        "code_note"
    ] == ("")


def test_a_lesson_that_names_code_carries_its_state_revision_and_date() -> None:
    def row(key: str, text: str, **code: Any) -> dict[str, Any]:
        written = datetime(2026, 3, 2, 10, 0)
        hit = {"memory_key": key, "namespace": "team", "value": f'"{text}"', "updated_at": written}
        return hit | code

    said = "Keep 32 buckets (src/a.cpp:8)."
    fresh = path_lessons._lesson(
        row("fresh", said, code_state="fresh", code_rev="ba5e" * 10), "src/a.cpp"
    )
    assert fresh == {
        "path": "src/a.cpp",
        "memory_key": "fresh",
        "namespace": "team",
        "excerpt": said,
        "code_state": "fresh",
        "rev": "ba5eba5eba5e",
        "date": "2026-03-02",
    }
    past = path_lessons._lesson(row("past", said), "src/a.cpp")
    assert past is not None and (past["code_state"], past["excerpt"], past["date"]) == (
        "unlinked",
        "Keep 32 buckets (src/a.cpp).",
        "2026-03-02",
    )
    # a rule that names the file only in `files` reads as it did
    assert path_lessons._lesson(row("rule", "Keep 64."), "src/a.cpp") == {
        "path": "src/a.cpp",
        "memory_key": "rule",
        "namespace": "team",
        "excerpt": "Keep 64.",
        "code_state": "",
    }
