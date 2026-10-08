"""The retry policy every HTTP provider client (LLM and embedding) runs: which
failures a second attempt can clear, and the backoff between attempts. The
attempt count and the backoff cap are the caller's."""

from __future__ import annotations

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

# 408 request timeout, 409 conflict, 425 too early, 429 rate limited, the
# transient 5xx and 529 (Anthropic's "overloaded"). Any other 4xx (400, 401,
# 413, 422, …) is the request's problem: no retry.
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def is_retryable(exc: BaseException) -> bool:
    """A dropped connection or timeout at the transport, or a retryable status."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return False


def retrying(*, attempts: int, max_wait_s: float) -> AsyncRetrying:
    """`attempts` tries with exponential backoff (0.5 s doubling, capped at
    `max_wait_s`), on retryable failures only; the last failure is re-raised
    unchanged."""
    return AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=0.5, max=max_wait_s),
        retry=retry_if_exception(is_retryable),
        reraise=True,
    )
