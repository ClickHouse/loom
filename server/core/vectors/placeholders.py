"""Fixed-length placeholders for missing vectors.

The `Quantized(...)` codec demands that every row of a coded
column carries exactly the declared dimension (else code 190), and
`loom.memory_embeddings` holds stub rows whose `embedding` is empty
(`embedding_dim = 0`, the embed worker's queue) and rows whose
`memory_key_embedding` is `[]`. A coded table cannot hold them, and any
writer emitting an empty vector would fail its INSERT.

Every insert into
`loom.memory_embeddings` replaces an empty vector with a zero vector of
`settings.embedding_dim` elements. The `*_dim` columns stay what the writer
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


def pad_rows(table: str, rows: list[dict[str, Any]]) -> int:
    """Replace empty vectors in `rows` (in place) for a table that has
    placeholder columns. Returns the number of vectors replaced; 0 for every
    other table or when nothing was empty."""
    from server.settings import settings

    full = table if "." in table else f"loom.{table}"
    cols = PLACEHOLDER_COLUMNS.get(full)
    dim = int(settings.embedding_dim)
    if not cols:
        # A second model's chunk table carries that model's
        # width; the map says which, and the placeholder must match it.
        if not is_chunks_table(full):
            return 0
        cols = PLACEHOLDER_COLUMNS["loom.code_chunks"]
        dim = vector_dim_for_table(full) or dim
    if dim <= 0:
        return 0
    n = 0
    for r in rows:
        for c in cols:
            if c in r and not r[c]:
                r[c] = placeholder(dim)
                n += 1
    return n
