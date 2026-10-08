"""Runtime model fallback: when the configured model's provider is down,
answer with another model instead of failing the operation.

Every provider client already retries 429 / 5xx / transport errors four
times with backoff. What happens after the retries is the gap: the
exception propagates, the op fails, and — for memory.ask — the agent gets
nothing although loom had the grounding memories in hand and a second
provider one call away. A fallback is a reliability concern distinct from
routing.

`FallbackLLMClient` wraps a primary client and a secondary one. A call goes
to the primary; if it raises a PROVIDER-SIDE failure (rate limit, 5xx,
transport or timeout — the same class the providers retry) the secondary
answers. So does a request the primary's provider rejected outright (400,
413, 422): the secondary parses a different payload against different
limits. Authentication and unknown-model errors (401, 403, 404) are
configuration and propagate unchanged.

The result carries the model that actually answered (`LLMResult.model`),
and each provider client reports its own tokens to the cost accumulator,
so spend is attributed to the model that earned it. The fallback itself is
counted in `loom.llm.fallback` and logged once per call, so an operator
sees "opus was down for 40 minutes, 312 asks answered by gpt-4o-mini"
rather than an unexplained cost dip.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from server.llm._retry import is_provider_outage
from server.llm.client import LLMClient, LLMResult

log = logging.getLogger(__name__)


# A provider that REJECTED the request (400 bad request, 413
# too large, 422 unprocessable) is not an outage, but the request will not
# succeed there on any retry and the secondary provider parses a different
# payload against different limits and a different prompt filter — so the
# answer is handed over, with the reason on the fallback metric and the
# warning line. Authentication (401/403) and an unknown model (404) stay hard
# failures: those are configuration, and a silent hand-over would hide them.
_REQUEST_REJECTED_STATUS = frozenset({400, 413, 422})


def hands_over(exc: BaseException) -> bool:
    """True when the secondary should answer: an outage, or a request the
    primary's provider rejected outright."""
    if is_provider_outage(exc):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _REQUEST_REJECTED_STATUS
    return False


class FallbackLLMClient:
    """An LLMClient that answers from `secondary` when `primary`'s provider
    is unavailable. `provider` / `model` name the primary, so callers that
    label by intended model keep working; the served model rides the
    result."""

    def __init__(self, primary: LLMClient, secondary: LLMClient) -> None:
        self.primary = primary
        self.secondary = secondary
        self.provider = primary.provider
        self.model = primary.model
        self.fallbacks = 0  # process-lifetime count, for tests and doctor

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        **kw: Any,
    ) -> LLMResult:
        try:
            return await self.primary.complete(
                system, user, max_tokens=max_tokens, response_format=response_format, **kw
            )
        except BaseException as exc:
            if not hands_over(exc):
                raise
            self.fallbacks += 1
            reason = _reason(exc)
            log.warning(
                "llm fallback: %s/%s unavailable (%s); answering with %s/%s",
                self.primary.provider,
                self.primary.model,
                reason,
                self.secondary.provider,
                self.secondary.model,
            )
            try:
                from server.observability import metrics as _metrics

                _metrics.record_llm_fallback(
                    from_model=self.primary.model,
                    to_model=self.secondary.model,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001 — telemetry must not block the answer
                pass
        return await self.secondary.complete(
            system, user, max_tokens=max_tokens, response_format=response_format, **kw
        )


def _reason(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        from server.clients._http_errors import provider_error_code

        code = provider_error_code(exc)
        status = exc.response.status_code
        return f"http_{status}_{code}" if code else f"http_{status}"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "transport"
    return type(exc).__name__.lower()
