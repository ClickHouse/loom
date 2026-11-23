"""OpenAI's Responses API client, and the call it shares with `LocalLLMClient`.

`OpenAILLMClient` sends one `POST <openai_base_url>/responses` per completion;
`LocalLLMClient` (server/llm/local.py) is the same client speaking chat
completions (server/llm/chat_completions.py) to a server the operator runs. Each
API owns its request and its reply (a `Wire`). The call around them is shared:
bounded retries on transient transport, rate-limit and 5xx answers over the
process-wide model client, the bearer and the customer headers, the re-sends that
server/llm/traits.py learns from, the generation span, provider health, the cost
report and the content capture.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from server.clients._http import get_model_client as _http_client
from server.clients._http_errors import raise_for_status_with_detail as _raise_for_status
from server.core.provider_keys import provider_key
from server.llm import customer, gateway, traits
from server.llm._retry import retrying
from server.llm.client import LLMResult


@dataclass(frozen=True)
class Call:
    """What one completion asks for, before the model's traits shape the request."""

    system: str
    user: str
    max_tokens: int
    response_format: Mapping[str, Any] | None
    temperature: float | None
    # The client's own reasoning effort ('' = the pin, else the lowest the model takes).
    effort: str


@dataclass(frozen=True)
class Reply:
    """One answer, read the same way from either API."""

    text: str
    # The whole prompt, its cached share included.
    tokens_in: int
    tokens_out: int
    # The prompt's share served from the provider's prefix cache, billed at a
    # discount. Extraction sends a long static system prompt on every call, so
    # the hit rate drives its latency and spend.
    cached_in: int
    # Reasoning tokens, counted in tokens_out and in the output cap.
    hidden: int
    # In chat completions' words: "stop", "length", "content_filter", "refusal".
    stop_reason: str

    @property
    def starved(self) -> bool:
        """The reply hit the cap with no visible text."""
        return self.stop_reason == "length" and not self.text.strip()


class Wire(Protocol):
    """One API's request and reply."""

    @property
    def path(self) -> str:
        """Appended to the API root."""
        ...

    @property
    def optional(self) -> tuple[str, ...]:
        """Top-level fields a model may refuse; traits.refused_field drops them."""
        ...

    def payload(self, model: str, call: Call) -> dict[str, Any]: ...

    def cap_field(self, payload: Mapping[str, Any]) -> str | None:
        """The field of `payload` that carries the output cap, if any."""
        ...

    def effort(self, payload: Mapping[str, Any]) -> str:
        """The reasoning effort `payload` asks for ('' for none)."""
        ...

    def reply(self, data: Any) -> Reply: ...


def count(data: Any, *path: str) -> int:
    """The token count at `path` in an answer's JSON; 0 when absent or not a number."""
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    if isinstance(data, bool) or not isinstance(data, (int, float)):
        return 0
    return max(0, int(data))


def effort_for(model: str, wanted: str = "") -> str:
    """The reasoning effort to ask `model` for. Loom's calls are structured
    extraction, judging and grounded synthesis, so the default is the lowest
    the model takes ('none' until it names a higher floor), and
    `llm_openai_reasoning_effort` pins one. `wanted`, a client's own effort,
    replaces both. Neither goes below the model's floor."""
    from server.settings import settings

    floor = traits.traits(model).effort or traits.EFFORT_ORDER[0]
    wanted = wanted or settings.llm_openai_reasoning_effort.strip()
    if wanted in traits.EFFORT_ORDER and floor in traits.EFFORT_ORDER:
        return max(wanted, floor, key=traits.EFFORT_ORDER.index)
    return wanted or floor


def learn_from_refusal(wire: Wire, model: str, r: httpx.Response, payload: dict[str, Any]) -> bool:
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
        cap_field = wire.cap_field(payload)
        cap = int(payload[cap_field]) if cap_field else 0
        traits.note_hidden(model, max(cap, traits.STARVED_FLOOR))
        return True
    t = traits.traits(model)
    sent = wire.effort(payload)
    if sent:
        lower = traits.lower_effort(body, sent)
        if lower is not None:
            t.effort = lower
            # A refused effort above the new floor (a pin, a client's own) would go
            # out again unchanged: the field is dropped below instead.
            if effort_for(model, sent) != sent:
                return True
    name = traits.refused_field(body, payload, wire.optional)
    if name is None:
        return False
    t.refused.add(name)
    return True


# The Responses API refuses a smaller `max_output_tokens`.
_LEAST_OUTPUT_TOKENS = 16


