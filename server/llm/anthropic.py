"""Anthropic (Claude) chat completion client for `llm_provider == "anthropic"`.

JSON mode: the Messages API has no `response_format` parameter, so the
`{"type": "json_object"}` hint is honoured with a system-prompt directive
plus code-fence stripping on the way out (assistant-prefill 400s on current
models). Weaker than a hard constraint, but the extraction caller tolerates
loose output and silently dropping the hint reintroduces parse-failure 500s.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from server.clients._http import get_model_client as _http_client
from server.clients._http_errors import raise_for_status_with_detail as _raise_for_status
from server.core.provider_keys import provider_key
from server.llm import customer, gateway, traits
from server.llm._retry import retrying
from server.llm.client import LLMResult
from server.llm.json_text import strip_code_fence

log = logging.getLogger(__name__)

_API_VERSION = "2023-06-01"
_ANTHROPIC_ROOT = "https://api.anthropic.com"

# Fields a model may refuse; server/llm/traits.py learns which, per model, from
# the 400 that names them (e.g. "`temperature` is deprecated for this model").
_OPTIONAL = ("temperature", "output_config")

_JSON_DIRECTIVE = (
    "Respond with exactly one valid JSON object and nothing else — "
    "no prose, no markdown code fences."
)


def _split_cache_writes(usage: dict[str, Any], total_write: int) -> tuple[int, int]:
    """(1-hour, 5-minute) cache-write token counts. The API reports the TTL
    split under `usage.cache_creation.{ephemeral_1h,ephemeral_5m}_input_tokens`;
    without it every write is a 5-minute write (the breakpoint loom sends)."""
    detail = usage.get("cache_creation")
    one_hour = 0
    if isinstance(detail, dict):
        raw = detail.get("ephemeral_1h_input_tokens", 0)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            one_hour = max(0, min(int(raw), total_write))
    return one_hour, max(0, total_write - one_hour)


def _thinking_starved(data: dict[str, Any]) -> bool:
    """The reply hit max_tokens with thinking blocks and no visible text."""
    blocks = data.get("content") or []
    return (
        data.get("stop_reason") == "max_tokens"
        and any(b.get("type") == "thinking" for b in blocks)
        and not any(str(b.get("text") or "").strip() for b in blocks if b.get("type") == "text")
    )


def _payload(
    model: str, system: str, user: str, max_tokens: int, temperature: float | None
) -> dict[str, Any]:
    """The request this call would like to send, minus what the model refused."""
    from server.settings import settings

    t = traits.traits(model)
    # max_tokens is required; it counts thinking, so the model's learned
    # reserve rides on top of the caller's cap.
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": traits.output_cap(model, max_tokens),
        "messages": [{"role": "user", "content": user}],
    }
    if settings.llm_anthropic_effort and "output_config" not in t.refused:
        payload["output_config"] = {"effort": settings.llm_anthropic_effort}
    if system:
        # Prompt caching: the system prompt is the static prefix of every
        # loom call (extraction instructions, synthesis rules), the user
        # turn is the variable part. Marking the system block ephemeral
        # lets Anthropic serve it from cache on the next call with the
        # same prefix; below the model's minimum cacheable length (1,024
        # tokens on Sonnet/Opus, 2,048 on Haiku) the marker is a no-op,
        # so it is always safe to send.
        payload["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    if temperature is not None and "temperature" not in t.refused:
        payload["temperature"] = temperature
    return payload


def _learn_from_refusal(model: str, r: httpx.Response, payload: dict[str, Any]) -> bool:
    """Whether a 400 named a field the model does not take (then re-sent without it)."""
    if r.status_code != 400:
        return False
    try:
        body = r.json()
    except Exception:  # noqa: BLE001 — an undecodable body is not a refusal
        return False
    if traits.learn_output_limit(model, body):
        return True
    name = traits.refused_field(body, payload, _OPTIONAL)
    if name is None:
        return False
    traits.traits(model).refused.add(name)
    return True


def _thinking_tokens(data: dict[str, Any]) -> int:
    details = (data.get("usage") or {}).get("output_tokens_details") or {}
    raw = details.get("thinking_tokens") if isinstance(details, dict) else 0
    return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0


async def _post(
    client: Any, model: str, api_key: str, payload: dict[str, Any], who: customer.Customer
) -> httpx.Response:
    from server.settings import settings

    root = settings.anthropic_base_url.rstrip("/")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _API_VERSION,
        "Content-Type": "application/json",
        **customer.headers(who),
    }
    if root != _ANTHROPIC_ROOT:
        headers["Authorization"] = f"Bearer {api_key}"  # what gateways in front of it take
    r: httpx.Response = await client.post(
        f"{root}/v1/messages",
        headers=headers,
        json=payload,
        timeout=settings.llm_request_timeout_s,
    )
    return r


class AnthropicLLMClient:
    provider = "anthropic"

    def __init__(self, model: str, *, api_key: str | None = None) -> None:
        self.model = model
        self._api_key = provider_key("anthropic") if api_key is None else api_key
        if not self._api_key and not gateway.keyless():
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set; AnthropicLLMClient requires a key. "
                "Use LOOM_LLM_PROVIDER=fake for local dev."
            )

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        temperature: float | None = 0.0,
    ) -> LLMResult:
        # Before the gateway token and outside `recording`: a call naming no
        # customer is the caller's to fix, not a provider failure.
        who = await customer.require()
        client = _http_client()
        data: dict[str, Any] = {}
        json_mode = (response_format or {}).get("type") == "json_object"
        if response_format and not json_mode:
            # No Messages API equivalent for non-json_object formats; ignore loudly.
            log.debug("anthropic client ignoring response_format=%s", response_format)
        if json_mode:
            system = f"{system}\n\n{_JSON_DIRECTIVE}" if system else _JSON_DIRECTIVE
        payload = _payload(self.model, system, user, max_tokens, temperature)
        from server.observability.genai import generation_span
        from server.observability.provider_health import recording
        from server.settings import settings

        # Span covers the WHOLE retry loop: tenacity retries up to 4
        # attempts and only the last one's usage is returned, so timing a
        # single attempt would under-report latency by up to 4x.

        with generation_span(provider=self.provider, model=self.model) as _gen:
            # Retries exhausted or non-retryable: health reports a
            # configured-but-failing provider. Cancellation is not recorded.
            with recording("anthropic:messages"):
                root = settings.anthropic_base_url.rstrip("/")
                key = await gateway.bearer(self._api_key, root)  # an outage is the provider's
                async for attempt in retrying():
                    with attempt:
                        _gen.note_attempt()
                        r = await _post(client, self.model, key, payload, who)
                        for _ in range(traits.MAX_NEGOTIATION):
                            # A refused field: re-send without it.
                            if not _learn_from_refusal(self.model, r, payload):
                                break
                            payload = _payload(self.model, system, user, max_tokens, temperature)
                            r = await _post(client, self.model, key, payload, who)
                        _raise_for_status(r, "anthropic:messages")
                        data = r.json()
                        hidden = _thinking_tokens(data)
                        traits.note_hidden(self.model, hidden)
                        if _thinking_starved(data):
                            # Thinking used the whole cap: once more with room.
                            payload["max_tokens"] = traits.starved_cap(
                                self.model, int(payload["max_tokens"]), hidden
                            )
                            r = await _post(client, self.model, key, payload, who)
                            _raise_for_status(r, "anthropic:messages")
                            data = r.json()
            text = "".join(
                str(block.get("text", ""))
                for block in (data.get("content") or [])
                if block.get("type") == "text"
            )
            if json_mode:
                text = strip_code_fence(text)
            usage = data.get("usage") or {}
            uncached_in = int(usage.get("input_tokens", 0))
            tokens_out = int(usage.get("output_tokens", 0))
            # Anthropic reports cached prefix tokens OUTSIDE input_tokens:
            # input_tokens = uncached, cache_read_input_tokens = served from
            # cache, cache_creation_input_tokens = written to cache this call
            # (split by TTL under `cache_creation` when the API sends it).
            # The token total folds all of them so `LLMResult.tokens_in`, the
            # ledger's input_tokens and the prompt_cache_hit_ratio gauge see
            # the same shape the OpenAI client reports (total prompt, cached
            # subset); the DOLLARS are computed from the split
            # (reads 0.1x, 5-minute writes 1.25x, 1-hour writes 2x).
            cache_read = max(0, int(usage.get("cache_read_input_tokens", 0) or 0))
            cache_write = max(0, int(usage.get("cache_creation_input_tokens", 0) or 0))
            cache_write_1h, cache_write_5m = _split_cache_writes(usage, cache_write)
            tokens_in = uncached_in + cache_read + cache_write
            # Attribute cost to the active op; fail-quiet so it never breaks completion.
            try:
                from server.observability.cost_accumulator import (
                    is_priced,
                    report_llm,
                )

                report_llm(
                    self.model,
                    uncached_in,
                    tokens_out,
                    cache_read=cache_read,
                    cache_write_5m=cache_write_5m,
                    cache_write_1h=cache_write_1h,
                )
                _gen.record_usage(
                    tokens_in,
                    tokens_out,
                    priced=is_priced(self.model),
                    cached_input_tokens=min(cache_read, tokens_in),
                    cache_write_5m_tokens=cache_write_5m,
                    cache_write_1h_tokens=cache_write_1h,
                )
                # Bounded prompt + completion, and the prompt-version link. Here
                # rather than at the call sites because this is the only place
                # that holds the system prompt, the user prompt AND the model's
                # reply together — and it covers every caller by construction.
                _gen.record_content(system=system, user=user, output=text or "")
            except Exception:  # noqa: BLE001 — content capture must not fail the call
                pass
        return LLMResult(
            text=text or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            provider=self.provider,
            model=self.model,
            cached_input_tokens=min(cache_read, tokens_in),
            cache_write_input_tokens=min(cache_write, tokens_in),
            stop_reason=str(data.get("stop_reason") or ""),
        )
