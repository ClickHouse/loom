"""Python function chunks begin at the decorators owned by their syntax tree."""

from __future__ import annotations

from server.code.domain.extract.chunker import Chunk, chunk_file
from server.code.domain.extract.edges_treesitter import _caller_spans
from server.code.domain.extract.treesitter import ParsedFile


def python_chunks(parsed: ParsedFile, *, max_chars: int) -> list[Chunk]:
    functions = [symbol for symbol in parsed.symbols if symbol.kind == "function"]
    starts = {
        symbol: start
        for start, _, symbol in _caller_spans(parsed, functions)
        if start < symbol.start_line
    }
    return chunk_file(parsed, max_chars=max_chars, prefix_starts=starts)
