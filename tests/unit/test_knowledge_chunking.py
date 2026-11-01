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


def test_a_fence_closes_only_on_its_own_run_at_least_as_long() -> None:
    text = "# A\n\n````md\n```bash\n# not a heading\n```\n````\n\n# B\n\nbody\n"
    assert [s.path for s in detect_sections(text)] == [("A",), ("B",)]
    assert [c.section_path for c in chunk_document(text, size=500, stride=400)] == [("A",), ("B",)]


def test_crlf_headings_are_sections_with_clean_titles() -> None:
    text = "Intro\r\n=====\r\n\r\nbody\r\n\r\n## Closed ##\r\n\r\nmore\r\n"
    assert [s.path for s in detect_sections(text)] == [("Intro",), ("Intro", "Closed")]


def test_a_fence_line_with_an_info_string_does_not_close_the_fence() -> None:
    text = "# A\n\n```md\n```python\n# not a heading\n```\n\n# B\n"
    assert [s.path for s in detect_sections(text)] == [("A",), ("B",)]


# Heading, fence and HTML-block rules, after CommonMark.


def test_a_bold_line_over_a_rule_is_a_heading() -> None:
    assert [s.path for s in detect_sections("**Warning**\n---\n\ntext\n")] == [("Warning",)]


def test_a_rule_over_a_rule_is_no_heading() -> None:
    for text in (
        "***\n---\n\ntext\n",
        "---\n---\n\ntext\n",
        "intro\n\n_ _ _\n---\n",
        "## #\n---\n\ntext\n",  # an empty ATX heading is never a setext title
        "## \n===\n",
    ):
        assert all(s.path == () for s in detect_sections(text)), text


def test_an_underline_indented_up_to_three_spaces_is_an_underline() -> None:
    for text in ("Title\n  ---\n\ntext\n", "Title\n   ===\n\ntext\n"):
        assert [s.path for s in detect_sections(text)] == [("Title",)]
    assert not any(s.path for s in detect_sections("Title\n    ---\n")), "four spaces is code"
    assert not any(s.path for s in detect_sections("Title\n\t---\n")), "a tab is four columns"


def test_a_list_item_over_a_rule_is_no_heading() -> None:
    for text in ("1. Overview\n-----------\n", "2) Setup\n=====\n", "+ x\n---\n", "-\n---\n"):
        assert all(s.path == () for s in detect_sections(text)), text


def test_an_html_block_line_over_a_rule_is_no_heading() -> None:
    for text in (
        '<p align="center">\n  <img src="logo.svg">\n</p>\n---\n\nIntro\n',
        "</div>\n---\n\nbody\n",
        "<!-- x -->\n---\n\nbody\n",
        "<!--\nTODO\n-->\n---\n\nbody\n",  # a comment's closing line
        '<p align="center"><img src="logo.svg"></p>\n---\n\nIntro\n',  # a block tag opens it
        "<div></div>\n---\n",
        "<p>Title</p>\n---\n",
        "<pre>x</pre>\n---\n",  # a raw-text block
        "<!DOCTYPE html>\n---\n",
        "<!--\n\nnote\n\n-->\n---\n\nbody\n",
        "<!-- note --> Install\n---\n\nbody\n",
        # A line ending in `-->` may close a comment: a line-based splitter cannot tell.
        "Next step -->\n---\n\nbody\n",
    ):
        assert not any(s.path for s in detect_sections(text)), text
    assert [s.path for s in detect_sections("<b>Warning</b>\n---\n\nx\n")] == [("Warning",)]


def test_an_html_block_ends_at_its_end_marker() -> None:
    """A comment, `<?`, `<!` or raw-text block ends on the line holding its end
    marker and interrupts a paragraph, so the line after it can be a title."""
    for text in (
        "<!-- toc -->\nInstall\n---\n\nbody\n",
        "Intro\n<!-- c -->\nInstall\n---\n\nbody\n",
        "<!--\nTODO\n-->\nInstall\n---\n\nbody\n",
        "<pre>\nx\n</pre>\nInstall\n---\n\nbody\n",
        "<!DOCTYPE html>\nInstall\n---\n\nbody\n",
        "<!-->\nInstall\n---\n\nbody\n",  # an opener that is its own end
        "<!--->\nInstall\n---\n\nbody\n",
        "<?>\nInstall\n---\n\nbody\n",
    ):
        assert [s.path for s in detect_sections(text) if s.path] == [("Install",)], text


def test_an_autolink_in_a_heading_keeps_its_url_in_the_section_path() -> None:
    assert [s.path for s in detect_sections("## See <https://loom.dev>\n\nx\n")] == [
        ("See https://loom.dev",)
    ]


def test_a_bare_hash_is_an_empty_heading_and_never_a_setext_title() -> None:
    for text in ("#\n---\n\nbody\n", "##\n===\n"):
        assert not any(s.path for s in detect_sections(text)), text


def test_a_long_heading_keeps_its_text_and_reads_markup_in_a_bounded_head() -> None:
    from server.knowledge.domain import chunking

    for line in ("*a " * 30000, "<b>x " * 20000, "`a" * 30000):
        head, rest = line[: chunking._TITLE_MAX], line[chunking._TITLE_MAX :]
        title = chunking._clean_title(line)
        assert title.startswith(chunking._clean_title(head).rsplit(" ", 1)[0])
        assert title.endswith(" ".join(rest.split())[-200:])
    long = "word " * 200 + "tail"
    (section,) = [s for s in detect_sections(f"# {long}\n\nbody\n") if s.path]
    assert section.path[0].endswith("tail"), "no text of a long heading is dropped"


def test_a_heading_line_with_a_long_run_of_blanks_is_read_whole() -> None:
    line = "# a" + " " * 40_000 + "b ##"
    assert [s.path for s in detect_sections(line + "\n\nx\n") if s.path] == [("a b",)]


def test_hostile_markdown_lines_chunk_in_bounded_time() -> None:
    import time

    for line in (
        "`" * 1_000_000 + "a`",  # a 1 MB backtick run with a trailing backtick
        "# a" + " " * 200_000 + "b",  # a heading's blanks before its closing run
        "# " + "*a " * 200_000,  # unclosed emphasis
        "<div>\n" + "===\n" * 50_000,  # an HTML line over many underlines
        "<br />" * 40 + "x\n---\n",  # self-closing tags a tag-only pattern could try 2^n ways
        "<img src=a /> " * 40 + "text\n---\n",
        "<pre " + "<pre\n" * 100_000,  # a raw-text block never closed
    ):
        start = time.perf_counter()
        chunk_document(line, size=1500, stride=1200)
        assert time.perf_counter() - start < 5.0, line[:20]
