"""Callee outline for a function chunk.

What a function calls is the shortest description of what it does, and
who calls it is where it matters. `context_text.build_context_text` can
carry both as head lines; this module derives them:

  * callees: the chunk's own call sites (`edges_treesitter.call_sites`,
    bucketed by line into the chunk's span), in first-seen order, through
    `context_text.filter_callees` (built-in stoplist + an optional
    corpus-derived df stoplist) — at most MAX_CALLEES names;
  * callers: one `code_edges` read by `to_symbol_id` for every function
    chunk of the file (the callers direction walks the primary key), the
    parser tier at confidence >= CALLER_MIN_CONFIDENCE or the compiler tier,
    sorted caller qualified names, at most MAX_CALLERS per chunk.

The caller read may address another namespace (`callers_namespace`): a
bench twin holds one prefix of the tree, and its callers live in the full
index. Symbol ids hash (qualified name, signature) and carry no namespace,
so they match across.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from server.code.domain.extract.chunker import Chunk
from server.code.domain.extract.context_text import (
    MAX_CALLERS,
    filter_callees,
)

CALLER_MIN_CONFIDENCE = 0.8
# Rows read per target symbol: the cap is applied after anonymous-namespace
# and self-call filtering, so read a few more than are served.
_CALLER_ROWS_PER_SYMBOL = MAX_CALLERS * 3
_CALLER_ROWS_LIMIT = 4000

MODES = ("off", "calls", "calls_callers")


def callees_for_chunk(
    sites: Iterable[tuple[int, str]], chunk: Chunk, *, stoplist: frozenset[str] = frozenset(),
) -> list[str]:
    """Callee names whose call site lies inside the chunk's line span."""
    inside = (
        name for line, name in sites if chunk.start_line <= line <= chunk.end_line
    )
    return filter_callees(inside, stoplist=stoplist)


def df_stoplist(chunk_counts: Counter[str], n_chunks: int, *, ratio: float) -> frozenset[str]:
    """Names present in more than `ratio` of the function chunks of a corpus:
    a callee that most units call (a logger, an exception constructor) tells
    the embedding nothing about one of them."""
    if n_chunks <= 0 or ratio <= 0:
        return frozenset()
    floor = ratio * n_chunks
    return frozenset(name for name, n in chunk_counts.items() if n > floor)


async def callers_for_chunks(
    repo: Any, org: str, ns: str, chunks: list[Chunk],
) -> dict[str, list[str]]:
    """`{qualified scope -> sorted caller qualified names}` for the function
    chunks, two reads: the scopes resolved to their live symbol ids across
    BOTH tiers (`symbol_ids_for_names` — the compiler tier keys the same
    definition under a different id, so a read by the chunk's parser-tier
    ids alone would see only parser edges, mostly at confidence 0.6), then
    one caller hop by `to_symbol_id`, grouped back by `to_qualified_name`.
    Empty on any read error: the outline is a retrieval aid, never a reason
    to fail an ingest."""
    scopes = sorted({c.scope for c in chunks if c.kind == "function" and c.scope})
    if not scopes:
        return {}
    try:
        by_name = await repo.symbol_ids_for_names(org, ns, scopes)
        ids = sorted({sid for c in chunks if c.kind == "function" for sid in c.symbol_ids}
                     | {sid for sids in by_name.values() for sid in sids})
        if not ids:
            return {}
        rows = await repo.edges_for_symbols(
            org, ns, ids, direction="callers", min_confidence=CALLER_MIN_CONFIDENCE,
            per_match=_CALLER_ROWS_PER_SYMBOL, limit=_CALLER_ROWS_LIMIT,
        )
    except Exception:  # noqa: BLE001 — best effort
        return {}
    by_target: dict[str, set[str]] = {}
    for r in rows:
        target = str(r.get("to_qualified_name") or "").strip()
        name = str(r.get("from_qualified_name") or "").strip()
        if not target or not name or "(anonymous" in name or name == target:
            continue
        if name.startswith("DB::"):
            name = name[4:]
        by_target.setdefault(target, set()).add(name)
    return {scope: sorted(names)[:MAX_CALLERS] for scope, names in by_target.items()}


def callers_for_chunk(chunk: Chunk, by_scope: dict[str, list[str]]) -> list[str]:
    """The chunk's caller names, its own qualified scope excluded (a
    recursive call is not a dependent)."""
    own = chunk.scope[4:] if chunk.scope.startswith("DB::") else chunk.scope
    return [n for n in by_scope.get(chunk.scope, []) if n != own][:MAX_CALLERS]
