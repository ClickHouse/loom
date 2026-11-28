"""What a read says of a memory that names code: its state, the commit its link was
anchored at and the day it was written; `unlinked`, a label no store holds, for one that
names a file and shows no link; and one sentence on what was compared."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, get_args

import pytest

from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.memory.app import code_link_reads, for_file, path_lessons, search_pages
from server.memory.domain import code_change, files
from server.memory.store import rows

# a record of a past fix as an agent wrote it: a line beside the file, a line phrase and
# the change as diff text
_RECORD = (
    "Root Cause: The issue was in the Printable class (src/core/print_helpers.py:8), which"
    " is a parent class of Basic. The fix adds two lines at lines 940-944:"
    " @@ -940,2 +940,4 @@ class Printable: __slots__ = ()"
)


def test_the_lead_names_the_state_then_the_revision_and_write_day_it_is_given() -> None:
    assert code_change.lead("") == code_change.lead("", "ba5e" * 10, "2026-03-02") == ""
    assert code_change.lead("unlinked") == " (code_state=unlinked)"
    assert code_change.lead("fresh", "ba5e" * 10) == " (code_state=fresh rev=ba5eba5eba5e)"
    assert code_change.lead("changed", "ba5e" * 10, "2026-03-02T10:00:00") == (
        " (code_state=changed rev=ba5eba5eba5e written=2026-03-02)"
    )


def test_a_hit_note_reads_a_model_as_its_dump_and_names_the_day_the_hit_was_written() -> None:
    hit = _MemorySearchHit(
        memory_key="k",
        memory_type="semantic",
        content_excerpt="x",
        distance=0.0,
        similarity=1.0,
        # the dates of what the text is about are not when it was written
        temporal_anchor="2025-01-15T00:00:00",
        valid_at="2026-03-02T00:00:00",
        created_at="2026-04-01T00:00:00",
        code_state="changed",
        code_changed_since="c0ffee" * 7,
        code_rev="ba5e" * 10,
    )
    note = (
        " (code_state=changed rev=ba5eba5eba5e written=2026-04-01) (its code changed since it"
        " was written, commit c0ffeec0ffee: check before relying on it)"
    )
    assert code_change.hit_note(hit) == code_change.hit_note(hit.model_dump()) == note
    assert code_change.hit_note({"memory_key": "plain", "created_at": "2026-04-01"}) == ""


def test_a_reply_says_once_what_was_compared_for_the_states_it_serves() -> None:
    unlinked = "Loom shows no code comparison for memories marked unlinked"
    indexed = "code_state and rev describe the repository as indexed, not your checkout"
    assert code_change.compared(["", "unlinked"]) == f"{unlinked}."
    # every link state is the index's, a fresh one too: no read compares the caller's tree
    for state in get_args(code_change.CodeState):
        assert code_change.compared(["", state]) == f"{indexed}."
    assert code_change.compared(["fresh", "unlinked", "changed"]) == f"{unlinked}; {indexed}."
    assert code_change.compared([]) == code_change.compared(["", ""]) == ""
    # the room a block keeps for the candidates' sentence covers the served rows' one
    assert len(code_change.compared(["unlinked", "fresh"])) > len(code_change.compared(["fresh"]))


def test_a_text_names_a_file_by_a_path_with_a_directory() -> None:
    assert files.names_file("the cause is in src/core/print_helpers.py:8")
    for text in ("saved as notes.txt", "see README.md", "https://example.com/a/b.py", "and/or"):
        assert not files.names_file(text)
    # the one pattern answers as the whole detector does
    for text in (_RECORD, "./a.py", "././src/a.py:12", "www.example.com/docs/index.html", "v1/2"):
        whole = any("/" in name for name in files.detect_value_files(text))
        assert files.names_file(text) is whole


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


def _hit(key: str, text: str = "x", *, row: bool = True, **code: Any) -> Any:
    hit = _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=text,
        distance=0.0,
        similarity=1.0,
        **code,
    )
    hit._row_identity = ("a", "-", "-") if row else None
    return hit


@pytest.mark.asyncio
async def test_a_hit_whose_text_names_a_file_and_holds_no_link_is_unlinked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where no repository is indexed nothing is read, and a hit that names a file still
    says no comparison is shown for it; naming a file moves no hit and changes no text."""
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
    assert hits[0].content_excerpt == _RECORD


