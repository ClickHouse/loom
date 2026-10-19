"""Bounded retry for the fresh-part empty-block error (CH code 10) that a
leg can hit on a part that has just been written, and the per-leg tracing
span. The retry counter is process-local and lives here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from server.memory.domain.search_types import _CHClient
from server.settings import settings

log = logging.getLogger("server.memory.app.retrieval")


_empty_block_retries = 0  # process-local counter, surfaced in the warning


def _is_empty_block_column_error(exc: BaseException) -> bool:
    text = str(exc)
    return "Not found column" in text and "size = 0" in text


async def _spanned(name: str, coro: Any) -> Any:
    """Await `coro` under a child span named for the leg. A
    no-op wrapper when tracing is off — `traced` returns a null context."""
    from server.observability.otel import traced

    with traced(name):
        return await coro


async def _query_all_retrying_empty_block(
    ch: _CHClient, sql: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """The vector leg's statement under the fresh-part retry."""
    return await _call_retrying_empty_block(lambda: ch.query_all(sql, params=params), leg="vector")


async def _call_retrying_empty_block[T](
    factory: Callable[[], Awaitable[T]], *, leg: str = "side"
) -> T:
    """Run `factory()`, re-running it on the fresh-part empty-block error up
    to `memory_retrieval_empty_block_retries` times with a linear backoff; any other error,
    or the last one, propagates. The chunk, lexical and record legs read the
    same parts the vector leg does and hit the same CH code 10 on a part that
    has just been written; they run each statement under this loop inside
    their own fail-open except, so a transient code 10 is re-read instead of
    dropping the leg ("Not found column org ... size = 0")."""
    global _empty_block_retries
    attempt = 0
    while True:
        try:
            return await factory()
        except Exception as e:
            if (
                not _is_empty_block_column_error(e)
                or attempt >= settings.memory_retrieval_empty_block_retries
            ):
                raise
            attempt += 1
            _empty_block_retries += 1
            log.warning(
                "memory.search %s leg: empty-block column error on a fresh part "
                "(CH code 10), retry %d/%d (process total %d)",
                leg,
                attempt,
                settings.memory_retrieval_empty_block_retries,
                _empty_block_retries,
            )
            await asyncio.sleep(settings.memory_retrieval_empty_block_retry_sleep_s * attempt)