@dataclass(frozen=True)
class Responses:
    """The Responses API wire. Every request sets `store: false`, so the provider
    keeps no response to retrieve later, and none carries `previous_response_id`,
    `tools` or `prompt`."""

    path: str = "/responses"
    # Fields a model may refuse (server/llm/traits.py learns which from its 400s).
    # The cap is not one: a call is never sent uncapped.
    optional: tuple[str, ...] = ("temperature", "reasoning")

    def payload(self, model: str, call: Call) -> dict[str, Any]:
        """The request this call would like to send, minus what the model refused."""
        t = traits.traits(model)
        # The output cap counts hidden reasoning, so the model's learned reserve
        # rides on top of the caller's cap.
        cap = max(traits.output_cap(model, call.max_tokens), _LEAST_OUTPUT_TOKENS)
        # The system prompt is the first input message, not `instructions`: a
        # `json_object` format needs the word "json" in an input message, and
        # Loom's prompts say it in the system prompt. Prompt caching here is
        # automatic and prefix-based (≥ 1,024 tokens, no breakpoint to place):
        # the cached run is the system message followed by however much of the
        # user message repeats.
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                *([{"role": "developer", "content": call.system}] if call.system else []),
                {"role": "user", "content": call.user},
            ],
            "store": False,
            "max_output_tokens": cap,
        }
        if "reasoning" not in t.refused:
            payload["reasoning"] = {"effort": effort_for(model, call.effort)}
        if call.temperature is not None and "temperature" not in t.refused:
            payload["temperature"] = call.temperature
        if call.response_format is not None:
            payload["text"] = {"format": _text_format(call.response_format)}
        return payload

    def cap_field(self, payload: Mapping[str, Any]) -> str | None:
        return "max_output_tokens"

    def effort(self, payload: Mapping[str, Any]) -> str:
        reasoning = payload.get("reasoning")
        return str(reasoning.get("effort") or "") if isinstance(reasoning, dict) else ""

    def reply(self, data: Any) -> Reply:
        """The text of the `output_text` parts of the message items (reasoning
        items carry none), and the usage."""
        parts = [
            part
            for item in data.get("output") or []
            if isinstance(item, dict) and item.get("type") == "message"
            for part in item.get("content") or []
            if isinstance(part, dict)
        ]
        tokens_in = count(data, "usage", "input_tokens")
        return Reply(
            text="".join(str(p.get("text") or "") for p in parts if p.get("type") == "output_text"),
            tokens_in=tokens_in,
            tokens_out=count(data, "usage", "output_tokens"),
            cached_in=min(count(data, "usage", "input_tokens_details", "cached_tokens"), tokens_in),
            hidden=count(data, "usage", "output_tokens_details", "reasoning_tokens"),
            stop_reason=_stop_reason(data, parts),
        )


# Why a Responses reply is incomplete, in chat completions' words (others as given).
_INCOMPLETE = {"max_output_tokens": "length"}


def _stop_reason(data: Mapping[str, Any], parts: list[dict[str, Any]]) -> str:
    """The reply's status in the words callers read (`LLMResult.stop_reason`)."""
    if any(p.get("type") == "refusal" for p in parts):
        return "refusal"
    status = str(data.get("status") or "")
    if status == "completed":
        return "stop"
    details = data.get("incomplete_details")
    reason = str(details.get("reason") or "") if isinstance(details, dict) else ""
    return _INCOMPLETE.get(reason, reason or status)


def _text_format(response_format: Mapping[str, Any]) -> dict[str, Any]:
    """A chat-completions `response_format` as the Responses `text.format`: a JSON
    schema's `name`, `schema` and `strict` sit beside its `type`."""
    if response_format.get("type") == "json_schema":
        return {"type": "json_schema", **(response_format.get("json_schema") or {})}
    return dict(response_format)


# The HTTP status that a failed Responses reply's `error.code` stands for; any
# other code is the request's problem, as a 400.
_FAILED_STATUS = {"server_error": 500, "rate_limit_exceeded": 429}


def _raise_for_failed(data: Any, url: str, source: str) -> None:
    """A 200 whose body says the response failed (`status: "failed"`) raises the
    HTTP error that its `error.code` stands for, so the retries, the fallback and
    provider health treat it as that error."""
    if not isinstance(data, dict) or data.get("status") != "failed":
        return
    error = data.get("error")
    if not isinstance(error, dict):
        error = {}
    status = _FAILED_STATUS.get(str(error.get("code") or ""), 400)
    request = httpx.Request("POST", url)
    _raise_for_status(httpx.Response(status, json={"error": error}, request=request), source)