@pytest.mark.asyncio
async def test_a_search_whose_links_cannot_be_read_still_labels_a_hit_unlinked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label needs no link: page one of a search says what its later pages say."""

    async def unreadable(input: Any, hits: list[Any]) -> Any:
        raise RuntimeError("no link store")

    monkeypatch.setattr(code_link_reads, "_annotation_reads", unreadable)
    hits = [_hit("past_fix", _RECORD), _hit("chunk", _RECORD, row=False), _hit("plain_fact")]
    search = MemorySearchInput(org="o", namespace="agent-a", agent="a", query="q")
    await code_link_reads.annotate_hits(search, hits)
    assert [(h.memory_key, h.code_state) for h in hits] == [
        ("past_fix", "unlinked"),
        ("chunk", ""),
        ("plain_fact", ""),
    ]
    # nothing else is annotated: a later read of the links still knows they were not read
    assert all(h._code_links is None and not h._code_ranked_down for h in hits)


@pytest.mark.parametrize(
    "write",
    [
        {"pinned": False},  # memory.unpin
        {"lock_mode": "value", "lock_reason": "reviewed"},  # memory.lock
        {"lock_mode": "", "lock_reason": ""},  # memory.unlock
    ],
)
def test_a_write_that_leaves_the_text_alone_does_not_move_the_printed_day(
    write: dict[str, Any],
) -> None:
    """An unpin, a lock and an unlock stamp `updated_at` and carry `created_at`: the day
    beside the state stays the first write's."""
    first = datetime(2026, 3, 2, 10, 0)
    said = "Keep 32 buckets (src/a.cpp:8)."
    head = rows.MemoryRow.from_snapshot(
        {
            "org": "o",
            "namespace": "team",
            "agent": "a",
            "memory_key": "rules/buckets",
            "memory_type": "semantic",
            "value": json.dumps(said),
            "pinned": True,
            "created_at": first,
            "updated_at": first,
        }
    )
    row = rows.build_memory_version_row(head, version=2, updated_at=rows.written_now(), **write)
    assert row["updated_at"] != first and row["value"] == json.dumps(said)
    hit = row | {"code_state": "unlinked"}
    assert for_file.listing_line(hit) == (
        f"- rules/buckets{' (pinned)' if row['pinned'] else ''}"
        f" (code_state=unlinked written=2026-03-02): {said}"
    )
    lesson = path_lessons._lesson(hit, "src/a.cpp")
    assert lesson is not None and lesson["written"] == "2026-03-02"


def test_the_output_says_what_was_compared_for_the_hits_it_was_built_with() -> None:
    hits = [_hit("past_fix", _RECORD, code_state="unlinked"), _hit("plain_fact")]
    out = MemorySearchOutput(results=hits, embedding_model="test")
    said = code_change.compared(["unlinked"])
    # the model says what its dump says, on any dump
    assert out.code_note == out.model_dump()["code_note"] == said
    without_states = out.model_dump(exclude={"results": {"__all__": {"code_state"}}})
    assert without_states["code_note"] == said
    plain = MemorySearchOutput(results=hits[1:], embedding_model="test")
    assert plain.code_note == plain.model_dump()["code_note"] == ""


def test_a_parked_page_holds_link_states_only_and_its_reader_labels_the_hit_again() -> None:
    """`unlinked` is a value the build before this one refuses in a hit: a page it may
    serve stores none, and the build that knows it labels the hit as the search did."""
    tail = [
        _hit("past_fix", _RECORD, code_state="unlinked"),
        _hit("anchored", _RECORD, code_state="fresh", code_rev="ba5e" * 10),
        _hit("chunk", _RECORD, row=False),
        _hit("plain_fact"),
    ]
    page = json.loads(
        search_pages.encode_page(
            query="q",
            top_k=2,
            candidates_total=6,
            tail=tail,
            namespaces_searched=["agent-a"],
            namespaces_skipped=[],
        )
    )
    stored = [row["code_state"] for row in page["hits"]]
    assert stored == ["", "fresh", "", ""]
    assert set(stored) <= {"", *get_args(code_change.CodeState)}
    served = [search_pages.unparked(row) for row in page["hits"]]
    assert [(h.memory_key, h.code_state, h.code_rev) for h in served if h is not None] == [
        ("past_fix", "unlinked", ""),
        ("anchored", "fresh", "ba5e" * 10),
        ("chunk", "", ""),
        ("plain_fact", "", ""),
    ]
    assert tail[0].code_state == "unlinked"  # parking a hit leaves it as it was served


def test_a_lesson_that_names_code_carries_its_state_revision_and_write_day() -> None:
    def row(key: str, text: str, **code: Any) -> dict[str, Any]:
        written = datetime(2026, 3, 2, 10, 0)
        hit = {"memory_key": key, "namespace": "team", "value": f'"{text}"', "created_at": written}
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
        "written": "2026-03-02",
    }
    # an unlinked row says so and keeps the text it was written with
    past = path_lessons._lesson(row("past", said), "src/a.cpp")
    assert past is not None and (past["code_state"], past["excerpt"], past["written"]) == (
        "unlinked",
        said,
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
