"""What one file's ingest did (the counters the pass sums) and the error text it records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FileIngestStats:
    path: str
    chunks_new: int = 0
    chunks_kept: int = 0
    chunks_moved: int = 0  # content-identical, span refreshed (file edited above them)
    chunks_tombstoned: int = 0
    symbols_new: int = 0
    symbols_moved: int = 0  # same symbol, new span/signature: row rewritten
    symbols_tombstoned: int = 0
    edges_new: int = 0
    edges_tombstoned: int = 0
    embedded: int = 0
    embed_reused: int = 0  # vectors copied from embed_reuse_from instead of embedded
    embed_stubs: int = 0
    described: int = 0
    error: str = ""
    skipped: bool = False
    edges_only: bool = False  # dependents re-extraction: chunks/symbols untouched
    # A generated file indexed for its declarations only (no chunks)
    declared_only: bool = False
    # The stages this call ran (all four unless a heal scoped it), and whether it found
    # nothing to write — an unchanged file under an extractor whose moved stages produce
    # the same rows costs its reads only.
    stages_run: tuple[str, ...] = ("chunk", "symbol", "edge", "guard")
    identical: bool = False
    misses_new: int = 0
    misses_tombstoned: int = 0
    # Caller-guard rows (the guard stage)
    guards_new: int = 0
    guards_tombstoned: int = 0
    # same site key, different verdict: `disposition` / `candidates` rewritten (an
    # `ambiguous` count goes stale otherwise)
    misses_refreshed: int = 0
    # Non-empty when the denylist refused the path (skipped is True too);
    # the reason string is what the worker/CLI counters and logs show.
    excluded: str = ""
    # qualified_name/signature/symbol_id/path of symbols (re)defined in this
    # file — the context-pack builder's input.
    symbol_summaries: list[dict[str, Any]] = field(default_factory=list)
    # What this ingest changed in the NAME TABLE other files resolve against: bare names
    # of symbols that appeared / disappeared and the ids the tombstones retired. The
    # commit's dependents set is derived from these, so an edge from an untouched file
    # into this one is re-resolved instead of outliving its target.
    symbol_names_added: list[str] = field(default_factory=list)
    symbol_names_removed: list[str] = field(default_factory=list)
    symbols_tombstoned_ids: list[str] = field(default_factory=list)
    # The headers whose indexed chunks the edge stage read to declare this source file's
    # fields and aliases (sibling first), and how many of those reads went to the
    # database (the rest hit the pass cache).
    header_paths: tuple[str, ...] = ()
    headers_read: int = 0
    # The symbols this file's calls reach after the edge stage — the current parse's
    # targets and the retired edges' — so the commit's risk refresh covers the files
    # whose amp just moved.
    callee_symbol_ids: list[str] = field(default_factory=list)


@dataclass
class EmbedResult:
    vectors: dict[int, list[float]] = field(default_factory=dict)  # idx -> vec
    failures: dict[int, str] = field(default_factory=dict)


_ERROR_MAX_CHARS = 200


def error_text(stage: str, exc: BaseException) -> str:
    """Bounded `stage: ExcClass: message` for `code_files.index_error`.

    Bounded because the column is read back verbatim by `code.index_errors`
    and the dashboard's Health tile, and a RecursionError's message (or a parser's
    dump of the offending source) is not something to store per file.
    """
    msg = str(exc).replace("\n", " ").strip()
    return f"{stage}: {type(exc).__name__}: {msg[:_ERROR_MAX_CHARS]}"
