"""Chunker contract: whole semantic units, never a cut window.

GATE: a `function` chunk always contains the function's
signature line, and no function body line ever lands in a chunk without
its header — the failure mode this prevents is the classic "RAG cut the
function in half" wrong-answer generator.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter", reason="code extras not installed")

from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.treesitter import parse_cpp

_SRC = b"""#include <memory>

/// File docs.

namespace DB
{

/// A class.
class Squasher
{
public:
    void flush()
    {
        squashImpl();
    }

    void squashImpl() { work(); }

    size_t rows = 0;
};

void freeFunction(int x)
{
    process(x);
    more(x);
}

}
"""


def _parsed():
    return parse_cpp("src/Interpreters/Squasher.cpp", _SRC)


def test_function_chunks_are_whole() -> None:
    chunks = chunk_file(_parsed())
    fns = [c for c in chunks if c.kind == "function"]
    scopes = {c.scope for c in fns}
    assert {"DB::Squasher::flush", "DB::Squasher::squashImpl", "DB::freeFunction"} <= scopes
    for c in fns:
        # signature present and braces balanced => complete unit
        assert "(" in c.content and ")" in c.content
        assert c.content.count("{") == c.content.count("}")


def test_class_skeleton_elides_method_bodies() -> None:
    chunks = chunk_file(_parsed())
    skel = next(c for c in chunks if c.kind == "class_skeleton")
    assert "class Squasher" in skel.content
    assert "body elided" in skel.content
    assert "squashImpl();" not in skel.content  # flush() body gone
    assert "size_t rows = 0;" in skel.content  # data members stay


def test_file_header_chunk() -> None:
    chunks = chunk_file(_parsed())
    header = next(c for c in chunks if c.kind == "file_header")
    assert header.start_line == 1
    assert "#include <memory>" in header.content


def test_oversized_function_splits_with_reprefixed_header() -> None:
    body_lines = "".join(f"    stmt_{i}(x);\n" for i in range(200))
    src = f"void bigFn(int x)\n{{\n{body_lines}}}\n".encode()
    pf = parse_cpp("src/big.cpp", src)
    chunks = [c for c in chunk_file(pf, max_chars=1000) if c.kind == "function"]
    assert len(chunks) > 1
    for later in chunks[1:]:
        assert "continued from" in later.content.splitlines()[0]
    # line ranges tile the function without gaps
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start_line == prev.end_line + 1


def test_chunk_id_deterministic_and_content_addressed() -> None:
    a = chunk_file(_parsed())
    b = chunk_file(_parsed())
    assert [c.chunk_id("ns") for c in a] == [c.chunk_id("ns") for c in b]
    # content change => id change (content-addressed)
    changed = parse_cpp("src/Interpreters/Squasher.cpp", _SRC.replace(b"work()", b"work2()"))
    ids_a = {c.chunk_id("ns") for c in a}
    ids_c = {c.chunk_id("ns") for c in chunk_file(changed)}
    assert ids_a != ids_c
    # ...but only for the touched unit; untouched chunks keep their ids
    assert ids_a & ids_c


def test_fallback_chunks_for_non_cpp() -> None:
    from server.code.domain.extract.treesitter import ParsedFile

    lines = "".join(f"line {i} of a config file with words\n" for i in range(300))
    pf = ParsedFile(path="tests/config.xml", lang="config", source=lines.encode())
    chunks = chunk_file(pf)
    assert chunks
    assert all(c.kind == "other" for c in chunks)
    assert chunks[0].start_line == 1


def test_every_chunk_has_lexemes() -> None:
    for c in chunk_file(_parsed()):
        assert c.lexemes
