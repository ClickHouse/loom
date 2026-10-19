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
from server.core.errors import EmbedTimeoutError
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.filters import read_filters
from server.memory.domain.query_spelling import apply_spellings, pick_spellings, spelling_candidates
from server.memory.domain.search_types import SearchQuery
from server.memory.store.query_spellings import spelling_rows
from server.settings import settings

log = logging.getLogger(__package__)

# After a respelled embed times out, how long the text as typed may take:
# long enough for an embedding-cache hit, not for a provider call.
_CACHE_HIT_GRACE_S = 0.25
# Short queries repeat (an agent re-asks, a reader retypes) and a corpus's
# spelling changes slowly, so an answer is reused for a minute.
_CACHE_TTL_S = 60.0
_CACHE_SIZE = 4096
_cache: OrderedDict[tuple[str, tuple[str, ...]], tuple[float, dict[str, str]]] = OrderedDict()
# A lookup that fails or runs past the deadline skips only its own query.
# Failures that overlap (one recorded while another lookup ran) count once.
# A namespace whose lookups fail _BACKOFF_AFTER times in a row is not read
# again for a while: a minute, doubling with each further failure up to an
# hour; a success ends the streak. (org, namespace) -> (failures in a row,
# retry_at): until the back-off starts, retry_at is the last failure's time,
# which is what the overlap check compares; after, the time to try again.
# An entry is dropped an hour after its back-off ends, and the oldest go
# first past the size bound.
_BACKOFF_AFTER = 3
_MAX_BACKOFF_S = 3600.0
_MAX_DOUBLINGS = 6  # 60 s doubled six times passes the hour
_FAILED_SIZE = 4096
_failed: OrderedDict[tuple[str, str], tuple[int, float]] = OrderedDict()


def _lookup_key(
    input: MemorySearchInput, embedding_model: str
) -> tuple[SearchQuery, list[str], tuple[str, tuple[str, ...]]] | None:
    """The read bounds, the candidate words and the cache key of a lookup;
    None when the query has no word to respell (or the feature is off)."""
    from server.settings import settings

    tokens = spelling_candidates(
        input.query, max_words=settings.memory_search_query_spelling_max_words
    )
    if not tokens:
        return None
    q = SearchQuery(
        org=input.org,
        namespace=input.namespace,
        **read_filters(input),
        embedding_model=embedding_model,
        query_vec=[],
        top_k=0,
    )
    return q, tokens, (repr(q), tuple(tokens))


def _cached(key: tuple[str, tuple[str, ...]]) -> dict[str, str] | None:
    hit = _cache.get(key)
    if hit is None or hit[0] <= time.monotonic():
        return None
    _cache.move_to_end(key)
    return hit[1]


async def _lookup(
    q: SearchQuery, tokens: list[str], key: tuple[str, tuple[str, ...]]
) -> dict[str, str] | None:
    """lowercase word -> the identifier spelling the rows `q` may read use
    instead ({} when none does), or None when the lookup did not answer (it
    failed or ran past its deadline, now or recently enough that the
    namespace is backing off). The answer is cached under `key`."""
    started = time.monotonic()
    where = (q.org, q.namespace)
    if _failed.get(where, (0, 0.0))[1] > started:
        return None
    try:
        # The outer bound also covers the wait for a pool slot and a
        # reconnect; the inner one makes ClickHouse stop the statement.
        async with asyncio.timeout(settings.memory_search_spelling_lookup_timeout_s):
            with statement_timeout(settings.memory_search_spelling_lookup_timeout_s):
                rows = await spelling_rows(get_client(), q, tokens)
    except Exception as e:  # noqa: BLE001 — the spelling is a recall aid; the query embeds as typed
        # Read after the await. A failure recorded since this lookup started
        # (or a back-off begun) is the same slow moment, so it counts once.
        failures, retry_at = _failed.get(where, (0, 0.0))
        if retry_at > started:
            return None
        now = time.monotonic()
        if retry_at + _MAX_BACKOFF_S < now:
            failures = 0  # the last streak ended over an hour ago: this one is new
        failures += 1
        backoff = 0.0
        if failures >= _BACKOFF_AFTER:
            doublings = min(failures - _BACKOFF_AFTER, _MAX_DOUBLINGS)
            backoff = min(_CACHE_TTL_S * 2**doublings, _MAX_BACKOFF_S)
        _record_failure(where, failures, now + backoff, now)
        # One warning per streak, when its back-off starts; the rest log at debug.
        level = logging.WARNING if failures == _BACKOFF_AFTER else logging.DEBUG
        log.log(
            level,
            "query spelling lookup in %s failed (%d in a row), retrying in %.0f s: %r",
            q.namespace,
            failures,
            backoff,
            e,
        )
        return None
    _failed.pop(where, None)
    spellings = pick_spellings(tokens, rows)
    _cache[key] = (started + _CACHE_TTL_S, spellings)
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
    typed because the lookup did not answer or the respelled embed failed.
    A lookup runs beside the embed of `text` as typed; a cached answer
    embeds only the text it names, and as typed only if that fails (after a
    timeout, only as long as an embedding-cache hit takes). The search fails
    only when every embed it tried failed."""
    found = _lookup_key(input, embedding_model)
    if found is None:
        return await embed(text), {}
    q, tokens, key = found
    as_typed: asyncio.Task[list[float]] | None = None
    try:
        spellings = _cached(key)
        if spellings is None:
            as_typed = asyncio.create_task(embed(text))  # overlaps the lookup
            spellings = await _lookup(q, tokens, key)
        if not spellings:
            return await (as_typed or embed(text)), spellings
        try:
            return await embed(apply_spellings(text, spellings)), spellings
        except Exception as e:  # noqa: BLE001 — the rewrite is a recall aid; the vector as typed serves
            failure = e
        if as_typed is None and isinstance(failure, EmbedTimeoutError):
            # The provider hangs: only an embedding-cache hit can still answer
            # as typed, and one answers within _CACHE_HIT_GRACE_S.
            try:
                async with asyncio.timeout(_CACHE_HIT_GRACE_S):
                    return await embed(text), None
            except TimeoutError:
                raise failure from None
        vec = await (as_typed or embed(text))
        # Per request it shows as query_spellings_skipped; an outage would log it on every search.
        log.debug("embedding the respelled query failed, using it as typed: %r", failure)
        return vec, None
    finally:
        if as_typed is not None:
            as_typed.cancel()
            await asyncio.gather(as_typed, return_exceptions=True)
