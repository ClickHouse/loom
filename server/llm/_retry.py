"""The retry policy every LLM provider client runs, and the outage class the
fallback hands over on: the same statuses, so what a client gave up on is
exactly what the fallback treats as the provider being down."""

from __future__ import annotations

import asyncio

from tenacity import AsyncRetrying

from server.clients import _retry


def is_provider_outage(exc: BaseException) -> bool:
    """What the retries could not clear, plus a caller-side timeout: the
    failure class where another model is worth trying."""
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or _retry.is_retryable(exc)


def retrying() -> AsyncRetrying:
    """`llm_retry_attempts` attempts, backoff capped at `llm_retry_max_wait_s`."""
    from server.settings import settings

    return _retry.retrying(
        attempts=settings.llm_retry_attempts, max_wait_s=settings.llm_retry_max_wait_s
    )
