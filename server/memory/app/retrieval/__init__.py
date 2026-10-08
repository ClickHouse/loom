"""memory.search vector-ranking service.

Retrieves memories by semantic similarity: query embedding, vector +
filter SQL against loom.memory_embeddings FINAL, and an additive
(non-gating) entity-filter boost so entity-named queries surface tagged
rows even when phrasing is weak.

search_trace reuses the same SQL shape with extra columns; keep the two
paths in lockstep when ranking changes. The handler owns input
validation, Pydantic output, and tracing; the service owns the query.

Modules, each importing only from those listed before it:

- `retry`: the fresh-part empty-block retry and the per-leg span.
- `key_axis`: whether the fact leg also ranks by the key embedding.
- `lexical`: the lexical, IDF and BM25 score expressions.
- `fact_rank`: the fact leg's rank terms and per-mode rank expression.
- `fact_sql`: the fact leg's statement.
- `chunks`: the verbatim-chunk leg.
- `side_legs`: the lexical pool, records and unmatched-candidate legs.
- `fusion`: ordering the fused pool, RRF included.
- `pool`: the pool's last steps (set-chunk fold, recency, chunk share).
- `search`: `vector_search` and `vector_search_trace`, which run the rest.
- `graph`, `summaries`, `derived`: the legs the handler calls directly.

This module exports what the handler and operations layers call; everything
else is imported from the submodule that defines it.
"""

from __future__ import annotations

from server.memory.app.retrieval.derived import (
    fetch_current_state_observation,
    fetch_derived_aggregate,
    fetch_temporal_diff,
)
from server.memory.app.retrieval.graph import (
    fetch_graph_neighbors,
    fetch_triple_matched_memories,
)
from server.memory.app.retrieval.key_axis import key_axis_auto_cached
from server.memory.app.retrieval.pool import chunk_share
from server.memory.app.retrieval.retry import _is_empty_block_column_error
from server.memory.app.retrieval.search import vector_search, vector_search_trace
from server.memory.app.retrieval.summaries import (
    fetch_entity_summaries,
    fetch_session_summaries,
    summary_entities_present,
)
from server.memory.domain.search_types import SearchHit, SearchQuery, SearchTraceHit

__all__ = [
    "SearchHit",
    "SearchQuery",
    "SearchTraceHit",
    "_is_empty_block_column_error",
    "chunk_share",
    "fetch_current_state_observation",
    "fetch_derived_aggregate",
    "fetch_entity_summaries",
    "fetch_graph_neighbors",
    "fetch_session_summaries",
    "fetch_temporal_diff",
    "fetch_triple_matched_memories",
    "key_axis_auto_cached",
    "summary_entities_present",
    "vector_search",
    "vector_search_trace",
]
