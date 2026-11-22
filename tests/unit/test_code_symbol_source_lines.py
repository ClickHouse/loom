"""Extracted source and hydrated bodies use the parser's physical line coordinates."""

from __future__ import annotations

import pytest

from server.code.app.index.ingest_chunks import _chunks_for
from server.code.app.index.ingest_parse import parse_for_ingest
from server.code.domain.symbol_body import complete_source_span


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("merge_target", [0, 3000])
@pytest.mark.parametrize(
    ("path", "text", "class_name"),
    [
        (
            "src/example.py",
            'class Group:\n    """A group.\n\n    Details.\n    """\n\n'
            "    def first(self):\n        return 1\n\n    \n"
            "    def second(self):\n        return 2\n",
            "Group",
        ),
        (
            "src/example.cpp",
            "struct Group {\n\n    int first() { return 1; }\n\n    \n"
            "    int second() { return 2; }\n\n};\n",
            "Group",
        ),
    ],
    ids=["python", "cpp"],
)
def test_container_bodies_retain_blank_source_between_members(
    newline: str, merge_target: int, path: str, text: str, class_name: str
) -> None:
    from server.code.domain.extract.chunker import chunk_file

    text = text.replace("\n", newline)
    parsed, error = parse_for_ingest(path, text.encode())
    assert parsed is not None and not error
    symbol = next(s for s in parsed.symbols if s.qualified_name == class_name)
    chunks = chunk_file(parsed, merge_target_chars=merge_target)
    rows = [
        {
            "kind": c.kind,
            "content": c.content,
            "start_line": c.start_line,
            "end_line": c.end_line,
        }
        for c in chunks
        if c.kind != "class_skeleton"
        and c.start_line <= symbol.end_line
        and c.end_line >= symbol.start_line
    ]
    assert complete_source_span(rows, symbol.start_line, symbol.end_line, max_chars=0) == text


@pytest.mark.parametrize("separator", ["", "\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("name", ["f", "g"])
def test_extraction_keeps_unicode_text_and_later_function_coordinates(
    separator: str, newline: str, name: str
) -> None:
    expected = {
        "f": f'void f() {{ const char *value = "before{separator}after"; work(); }}{newline}',
        "g": f"void g() {{ later(); }}{newline}",
    }
    source = (expected["f"] + expected["g"]).encode()
    parsed, error = parse_for_ingest("src/example.cpp", source)
    assert parsed is not None and not error
    chunks, _, _ = _chunks_for(parsed, "src/example.cpp", source, 6000)
    symbol = next(s for s in parsed.symbols if s.qualified_name == name)
    rows = [
        {
            "kind": chunk.kind,
            "content": chunk.content,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
        }
        for chunk in chunks
        if chunk.kind != "class_skeleton"
        and chunk.start_line <= symbol.end_line
        and chunk.end_line >= symbol.start_line
    ]
    assert (
        complete_source_span(rows, symbol.start_line, symbol.end_line, max_chars=12000)
        == expected[name]
    )


@pytest.mark.parametrize("separator", ["", "\u0085", "\u2028", "\u2029"])
def test_oversized_pieces_keep_the_same_physical_source_lines(separator: str) -> None:
    text = (
        "void f() {\n"
        + "".join(f'    consume("{str(n) * 32}{separator}tail");\n' for n in range(8))
        + "}\n"
    )
    source = text.encode()
    parsed, error = parse_for_ingest("src/example.cpp", source)
    assert parsed is not None and not error
    chunks, _, _ = _chunks_for(parsed, "src/example.cpp", source, 180)
    pieces = [chunk for chunk in chunks if chunk.kind == "function"]
    assert len(pieces) > 1
    rows = [
        {
            "kind": chunk.kind,
            "content": chunk.content,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
        }
        for chunk in pieces
    ]
    symbol = next(s for s in parsed.symbols if s.qualified_name == "f")
    assert complete_source_span(rows, symbol.start_line, symbol.end_line, max_chars=12000) == text


@pytest.mark.parametrize(
    "separator", ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\u0085", "\u2028", "\u2029"]
)
def test_an_ambiguous_retained_prefix_is_not_a_complete_single_line(separator: str) -> None:
    rows = [
        {
            "kind": "function",
            "content": f'void f() {{ const char *value = "before{separator}',
            "start_line": 1,
            "end_line": 1,
        }
    ]
    assert complete_source_span(rows, 1, 1, max_chars=12000) == ""
