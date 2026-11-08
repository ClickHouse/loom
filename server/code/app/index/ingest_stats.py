"""What one file's ingest did (the counters the pass sums) and the error text it records."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.core.errors import caller_text, stored_caller_text


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
    """`stage: <caller_text>` for `code_files.index_error`, which `code.index_errors`
    and the dashboard's Health tile read back to a viewer: a stage's exception can be
    a ClickHouse error with its statement, so the kind only. Each caller logs the text."""
    return f"{stage}: {caller_text(exc)}"[:_ERROR_MAX_CHARS]


# The stage of a file skipped for its size; its reason is Loom's own words.
SKIPPED = "skipped"
# The stages `error_text` is called with.
_STAGES = frozenset({"parse", "chunk", "edges"})
# Two failed stages are `; `-joined; a Loom message's own `; ` is no split.
_STAGE_SPLIT = re.compile(r"; (?=(?:parse|chunk|edges): )")


def shown_index_error(stored: str) -> str:
    """`code_files.index_error` as a viewer reads it. A skipped file's reason is kept
    whole; any other keeps each stage (`; `-joined when two failed) and `caller_text`'s
    shape of its error, since an error stored as `<stage>: <Kind>: <text>` can carry a
    ClickHouse statement."""
    if stored.startswith(f"{SKIPPED}: "):
        return stored
    shown = []
    for i, part in enumerate(_STAGE_SPLIT.split(stored)):
        stage, _, rest = part.partition(": ")
        if stage not in _STAGES:
            # A row with no stage is a bare error; a later part, a fragment of driver text.
            if i == 0:
                shown.append(stored_caller_text(part))
            continue
        error = stored_caller_text(rest)
        shown.append(f"{stage}: {error}" if error else stage)
    return "; ".join(s for s in shown if s)
