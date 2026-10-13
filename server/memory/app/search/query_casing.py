"""The query text a search embeds: its lowercase words in the spelling the
rows it may read use most. An embedder reads `datalakecatalog` and
`DataLakeCatalog` as different strings, so the vector leg otherwise misses
rows that spell an identifier in CamelCase."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.core.ch import get_client, statement_timeout
from server.memory.app.served_superseded import served_flags as _served
from server.memory.domain.query_casing import casing_candidates, pick_spellings
from server.memory.domain.search_types import SearchQuery
from server.memory.store.query_spellings import spelling_counts

log = logging.getLogger(__package__)

# Deadline for the lookup, client- and server-side; past it the query embeds as typed.
_LOOKUP_BOUND_S = 0.5


def _read_bounds(input: Any, embedding_model: str) -> SearchQuery:
    """The fact leg's read filters for a pinned search input, without a vector."""
    return SearchQuery(
        org=input.org,
        namespace=input.namespace,
        scope=input.scope,
        agent=input.agent,
        **_served(input),
        embedding_model=embedding_model,
        query_vec=[],
        top_k=0,
        memory_type=input.memory_type or None,
        user_id=input.user_id,
        session_ids=list(getattr(input, "session_ids", []) or []),
        session_id=getattr(input, "session_id", None),
        run_id=input.run_id,
        tags=list(getattr(input, "tags", []) or []),
        tags_soft=bool(getattr(input, "tags_soft", False)),
        as_of=input.as_of,
        as_of_basis=getattr(input, "as_of_basis", "valid_time"),
    )


async def corpus_spellings(input: Any, embedding_model: str) -> dict[str, str]:
    """lowercase word of `input.query` -> the spelling the rows `input` may
    read use most, for the words whose spelling differs. `input` is a search
    input whose read filters are already pinned to the caller. {} when off
    (`memory_search_corpus_casing_max_words`), nothing differs or the read
    fails or runs past its deadline."""
    from server.settings import settings

    tokens = casing_candidates(
        input.query, max_words=settings.memory_search_corpus_casing_max_words
    )
    if not tokens or not input.org or not input.namespace:
        return {}
    try:
        with statement_timeout(_LOOKUP_BOUND_S):
            counts = await asyncio.wait_for(
                spelling_counts(get_client(), _read_bounds(input, embedding_model), tokens),
                timeout=_LOOKUP_BOUND_S,
            )
    except Exception as e:  # noqa: BLE001 — the spelling is a recall aid; the query embeds as typed
        log.debug("query spelling lookup failed (non-fatal): %s", e)
        return {}
    return pick_spellings(tokens, counts)
