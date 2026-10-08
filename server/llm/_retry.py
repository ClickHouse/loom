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
    """Four attempts, backoff capped at 20 s."""
    return _retry.retrying(attempts=4, max_wait_s=20)
