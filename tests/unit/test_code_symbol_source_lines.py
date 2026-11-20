"""Extracted source and hydrated bodies use the parser's physical line coordinates."""

from __future__ import annotations

import pytest

from server.code.app.index.ingest_chunks import _chunks_for
from server.code.app.index.ingest_parse import parse_for_ingest
from server.code.domain.symbol_body import complete_source_span


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
