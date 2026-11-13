"""OpenAI chat completion client.

Single-shot chat completion over the process-wide httpx client, with bounded
retry on transient transport / rate-limit / 5xx responses so a burst of
concurrent completions doesn't surface OpenAI 429s to the caller as 500s.
"""

from __future__ import annotations

from typing import Any

import httpx

from server.clients._http import get_model_client as _http_client
from server.clients._http_errors import raise_for_status_with_detail as _raise_for_status
from server.core.provider_keys import provider_key
from server.llm import customer, gateway, traits
from server.llm._retry import retrying
from server.llm.client import LLMResult

# Fields a model may refuse; server/llm/traits.py learns which, per model, from
# the 400s that name them. Loom's calls are structured extraction, judging and
# grounded synthesis, so every call asks for the lowest reasoning effort ('none'
# until the model names a higher floor); `llm_openai_reasoning_effort` pins one.
_OPTIONAL = ("temperature", "reasoning_effort", "max_completion_tokens", "max_tokens")


def _reasoning_effort(model: str, wanted: str = "") -> str:
    """`wanted`, a client's own effort, replaces the pin and the 'none' default, but
    never goes below the lowest effort the model said it takes."""
    from server.settings import settings

    floor = traits.traits(model).effort or traits.EFFORT_ORDER[0]
    if wanted in traits.EFFORT_ORDER and floor in traits.EFFORT_ORDER:
        return max(wanted, floor, key=traits.EFFORT_ORDER.index)
    pinned = settings.llm_openai_reasoning_effort.strip()
    return pinned or floor


def _payload(
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    response_format: dict[str, str] | None,
    temperature: float | None,
    effort: str,
    cap_name: str,
) -> dict[str, Any]:
    """The request this call would like to send, minus what the model refused.
    `cap_name` is the output cap's field the server reads first."""
    t = traits.traits(model)
    # Prompt caching here is automatic and prefix-based (≥ 1,024 tokens,
    # no breakpoint to place): the cached run is `system` followed by
    # however much of `user` repeats.
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            *([{"role": "system", "content": system}] if system else []),
            {"role": "user", "content": user},
        ],
    }
    # The output cap counts hidden reasoning, so the model's learned reserve
    # rides on top of the caller's cap. Endpoints that still call it
    # `max_tokens` refuse the newer name once and get the older one after.
    cap_field = "max_tokens" if "max_completion_tokens" in t.refused else cap_name
    payload[cap_field] = traits.output_cap(model, max_tokens)
    if "reasoning_effort" not in t.refused:
        payload["reasoning_effort"] = _reasoning_effort(model, effort)
    if temperature is not None and "temperature" not in t.refused:
        payload["temperature"] = temperature
    if response_format is not None:
        # {"type": "json_object"} requires the word "json" in the prompt.
        payload["response_format"] = response_format
    return payload


def _learn_from_refusal(model: str, r: httpx.Response, payload: dict[str, Any]) -> bool:
    """Whether a 400 taught something about the model (the call is then re-sent)."""
    if r.status_code != 400:
        return False
    try:
        body = r.json()
    except Exception:  # noqa: BLE001 — an undecodable body is not a refusal
        return False
    if traits.learn_output_limit(model, body):
        return True
    if traits.starved_error(body):
        # Reasoning spent at least the whole cap: the reserve grows to twice it,
        # so each starved try doubles the room up to the ceiling.
        cap = int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 0)
        traits.note_hidden(model, max(cap, traits.STARVED_FLOOR))
        return True
    t = traits.traits(model)
    if "reasoning_effort" in payload:
        lower = traits.lower_effort(body, str(payload["reasoning_effort"]))
        if lower is not None:
            t.effort = lower
            return True
    name = traits.refused_field(body, payload, _OPTIONAL)
    if name is None or name == "max_tokens":
        return False
    t.refused.add(name)
    return True


def _reasoning_tokens(data: dict[str, Any]) -> int:
    details = (data.get("usage") or {}).get("completion_tokens_details") or {}
    raw = details.get("reasoning_tokens") if isinstance(details, dict) else 0
    return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0


def _starved(data: dict[str, Any]) -> bool:
    """The reply hit the cap with no visible text."""
    choice = (data.get("choices") or [{}])[0]
    text = str((choice.get("message") or {}).get("content") or "")
    return choice.get("finish_reason") == "length" and not text.strip()


async def _post(
    client: Any, url: str, headers: dict[str, str], payload: dict[str, Any]
) -> httpx.Response:
    from server.settings import settings

    r: httpx.Response = await client.post(
        url, headers=headers, json=payload, timeout=settings.llm_request_timeout_s
    )
    return r


