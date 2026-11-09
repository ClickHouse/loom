"""Markdown design docs by heading."""

from __future__ import annotations

from server.code.domain.extract.lang_markdown import (
    doc_reference_edges,
    doc_reference_sites,
    markdown_chunks,
    markdown_units,
    parse_markdown,
    sections_of,
)

DOC = b"""# Ledger design

Preamble is not a section? It is under the H1.

## Commit

A record is durable when `durable` covers its LSN; `Advance` moves it.
See the [glossary](glossary.md#watermarks) and [the spec](../tla/Ledger.tla)
and [an external link](https://example.com/x).

```cpp
// a fence with a heading-looking line
# not a heading
```

### Failure

`Recover` is not modelled; `uint64_t` is noise.

## Glossary

Terms live in `Ledger::TypeOK`.
"""


def test_sections_symbols_and_links() -> None:
    pf = parse_markdown("docs/design.md", DOC)
    names = [s.qualified_name for s in pf.symbols]
    assert names == [
        "docs.design::Ledger design",
        "docs.design::Ledger design::Commit",
        "docs.design::Ledger design::Commit::Failure",
        "docs.design::Ledger design::Glossary",
    ]
    commit = pf.symbols[1]
    assert (
        commit.kind == "section"
        and commit.signature == "## Commit"
        and (commit.start_line, commit.end_line) == (5, 19)
    )
    assert commit.doc_comment.startswith("A record is durable")
    assert pf.includes == ["docs/glossary.md", "tla/Ledger.tla"], (
        "relative links become repository paths; anchors and URLs drop"
    )
    secs, pre = sections_of(DOC.decode())
    assert pre == 0 and [s.level for s in secs] == [1, 2, 3, 2]


def test_chunks_one_per_section_own_lines() -> None:
    pf = parse_markdown("docs/design.md", DOC)
    ch = markdown_chunks(pf)
    assert all(c.kind == "comment_block" for c in ch)
    assert [c.scope.split("::")[-1] for c in ch] == [
        "Ledger design",
        "Commit",
        "Failure",
        "Glossary",
    ]
    assert (ch[1].start_line, ch[1].end_line) == (5, 15), (
        "a section's chunk stops where its child heading starts"
    )
    n = DOC.count(b"\n")
    seen = [0] * (n + 2)
    for c in ch:
        for ln in range(c.start_line, c.end_line + 1):
            seen[ln] += 1
    assert all(x <= 1 for x in seen) and sum(seen) == n


def test_long_section_splits_at_paragraphs_and_marks_continuations() -> None:
    body = b"# T\n\n" + b"\n\n".join(b"para %d " % i + b"x" * 500 for i in range(20)) + b"\n"
    pf = parse_markdown("docs/long.md", body)
    ch = markdown_chunks(pf, max_chars=1500)
    assert len(ch) > 3 and all(len(c.content) <= 1500 + 60 for c in ch)
    assert (
        ch[0].symbol_ids and not ch[1].symbol_ids and ch[1].content.startswith("<!-- T (cont.) -->")
    )


def test_reference_edges_read_backticked_names_once_each() -> None:
    pf = parse_markdown("docs/design.md", DOC)
    sites = doc_reference_sites(pf)
    assert {n for _f, n, _l in sites} == {"durable", "Advance", "Recover", "Ledger::TypeOK"}, (
        "noise types and fence lines are skipped"
    )
    table = {
        "durable": [("v1", "Ledger::durable", "variable")],
        "Advance": [("o1", "Ledger::Advance", "operator")],
        "Recover": [("a", "X::Recover", "function"), ("b", "Y::Recover", "function")],
        "TypeOK": [("t1", "Ledger::TypeOK", "operator"), ("t2", "Other::TypeOK", "operator")],
    }
    edges = doc_reference_edges(pf, sites, table)
    got = sorted(
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name, e.edge_kind, e.confidence)
        for e in edges
    )
    assert got == [
        ("Commit", "Ledger::Advance", "reads", 0.7),
        ("Commit", "Ledger::durable", "reads", 0.7),
        ("Glossary", "Ledger::TypeOK", "reads", 0.7),
    ], "an ambiguous bare name binds nothing; a qualified mention picks its suffix"


def test_a_long_qualified_name_is_cut_and_stays_distinct() -> None:
    # Every section's qualified name repeats its ancestors' titles.
    text = "# " + "x" * 600 + "\n## " + "y" * 600 + "\n### a\n### b\n"
    syms, _inc, _refs, _pre = markdown_units("a.md", text)
    names = [s.qualified_name for s in syms]
    assert [len(n) for n in names] == [603, 1000, 1000, 1000]
    assert names[1].startswith("a::" + "x" * 600 + "::y") and len(set(names)) == 4


def test_closing_hashes_come_off_and_a_heading_of_hashes_keeps_one() -> None:
    secs, _ = sections_of("## Title ##\n# #\n")
    assert [s.title for s in secs] == ["Title", "#"]


def test_long_headings_that_differ_after_the_cut_stay_distinct() -> None:
    text = "# t\n## " + "a" * 1000 + "x\n## " + "a" * 1000 + "y\n"
    syms, _inc, _refs, _pre = markdown_units("a.md", text)
    assert len({s.qualified_name for s in syms}) == 3
    assert all(len(s.name) == 1000 for s in syms[1:])
