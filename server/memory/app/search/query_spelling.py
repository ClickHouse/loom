"""The query text a search embeds: its lowercase words in the spelling the
rows it may read use most. An embedder reads `datalakecatalog` and
`DataLakeCatalog` as different strings, so the vector leg otherwise misses
rows that spell an identifier in CamelCase."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any

from server.core.ch import get_client, statement_timeout
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.filters import read_filters
from server.memory.domain.query_spelling import apply_spellings, pick_spellings, spelling_candidates
from server.memory.domain.search_types import SearchQuery
from server.memory.store.query_spellings import spelling_rows

log = logging.getLogger(__package__)

# The lookup reads every row of the namespace: 40-180 ms on the largest
# today (88k rows), so this deadline leaves ~75 ms of headroom there.
_LOOKUP_BOUND_S = 0.25
# Short queries repeat (an agent re-asks, a reader retypes) and a corpus's
# spelling changes slowly, so an answer is reused for a minute.
_CACHE_TTL_S = 60.0
_CACHE_SIZE = 4096
_cache: OrderedDict[tuple[str, tuple[str, ...]], tuple[float, dict[str, str]]] = OrderedDict()
# A namespace whose lookup failed or ran past the deadline is not read again
# for a while: a minute, doubling with each failure in a row up to an hour.
# (org, namespace) -> (failures in a row, monotonic time to try again).
_MAX_BACKOFF_S = 3600.0
_failed: dict[tuple[str, str], tuple[int, float]] = {}


async def corpus_spellings(input: MemorySearchInput, embedding_model: str) -> dict[str, str] | None:
    """lowercase word of `input.query` -> the spelling the rows `input` may
    read in the addressed namespace use instead. {} when off
    (`memory_search_query_spelling_max_words`) or nothing differs; None when
    the lookup did not answer (it failed or ran past its deadline, now or
    recently enough that the namespace is backing off)."""
    from server.settings import settings

    tokens = spelling_candidates(
        input.query, max_words=settings.memory_search_query_spelling_max_words
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
    now = time.monotonic()
    key = (repr(q), tuple(tokens))
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    failures, retry_at = _failed.get((q.org, q.namespace), (0, 0.0))
    if retry_at > now:
        return None
    try:
        # The outer bound also covers the wait for a pool slot and a
        # reconnect; the inner one makes ClickHouse stop the statement.
        async with asyncio.timeout(_LOOKUP_BOUND_S):
            with statement_timeout(_LOOKUP_BOUND_S):
                rows = await spelling_rows(get_client(), q, tokens)
    except Exception as e:  # noqa: BLE001 — the spelling is a recall aid; the query embeds as typed
        backoff = min(_CACHE_TTL_S * 2**failures, _MAX_BACKOFF_S)
        _failed[(q.org, q.namespace)] = (failures + 1, now + backoff)
        # One warning per streak; the retries after it log at debug.
        level = logging.WARNING if failures == 0 else logging.DEBUG
        log.log(
            level,
            "query spelling lookup in %s failed, retrying in %.0f s: %r",
            q.namespace,
            backoff,
            e,
        )
        return None
    _failed.pop((q.org, q.namespace), None)
    spellings = pick_spellings(tokens, rows)
    _cache[key] = (now + _CACHE_TTL_S, spellings)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return spellings


async def embed_query(
    embed: Callable[[str], Coroutine[Any, Any, list[float]]],
    text: str,
    input: MemorySearchInput,
    embedding_model: str,
) -> tuple[list[float], dict[str, str] | None]:
    """The vector for `text` with the corpus spellings of `input.query`'s
    words applied, and those spellings (None when the lookup did not
    answer). The lookup runs beside the embed of `text` as typed, so only a
    rewrite costs a second embed."""
    lookup = asyncio.create_task(corpus_spellings(input, embedding_model))
    as_typed = asyncio.create_task(embed(text))
    try:
        spellings = await lookup
        if not spellings:
            return await as_typed, spellings
        return await embed(apply_spellings(text, spellings)), spellings
    finally:
        lookup.cancel()
        as_typed.cancel()
        await asyncio.gather(lookup, as_typed, return_exceptions=True)