class OpenAILLMClient:
    provider = "openai"
    # What /v1/health and an error's detail call this client's calls.
    _source = "openai:chat"
    # The output cap's field. OpenAI's reasoning models take only the newer name.
    _cap_name = "max_completion_tokens"
    # This client's own reasoning effort ('' = the pin, else the lowest the model takes).
    reasoning_effort = ""

    def __init__(
        self, model: str, *, api_key: str | None = None, reasoning_effort: str = ""
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._api_key = provider_key("openai") if api_key is None else api_key
        if not self._api_key and not gateway.keyless():
            raise RuntimeError(
                "OPENAI_API_KEY not set; OpenAILLMClient requires a key. "
                "Use LOOM_LLM_PROVIDER=fake for local dev."
            )

    def _root(self) -> str:
        from server.settings import settings

        return settings.openai_base_url

    async def _headers(self, url: str) -> dict[str, str]:
        bearer = await gateway.bearer(self._api_key, url)  # an outage is the provider's
        return {"Authorization": f"Bearer {bearer}"}

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
        effort = self.reasoning_effort
        payload = _payload(
            self.model,
            system,
            user,
            max_tokens,
            response_format,
            temperature,
            effort,
            self._cap_name,
        )
        # Span covers the WHOLE retry loop: tenacity retries up to 4
        # attempts and only the last one's usage is returned, so timing a
        # single attempt would under-report latency by up to 4x.
        from server.observability.genai import generation_span
        from server.observability.provider_health import recording

        with generation_span(provider=self.provider, model=self.model) as _gen:
            # A failure after the retries (or a non-retryable one, or an
            # unreadable answer) marks the provider failing for /v1/health.
            with recording(self._source):
                url = f"{self._root().rstrip('/')}/chat/completions"
                headers = {
                    **await self._headers(url),
                    "Content-Type": "application/json",
                    **customer.headers(who),
                }
                async for attempt in retrying():
                    with attempt:
                        _gen.note_attempt()
                        r = await _post(client, url, headers, payload)
                        for _ in range(traits.MAX_NEGOTIATION):
                            # A refused field or effort: re-send without it.
                            if not _learn_from_refusal(self.model, r, payload):
                                break
                            payload = _payload(
                                self.model,
                                system,
                                user,
                                max_tokens,
                                response_format,
                                temperature,
                                effort,
                                self._cap_name,
                            )
                            r = await _post(client, url, headers, payload)
                        _raise_for_status(r, self._source)
                        data = r.json()
                        hidden = _reasoning_tokens(data)
                        traits.note_hidden(self.model, hidden)
                        if _starved(data):
                            # Reasoning spent the whole cap: once more with room.
                            cap_field = next(
                                k for k in ("max_completion_tokens", "max_tokens") if k in payload
                            )
                            payload[cap_field] = traits.starved_cap(
                                self.model, int(payload[cap_field]), hidden
                            )
                            r = await _post(client, url, headers, payload)
                            _raise_for_status(r, self._source)
                            data = r.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                usage = data.get("usage") or {}
                tokens_in = int(usage.get("prompt_tokens", 0))
                tokens_out = int(usage.get("completion_tokens", 0))
            cached_in = 0
            # Fail-quiet so cost attribution never breaks the completion path.
            try:
                from server.observability.cost_accumulator import (
                    is_priced,
                    report_llm,
                )

                # Provider-side prompt cache: OpenAI caches static prefixes over
                # 1024 tokens and bills them at a discount. Extraction ships a
                # ~15k-token static system prompt on every call, so the hit rate
                # is the biggest lever on its latency and spend; record it.
                details = usage.get("prompt_tokens_details")
                if isinstance(details, dict):
                    raw = details.get("cached_tokens")
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                        cached_in = max(0, min(int(raw), tokens_in))
                # `prompt_tokens` is the whole prompt; the cached
                # share bills at 0.5x, so the ledger takes the split.
                report_llm(self.model, tokens_in - cached_in, tokens_out, cache_read=cached_in)
                _gen.record_usage(
                    tokens_in,
                    tokens_out,
                    priced=is_priced(self.model),
                    cached_input_tokens=cached_in,
                )
                # Bounded prompt + completion, and the prompt-version link. Here
                # rather than at the call sites because this is the only place
                # that holds the system prompt, the user prompt AND the model's
                # reply together — and it covers every caller by construction.
                _gen.record_content(system=system, user=user, output=text or "")
            except Exception:  # noqa: BLE001 — content capture must not fail call
                pass
        return LLMResult(
            text=text or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            provider=self.provider,
            model=self.model,
            cached_input_tokens=cached_in,
            stop_reason=str(((data.get("choices") or [{}])[0] or {}).get("finish_reason") or ""),
        )
