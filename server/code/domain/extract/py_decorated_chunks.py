"""Python function chunks begin at the decorators owned by their syntax tree."""

from __future__ import annotations

from dataclasses import replace

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
    # Only the chunk copy starts earlier. The compiler join and symbol rows keep the def line.
    symbols = [
        replace(symbol, start_line=starts[symbol]) if symbol in starts else symbol
        for symbol in parsed.symbols
    ]
    return chunk_file(replace(parsed, symbols=symbols), max_chars=max_chars)
