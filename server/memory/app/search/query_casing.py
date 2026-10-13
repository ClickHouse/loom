"""The query text a search embeds: its lowercase words in the spelling the
rows it may read use most. An embedder reads `datalakecatalog` and
`DataLakeCatalog` as different strings, so the vector leg otherwise misses
rows that spell an identifier in CamelCase."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from server.core.ch import get_client, statement_timeout
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.filters import read_filters
from server.memory.domain.query_casing import apply_spellings, casing_candidates, pick_spellings
from server.memory.domain.search_types import SearchQuery
from server.memory.store.query_spellings import spelling_rows
from server.observability import degradations

# The lookup's deadline, enforced by ClickHouse. It reads every row of the
# namespace (40-180 ms on the largest) and runs beside the embed (~120 ms).
_LOOKUP_BOUND_S = 0.25


async def corpus_spellings(input: MemorySearchInput, embedding_model: str) -> dict[str, str]:
    """lowercase word of `input.query` -> the spelling the rows `input` may
    read in its namespace use instead; {} when off
    (`memory_search_corpus_casing_max_words`), when no word qualifies, or when
    the read fails (noted as a degradation)."""
    from server.settings import settings

    tokens = casing_candidates(
        input.query, max_words=settings.memory_search_corpus_casing_max_words
    )
    if not tokens:
        return {}
    q = SearchQuery(
        org=input.org,
        namespace=input.namespace,
        **read_filters(input),
        embedding_model=embedding_model,
        query_vec=[],
        top_k=0,
    )
    try:
        with statement_timeout(_LOOKUP_BOUND_S):
            rows = await spelling_rows(get_client(), q, tokens)
    except Exception as e:  # noqa: BLE001 — the spelling is a recall aid; the query embeds as typed
        degradations.note("query_spelling", "embedded_as_typed", e)
        return {}
    return pick_spellings(tokens, rows)


async def embed_query(
    embed: Callable[[str], Awaitable[list[float]]],
    text: str,
    input: MemorySearchInput,
    embedding_model: str,
) -> tuple[list[float], dict[str, str]]:
    """The vector for `text` with the corpus spellings of `input.query`'s
    words applied, and those spellings. The lookup runs beside the embed of
    `text` as typed, so only a rewrite costs a second embed."""
    lookup = asyncio.ensure_future(corpus_spellings(input, embedding_model))
    as_typed = asyncio.ensure_future(embed(text))
    try:
        spellings = await lookup
        if not spellings:
            return await as_typed, {}
        return await embed(apply_spellings(text, spellings)), spellings
    finally:
        lookup.cancel()
        as_typed.cancel()
        await asyncio.gather(lookup, as_typed, return_exceptions=True)
