"""Structure-aware chunking: split on headings, window inside
sections, breadcrumb into the embed text; unstructured text unchanged."""

from __future__ import annotations

import json

from server.knowledge.domain.chunking import (
    chunk_document,
    detect_sections,
    outline,
    window,
)

DOC = """Preamble paragraph before any heading.

# Chapter 1: Governance {#gov}

Intro to chapter one.

## 1.1 Institutions

Institutions text line one.
Institutions text line two.

### 1.1.1 Electoral Systems

The plurality voting system awards the seat to the candidate with the most votes.

```python
# not a heading: a comment inside a fence
x = 1
```

## 1.2 Parties

Parties text.

Setext Heading
==============

Setext body.

<h2>HTML heading</h2>
Html body.
"""


def test_detect_sections_builds_breadcrumbs_and_ignores_fenced_hashes() -> None:
    secs = detect_sections(DOC)
    paths = [s.path for s in secs]
    assert paths[0] == ()  # preamble
    assert ("Chapter 1: Governance",) in paths  # anchor `{#gov}` stripped
    assert ("Chapter 1: Governance", "1.1 Institutions", "1.1.1 Electoral Systems") in paths
    assert ("Chapter 1: Governance", "1.2 Parties") in paths
    assert ("Setext Heading",) in paths
    assert ("Setext Heading", "HTML heading") in paths
    # the `# not a heading` inside the fence did not open a section
    assert not any("not a heading" in " ".join(p) for p in paths)
    electoral = next(s for s in secs if s.path[-1:] == ("1.1.1 Electoral Systems",))
    assert "plurality voting" in "\n".join(electoral.lines)
    assert "# not a heading" in "\n".join(electoral.lines)  # fence content stays in the body


def test_chunk_document_sections_never_straddle_a_heading() -> None:
    chunks = chunk_document(DOC, size=60, stride=50)
    assert [c.idx for c in chunks] == list(range(len(chunks)))  # dense, in order
    for c in chunks:
        # a chunk's text never contains a heading line of another section
        assert not any(
            line.startswith("#") and "not a heading" not in line for line in c.text.split("\n")
        )
    by_path = {c.section_path for c in chunks}
    assert ("Chapter 1: Governance", "1.1 Institutions", "1.1.1 Electoral Systems") in by_path
    electoral = [c for c in chunks if c.section_path[-1:] == ("1.1.1 Electoral Systems",)]
    assert len(electoral) >= 2  # long section windowed inside itself
    assert (
        electoral[0].breadcrumb
        == "Chapter 1: Governance > 1.1 Institutions > 1.1.1 Electoral Systems"
    )
    assert electoral[0].meta["section_depth"] == 3
    assert json.dumps(electoral[0].meta, sort_keys=True)  # serialisable for chunk_metadata
    pre = [c for c in chunks if not c.section_path]
    assert pre and "Preamble" in pre[0].text and pre[0].meta == {}


def test_unstructured_text_chunks_as_one_window_over_the_text() -> None:
    text = "lorem ipsum " * 400
    chunks = chunk_document(text, size=1500, stride=1200)
    assert [(s, t) for s, t in window(text, size=1500, stride=1200)] == [
        (c.char_start, c.text) for c in chunks
    ]
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    assert all(c.meta == {} for c in chunks)
    # small text is one chunk; empty content is one empty chunk (as before)
    assert [c.text for c in chunk_document("short", size=1500, stride=1200)] == ["short"]
    assert [c.text for c in chunk_document("", size=1500, stride=1200)] == [""]


def test_headings_only_document_keeps_one_chunk() -> None:
    chunks = chunk_document("# A\n## B\n### C\n", size=100, stride=80)
    assert len(chunks) == 1 and chunks[0].idx == 0


def test_outline_lists_sections_in_document_order_with_chunk_indices() -> None:
    chunks = chunk_document(DOC, size=60, stride=50)
    toc = outline(chunks)
    titles = [e["title"] for e in toc]
    assert titles[:4] == [
        "Chapter 1: Governance",
        "1.1 Institutions",
        "1.1.1 Electoral Systems",
        "1.2 Parties",
    ]
    electoral = next(e for e in toc if e["title"] == "1.1.1 Electoral Systems")
    assert electoral["depth"] == 3 and len(electoral["chunk_idxs"]) >= 2  # type: ignore[arg-type]


def test_augment_for_embedding_adds_the_breadcrumb_after_title_and_description() -> None:
    from server.knowledge.app.writes import _augment_for_embedding

    out = _augment_for_embedding("body", title="Book", description="desc", section="A > B")
    assert out == "Book\ndesc\nA > B\n\nbody"
    assert _augment_for_embedding("body", section="A > B") == "A > B\n\nbody"
    assert _augment_for_embedding("body") == "body"


def test_chunk_text_uses_the_size_and_stride_settings(monkeypatch) -> None:
    from server.knowledge.app import writes as h
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_chunk_size", 40)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 30)
    structured = h._chunk_text(DOC)
    assert structured == chunk_document(DOC, size=40, stride=30)
    assert any(c.section_path for c in structured)


def test_a_stride_past_the_size_steps_one_window_so_no_text_is_skipped(monkeypatch) -> None:
    """The two settings change one at a time: a size lowered under the stride must not
    leave the text between windows unindexed."""
    from server.knowledge.app import writes as h
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_chunk_size", 40)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 55)
    assert h._chunk_text(DOC) == chunk_document(DOC, size=40, stride=40)
    assert h._chunk_text(DOC) != chunk_document(DOC, size=40, stride=55)
