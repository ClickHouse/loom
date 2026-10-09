"""Exhaustive vector-surface contract for a deployment-wide embedding change.

Changing a model is not equivalent to running the memory-only reembed command.
The coordinator must account for every live vector column before it may begin
rewriting data. Unknown surfaces fail closed, including operator-created tables.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

RebuildAction = Literal["reembed", "invalidate_cache", "derive_prefix", "preserve_history"]


@dataclass(frozen=True)
class EmbeddingSurface:
    table: str
    column: str
    action: RebuildAction


# This inventory is checked against the canonical schema and the live database.
# Two vectors in one row (memory text and key) are deliberately separate entries.
EMBEDDING_SURFACES = (
    EmbeddingSurface("categories", "embedding", "reembed"),
    EmbeddingSurface("chunks", "embedding", "reembed"),
    EmbeddingSurface("code_answers", "question_embedding", "reembed"),
    EmbeddingSurface("code_chunks", "embedding", "reembed"),
    EmbeddingSurface("code_chunks_prefix", "embedding", "derive_prefix"),
    EmbeddingSurface("code_summaries", "embedding", "reembed"),
    EmbeddingSurface("conversation_embeddings", "embedding", "reembed"),
    EmbeddingSurface("embedding_cache", "embedding", "invalidate_cache"),
    EmbeddingSurface("entity_summaries", "summary_embedding", "reembed"),
    EmbeddingSurface("entity_vectors", "entity_embedding", "reembed"),
    # Events are immutable observations of past operations, not the live index.
    # Rewriting their captured vectors would falsify historical evidence.
    EmbeddingSurface("events", "embedding", "preserve_history"),
    EmbeddingSurface("memory_chunks", "embedding", "reembed"),
    EmbeddingSurface("memory_embeddings", "embedding", "reembed"),
    EmbeddingSurface("memory_embeddings", "memory_key_embedding", "reembed"),
    EmbeddingSurface("qa_item_embeddings", "embedding", "reembed"),
    EmbeddingSurface("query_nodes", "embedding", "reembed"),
    EmbeddingSurface("session_summaries", "summary_embedding", "reembed"),
)

REBUILD_TABLES = tuple(
    dict.fromkeys(
        surface.table for surface in EMBEDDING_SURFACES if surface.action != "preserve_history"
    )
)

# Candidate preparation clones a table's DDL on one node; replicated and shared
# engines need their own replication-path plan before a rebuild can publish.
PUBLISHABLE_ENGINES = frozenset({"MergeTree", "ReplacingMergeTree", "AggregatingMergeTree"})


def unpublishable_refusal(tables: tuple[tuple[str, str], ...]) -> str:
    """Why a dashboard embedding change is refused, or "" when it can publish."""
    if not tables:
        return ""
    name, engine = tables[0]
    found = (
        f"{len(tables)} tables, such as loom.{name} ({engine}), use other engines"
        if len(tables) > 1
        else f"loom.{name} uses {engine}"
    )
    return (
        f"The embedding rebuild can publish only to local MergeTree tables; {found}. "
        "Change the embedding model in the server environment instead, then re-embed each "
        "index as the operations maintenance guide describes."
    )


_VECTOR_TYPE = re.compile(r"Array\((?:BFloat16|Float32|Float64)\)|QBit\(")

# These are per-result scalar scores captured for observability, not vectors in
# the provider's coordinate space. Keep the exclusions explicit and auditable.
NON_VECTOR_ARRAYS = frozenset(
    {
        ("code_search_trace", "returned_scores"),
        ("events", "scores"),
        ("search_trace_log", "returned_scores"),
    }
)


def rebuild_surfaces(columns: Iterable[Mapping[str, str]]) -> tuple[EmbeddingSurface, ...]:
    """Resolve the live inventory; never silently skip an unfamiliar vector.

    Input is system.columns metadata for physical tables only. Missing known
    tables are also errors: a partial schema cannot prove a complete rebuild.
    Scalar dimension/token counts are not vector surfaces.
    """
    actual = {
        (row["table"], row["name"]) for row in columns if _VECTOR_TYPE.search(row["type"])
    } - NON_VECTOR_ARRAYS
    expected = {(surface.table, surface.column) for surface in EMBEDDING_SURFACES}
    if actual != expected:
        unknown = ", ".join(f"{table}.{column}" for table, column in sorted(actual - expected))
        missing = ", ".join(f"{table}.{column}" for table, column in sorted(expected - actual))
        raise ValueError(
            f"Embedding rebuild inventory mismatch; unknown=[{unknown}], missing=[{missing}]"
        )
    return EMBEDDING_SURFACES
