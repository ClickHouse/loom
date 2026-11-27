"""One-shot Vertex AI completions using Google-managed workload credentials."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from server.clients._http import get_model_client
from server.clients._http_errors import raise_for_status_with_detail
from server.core.errors import GatewayRequiredError
from server.core.llm_provider import vertex_route
from server.llm import customer, traits, vertex_auth
from server.llm._retry import retrying
from server.llm.client import LLMResult
from server.llm.json_text import strip_code_fence
from server.llm.openai import Call, Reply, _report
from server.observability import cost_accumulator
from server.observability.genai import generation_span
from server.observability.provider_health import recording

_SOURCE = "vertex:generateContent"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # not a setting: fixed model-response decoder memory ceiling
_STOP_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "CONTINUATION": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "MODEL_ARMOR": "content_filter",
}
_SCHEMA_FIELDS = frozenset(
    {
        "$id",
        "$defs",
        "$ref",
        "$anchor",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "additionalProperties",
        "required",
        "propertyOrdering",
    }
)


def _check_schema(schema: object) -> None:
    if not isinstance(schema, dict) or not schema or set(schema) - _SCHEMA_FIELDS:
        raise ValueError("Vertex AI requires a JSON schema with supported keywords")
    for keyword in ("properties", "$defs"):
        children = schema.get(keyword, {})
        if not isinstance(children, dict):
            raise ValueError(f"Vertex AI JSON schema {keyword} must be an object")
        for child in children.values():
            _check_schema(child)
    for keyword in ("anyOf", "oneOf", "prefixItems"):
        if keyword not in schema:
            continue
        choices = schema[keyword]
        if not isinstance(choices, list) or not choices:
            raise ValueError(f"Vertex AI JSON schema {keyword} must be a nonempty list")
        for choice in choices:
            _check_schema(choice)
    if "items" in schema:
        _check_schema(schema["items"])
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        _check_schema(schema["additionalProperties"])


def _response_options(response_format: Mapping[str, Any] | None) -> dict[str, Any]:
    if response_format is None or response_format == {"type": "text"}:
        return {}
    options: dict[str, Any] = {"responseMimeType": "application/json"}
    if response_format == {"type": "json_object"}:
        return options
    if set(response_format) != {"type", "json_schema"} or response_format["type"] != "json_schema":
        raise ValueError("Vertex AI response format must be text, json_object, or json_schema")
    wrapper = response_format["json_schema"]
    if not isinstance(wrapper, dict) or set(wrapper) - {"name", "strict", "schema"}:
        raise ValueError("Vertex AI json_schema response format requires a schema object")
    schema = wrapper.get("schema")
    _check_schema(schema)
    return {**options, "responseJsonSchema": schema}


def _payload(call: Call, output_cap: int, response_options: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {"maxOutputTokens": output_cap}
    if call.temperature is not None:
        config["temperature"] = call.temperature
    config.update(response_options)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": call.user}]}],
        "generationConfig": config,
    }
    if call.system:
        payload["systemInstruction"] = {"parts": [{"text": call.system}]}
    return payload


def _usage(value: Any, *, has_text: bool) -> tuple[int, int, int, int] | None:
    if not isinstance(value, dict):
        return None
    prompt, output, cached, hidden = (
        value.get("promptTokenCount"),
        value.get("candidatesTokenCount", 0),
        value.get("cachedContentTokenCount", 0),
        value.get("thoughtsTokenCount", 0),
    )
    if any(type(count) is not int or count < 0 for count in (prompt, output, cached, hidden)):
        return None
    if not prompt or cached > prompt or (has_text and not output):
        return None
    return prompt, output, cached, hidden


def _reply(data: dict[str, Any], *, json_mode: bool) -> tuple[Reply, bool]:
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("Vertex AI completion candidates must be a list")
    if len(candidates) == 1 and isinstance(candidates[0], dict):
        candidate = candidates[0]
        reason = candidate.get("finishReason")
        if not isinstance(reason, str) or reason not in _STOP_REASONS:
            raise ValueError("Vertex AI response has no supported finish reason")
        stop = _STOP_REASONS[reason]
    elif not candidates and (data.get("promptFeedback") or {}).get("blockReason"):
        candidate, stop = {}, "content_filter"
    else:
        raise ValueError("Vertex AI completion has no single response candidate")
    text = ""
    if stop != "content_filter":
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if stop == "stop" and not text.strip():
            raise ValueError("Vertex AI returned a completed response without text")
    if json_mode:
        text = strip_code_fence(text)
    usage = _usage(data.get("usageMetadata"), has_text=bool(text))
    if usage is None:
        return Reply(text, 0, 0, 0, 0, stop), False
    prompt, output, cached, hidden = usage
    return Reply(text, prompt, output + hidden, cached, hidden, stop), True


def _report_unreported(
    gen: Any, model: str, call: Call, reply: Reply, payload: dict[str, Any], cap: int
) -> None:
    estimate = None
    if cost_accumulator.is_priced(model):
        # Estimate the input from serialized bytes, including schema and framing.
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        estimate = cost_accumulator.turn_cost_usd(model, input_tokens=size, output_tokens=cap)
    cost_accumulator.report_charge(model, estimate or 0.0, estimated=True)
    gen.record_accounting(usage_status="unknown", price_model=model, estimated_cost=estimate)
    gen.record_content(system=call.system, user=call.user, output=reply.text)


def _raise_for_vertex_status(response: httpx.Response, raw: bytearray) -> None:
    if response.is_success:
        return
    try:
        code = json.loads(raw).get("error", {}).get("status", "")
    except (ValueError, AttributeError):
        code = ""
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z_]{1,64}", code):
        code = "PROVIDER_ERROR"
    # A provider error message can include request text.
    safe = httpx.Response(
        response.status_code, request=response.request, json={"error": {"code": code}}
    )
    raise_for_status_with_detail(safe, _SOURCE)


async def _post(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    headers = await vertex_auth.headers(url, timeout_s)
    async with get_model_client().stream(
        "POST", url, headers=headers, json=payload, timeout=timeout_s, follow_redirects=False
    ) as response:
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise ValueError("Vertex AI response exceeds the model response limit")
            raw.extend(chunk)
        _raise_for_vertex_status(response, raw)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Vertex AI response must be an object")
    return data


class VertexLLMClient:
    provider = "vertex"

    def __init__(self, model: str) -> None:
        from server.settings import settings

        self.model, self._url = vertex_route(
            model, settings.vertex_project, settings.vertex_location
        )

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        from server.settings import settings

        await customer.require()
        if settings.gateway_required():
            raise GatewayRequiredError("Vertex AI cannot bypass the required inference gateway")
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("Vertex AI requires a positive integer output limit")
        call = Call(system, user, max_tokens, response_format, temperature, effort="")
        response_options = _response_options(call.response_format)
        timeout_s = settings.llm_request_timeout_s
        deadline = time.monotonic() + timeout_s
        with recording(_SOURCE):
            async with asyncio.timeout(timeout_s):
                cap = traits.output_cap(self.model, call.max_tokens)
                reply = await self._send(call, cap, response_options, deadline)
                if reply.starved and reply.hidden:
                    cap = traits.starved_cap(self.model, call.max_tokens, reply.hidden)
                    reply = await self._send(call, cap, response_options, deadline)
        return LLMResult(
            text=reply.text,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
            provider=self.provider,
            model=self.model,
            cached_input_tokens=reply.cached_in,
            stop_reason=reply.stop_reason,
        )

    async def _send(
        self, call: Call, cap: int, response_options: dict[str, Any], deadline: float
    ) -> Reply:
        payload = _payload(call, cap, response_options)
        with generation_span(provider=self.provider, model=self.model) as gen:
            async for attempt in retrying():
                with attempt:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Vertex AI completion exceeded its request deadline")
                    gen.note_attempt()
                    data = await _post(self._url, payload, remaining)
                    reply, reported = _reply(data, json_mode=bool(response_options))
                    if reported:
                        traits.note_hidden(self.model, reply.hidden)
                        _report(gen, self.model, call, reply)
                    else:
                        _report_unreported(gen, self.model, call, reply, payload, cap)
                    return reply
        raise RuntimeError("Vertex AI retry loop exited without a completion")
