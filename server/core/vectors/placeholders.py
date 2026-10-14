"""Fixed-length placeholders for missing vectors.

The `Quantized(...)` codec demands that every row of a coded
column carries exactly the declared dimension (else code 190), and
`loom.memory_embeddings` holds stub rows whose `embedding` is empty
(`embedding_dim = 0`, the embed worker's queue) and rows whose
`memory_key_embedding` is `[]`. A coded table cannot hold them, and any
writer emitting an empty vector would fail its INSERT.

Every insert into
`loom.memory_embeddings` replaces an empty vector with a zero vector of the
width its coded column was created at (`settings.embedding_dim` before the
columns are probed). The `*_dim` columns stay what the writer
set (0 for a stub), and they are what reads trust: every leg filters
`embedding_dim > 0`, and the key axis takes the key vector
only when `memory_key_embedding_dim` matches the query. A zero vector is
therefore never scored. Cost: ~6 KB per placeholder row (bf16 × 3072).

The codec rebuild runner refuses to swap while any row written in the last hour still
carries an empty vector.
"""

from __future__ import annotations

from typing import Any

from server.core.vectors.code_tables import is_chunks_table, vector_dim_for_table

PLACEHOLDER_COLUMNS: dict[str, tuple[str, ...]] = {
    "loom.memory_embeddings": ("embedding", "memory_key_embedding"),
    # Every vector table moves onto the coded layout, so every
    # vector column gets the same placeholder rule. All carry embedding_dim
    # vectors of the one configured model.
    "loom.memory_chunks": ("embedding",),
    "loom.code_chunks": ("embedding",),
    "loom.entity_vectors": ("entity_embedding",),
    "loom.session_summaries": ("summary_embedding",),
    "loom.conversation_embeddings": ("embedding",),
    "loom.chunks": ("embedding",),
    "loom.entity_summaries": ("summary_embedding",),
    # code_answers writes a stub row with an empty question_embedding
    # (code_repo defaults), and an importer fills categories from a foreign
    # export.
    "loom.code_answers": ("question_embedding",),
    "loom.categories": ("embedding",),
    # The file cards fill code_summaries' vector; a placeholder for the
    # structural rows (about a quarter carry `[]`) is what lets the table take
    # the codec.
    "loom.code_summaries": ("embedding",),
    # The tracker_embed pass always writes a full vector; the rule keeps it that
    # way on the coded column.
    "loom.qa_item_embeddings": ("embedding",),
}


def placeholder(dim: int) -> list[float]:
    return [0.0] * int(dim)


def _columns(table: str) -> tuple[str, tuple[str, ...], int]:
    """(qualified table, placeholder columns, width when not coded)."""
    from server.settings import settings

    full = table if "." in table else f"loom.{table}"
    dim = int(settings.embedding_dim)
    if full in PLACEHOLDER_COLUMNS:
        return full, PLACEHOLDER_COLUMNS[full], dim
    if is_chunks_table(full):
        # A second model's chunk table carries that model's
        # width; the map says which, and the placeholder must match it.
        return full, PLACEHOLDER_COLUMNS["loom.code_chunks"], vector_dim_for_table(full) or dim
    return full, (), dim


def _pads_coded(table: str, rows: list[dict[str, Any]]) -> bool:
    """Whether `pad_rows` would size a placeholder from a coded column's width."""
    from server.core.vectors.codes_state import stored_width

    full, cols, _ = _columns(table)
    return any(stored_width(full, c) and any(c in r and not r[c] for r in rows) for c in cols)


def pad_rows(table: str, rows: list[dict[str, Any]]) -> int:
    """Replace empty vectors in `rows` (in place) for a table that has
    placeholder columns. Returns the number of vectors replaced; 0 for every
    other table or when nothing was empty."""
    from server.core.vectors.codes_state import stored_width

    full, cols, dim = _columns(table)
    n = 0
    for c in cols:
        # A placeholder is never scored: it takes the width its coded column was
        # created at, so a write without a vector is never refused for its width.
        width = stored_width(full, c) or dim
        if width <= 0:
            continue
        for r in rows:
            if c in r and not r[c]:
                r[c] = placeholder(width)
                n += 1
    return n


async def fit_vectors(table: str, rows: list[dict[str, Any]]) -> None:
    """Refuse a vector its coded column cannot take, then pad the empty ones
    (in place), both against a recent reading when a coded column is padded."""
    from server.core.vectors import codes_state

    if _pads_coded(table, rows):
        await codes_state.recheck()
    await codes_state.refuse_other_widths(table, rows)  # skips the empty vectors
    pad_rows(table, rows)
