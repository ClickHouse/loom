"""One retry policy for every HTTP provider client (LLM and embedding); the
LLM fallback's outage class is that policy plus a caller-side timeout."""

from __future__ import annotations

import httpx
import pytest

from server.clients import _retry
from server.llm.fallback import is_provider_outage


def _status(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.example/v1")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


@pytest.mark.parametrize(
    ("code", "retried"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (409, True),
        (413, False),
        (422, False),
        (425, True),
        (429, True),
        (500, True),
        (501, False),
        (502, True),
        (503, True),
        (504, True),
        (529, True),
    ],
)
def test_one_status_table_for_llm_and_embedding(code: int, retried: bool) -> None:
    assert _retry.is_retryable(_status(code)) is retried
    # What the LLM retries gave up on is exactly what the fallback hands over on.
    assert is_provider_outage(_status(code)) is retried


def test_a_timeout_hands_over_without_being_a_retry_trigger() -> None:
    assert is_provider_outage(TimeoutError()) and not _retry.is_retryable(TimeoutError())
    assert _retry.is_retryable(httpx.ReadTimeout("t"))
    assert _retry.is_retryable(httpx.ConnectError("down"))
    assert not _retry.is_retryable(ValueError("x"))


def test_the_providers_run_the_shared_policy() -> None:
    from server.embedding import _provider
    from server.llm import _retry as llm_retry
    from server.llm import anthropic, openai

    assert anthropic.retrying is llm_retry.retrying and openai.retrying is llm_retry.retrying
    assert llm_retry.retrying().retry.predicate is _retry.is_retryable  # type: ignore[attr-defined]
    assert _provider.retrying().retry.predicate is _retry.is_retryable  # type: ignore[attr-defined]
    assert llm_retry.retrying().stop.max_attempt_number == 4  # type: ignore[attr-defined]
    assert _provider.retrying().stop.max_attempt_number == 3  # type: ignore[attr-defined]