async def _post(
    client: Any, url: str, headers: dict[str, str], payload: dict[str, Any]
) -> httpx.Response:
    from server.settings import settings

    r: httpx.Response = await client.post(
        url, headers=headers, json=payload, timeout=settings.llm_request_timeout_s
    )
    return r


def _report(gen: Any, model: str, call: Call, reply: Reply) -> None:
    """The call's cost to the ledger, and its usage and bounded content to its
    span. Fail-quiet, so attribution never breaks the completion."""
    try:
        from server.observability.cost_accumulator import is_priced, report_llm

        # The prompt count includes the cached share, which bills at a discount,
        # so the ledger takes the split.
        report_llm(
            model, reply.tokens_in - reply.cached_in, reply.tokens_out, cache_read=reply.cached_in
        )
        gen.record_usage(
            reply.tokens_in,
            reply.tokens_out,
            priced=is_priced(model),
            cached_input_tokens=reply.cached_in,
        )
        # Bounded prompt + completion, and the prompt-version link. Here
        # rather than at the call sites because this is the only place
        # that holds the system prompt, the user prompt AND the model's
        # reply together — and it covers every caller by construction.
        gen.record_content(system=call.system, user=call.user, output=reply.text)
    except Exception:  # noqa: BLE001 — content capture must not fail the call
        pass


class OpenAILLMClient:
    provider = "openai"
    # What /v1/health and an error's detail call this client's calls.
    _source = "openai:responses"
    # The API the client speaks.
    _wire: Wire = Responses()
    # Whether the client refuses to start without OPENAI_API_KEY.
    _needs_key = True
    # This client's own reasoning effort ('' = the pin, else the lowest the model takes).
    reasoning_effort = ""

    def __init__(
        self, model: str, *, api_key: str | None = None, reasoning_effort: str = ""
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._api_key = ""
        if self._needs_key:
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
        call = Call(
            system=system,
            user=user,
            max_tokens=max_tokens,
            response_format=response_format,
            temperature=temperature,
            effort=self.reasoning_effort,
        )
        from server.observability.genai import generation_span
        from server.observability.provider_health import recording

        # Span covers the WHOLE retry loop: tenacity retries up to 4
        # attempts and only the last one's usage is returned, so timing a
        # single attempt would under-report latency by up to 4x.
        with generation_span(provider=self.provider, model=self.model) as gen:
            # A failure after the retries (or a non-retryable one, or an
            # unreadable answer) marks the provider failing for /v1/health.
            with recording(self._source):
                reply = await self._send(call, who, gen)
            _report(gen, self.model, call, reply)
        return LLMResult(
            text=reply.text,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
            provider=self.provider,
            model=self.model,
            cached_input_tokens=reply.cached_in,
            stop_reason=reply.stop_reason,
        )

    async def _send(self, call: Call, who: customer.Customer, gen: Any) -> Reply:
        """The reply after the retries. Within an attempt, a 400 that teaches the
        model's traits re-sends the call without what it refused, and a reply
        that reasoning starved is re-sent once with room."""
        client = _http_client()
        wire, model = self._wire, self.model
        url = f"{self._root().rstrip('/')}{wire.path}"
        headers = {
            **await self._headers(url),
            "Content-Type": "application/json",
            **customer.headers(who),
        }
        payload = wire.payload(model, call)
        async for attempt in retrying():
            with attempt:
                gen.note_attempt()
                r = await _post(client, url, headers, payload)
                for _ in range(traits.MAX_NEGOTIATION):
                    # A refused field or effort: re-send without it.
                    if not learn_from_refusal(wire, model, r, payload):
                        break
                    payload = wire.payload(model, call)
                    r = await _post(client, url, headers, payload)
                reply = self._read(url, r)
                traits.note_hidden(model, reply.hidden)
                cap_field = wire.cap_field(payload)
                if reply.starved and cap_field:
                    # Reasoning spent the whole cap: once more with room.
                    payload[cap_field] = traits.starved_cap(
                        model, int(payload[cap_field]), reply.hidden
                    )
                    r = await _post(client, url, headers, payload)
                    reply = self._read(url, r)
                return reply
        raise RuntimeError("LLM retry loop exited without result")

    def _read(self, url: str, r: httpx.Response) -> Reply:
        """The reply of an answer that is neither an HTTP error nor a failed response."""
        _raise_for_status(r, self._source)
        data = r.json()
        _raise_for_failed(data, url, self._source)
        return self._wire.reply(data)
