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
# (org, namespace) -> (failures in a row, monotonic time to try again); an
# entry is dropped an hour after its back-off ends, and the oldest go first
# past the size bound.
_MAX_BACKOFF_S = 3600.0
_FAILED_SIZE = 4096
_failed: OrderedDict[tuple[str, str], tuple[int, float]] = OrderedDict()


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
    where = (q.org, q.namespace)
    if _failed.get(where, (0, 0.0))[1] > now:
        return None
    try:
        # The outer bound also covers the wait for a pool slot and a
        # reconnect; the inner one makes ClickHouse stop the statement.
        async with asyncio.timeout(_LOOKUP_BOUND_S):
            with statement_timeout(_LOOKUP_BOUND_S):
                rows = await spelling_rows(get_client(), q, tokens)
    except Exception as e:  # noqa: BLE001 — the spelling is a recall aid; the query embeds as typed
        # Read after the await: a lookup that ran beside this one may have
        # counted the same failure already.
        failures, retry_at = _failed.get(where, (0, 0.0))
        now = time.monotonic()
        if retry_at > now:
            return None
        backoff = min(_CACHE_TTL_S * 2**failures, _MAX_BACKOFF_S)
        _record_failure(where, failures + 1, now + backoff, now)
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
    _failed.pop(where, None)
    spellings = pick_spellings(tokens, rows)
    _cache[key] = (now + _CACHE_TTL_S, spellings)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return spellings


def _record_failure(where: tuple[str, str], failures: int, retry_at: float, now: float) -> None:
    _failed[where] = (failures, retry_at)
    _failed.move_to_end(where)
    for stale in [k for k, (_, at) in _failed.items() if at + _MAX_BACKOFF_S < now]:
        del _failed[stale]
    while len(_failed) > _FAILED_SIZE:
        _failed.popitem(last=False)


async def embed_query(
    embed: Callable[[str], Coroutine[Any, Any, list[float]]],
    text: str,
    input: MemorySearchInput,
    embedding_model: str,
) -> tuple[list[float], dict[str, str] | None]:
    """The vector for `text` with the corpus spellings of `input.query`'s
    words applied, and those spellings; None when `text` was embedded as
    typed because the lookup did not answer or the rewrite's embed failed.
    The lookup runs beside the embed of `text` as typed, so only a rewrite
    costs a second embed."""
    lookup = asyncio.create_task(corpus_spellings(input, embedding_model))
    tasks: list[asyncio.Task[Any]] = [lookup]
    try:
        as_typed = asyncio.create_task(embed(text))
        tasks.append(as_typed)
        spellings = await lookup
        if not spellings:
            return await as_typed, spellings
        try:
            return await embed(apply_spellings(text, spellings)), spellings
        except Exception as e:  # noqa: BLE001 — the rewrite is a recall aid; the vector as typed serves
            log.warning("embedding the respelled query failed, using it as typed: %r", e)
            return await as_typed, None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
