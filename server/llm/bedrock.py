"""One-shot Bedrock Converse completions using deployment-managed AWS credentials."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

import httpx

from server.clients import bedrock
from server.llm import customer, traits
from server.llm.client import LLMResult
from server.llm.json_text import strip_code_fence
from server.observability.genai import generation_span
from server.observability.provider_health import recording
from server.settings import settings


def _result(model: str, data: dict[str, Any], *, json_mode: bool) -> LLMResult:
    stop = str(data.get("stopReason") or "")
    message = (data.get("output") or {}).get("message")
    if message is None and traits.is_complete_stop(stop):
        raise ValueError("Bedrock completion has no output message")
    content = (message or {}).get("content") or []
    text = "".join(block["text"] for block in content if "text" in block)
    if json_mode:
        text = strip_code_fence(text)
    usage = data.get("usage") or {}
    read = max(0, int(usage.get("cacheReadInputTokens", 0)))
    written = max(0, int(usage.get("cacheWriteInputTokens", 0)))
    return LLMResult(
        text=text,
        tokens_in=int(usage.get("inputTokens", 0)) + read + written,
        tokens_out=int(usage.get("outputTokens", 0)),
        provider="bedrock",
        model=model,
        cached_input_tokens=read,
        cache_write_input_tokens=written,
        stop_reason=stop,
    )


def _record(span: Any, result: LLMResult, system: str, user: str) -> None:
    try:
        from server.observability.cost_accumulator import is_priced, report_llm

        read, written = result.cached_input_tokens, result.cache_write_input_tokens
        report_llm(
            result.model,
            result.tokens_in - read - written,
            result.tokens_out,
            cache_read=read,
            cache_write_5m=written,
        )
        span.record_usage(
            result.tokens_in,
            result.tokens_out,
            priced=is_priced(result.model),
            cached_input_tokens=read,
            cache_write_5m_tokens=written,
        )
        span.record_content(system=system, user=user, output=result.text)
    except Exception:  # noqa: BLE001 — telemetry must not fail a completion
        pass


class BedrockLLMClient:
    provider = "bedrock"

    def __init__(self, model: str) -> None:
        self.model = model
        self._model_id = bedrock.model_id(model)

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        who = await customer.require()
        json_mode = (response_format or {}).get("type") == "json_object"
        if json_mode:
            system = f"{system}\nRespond with exactly one valid JSON object, without markdown."
        config: dict[str, Any] = {"maxTokens": max_tokens}
        if temperature is not None and "temperature" not in traits.traits(self.model).refused:
            config["temperature"] = temperature
        payload: dict[str, Any] = {
            "modelId": self._model_id,
            "inferenceConfig": config,
            "messages": [{"role": "user", "content": [{"text": user}]}],
        }
        if system:
            payload["system"] = [{"text": system}]
        with generation_span(provider=self.provider, model=self.model) as span:
            with recording("bedrock:messages"):
                send = partial(
                    bedrock.call,
                    "converse",
                    payload,
                    who,
                    request_timeout_s=settings.llm_request_timeout_s,
                    attempts=settings.llm_retry_attempts,
                    max_wait_s=settings.llm_retry_max_wait_s,
                    on_attempt=span.note_attempt,
                )
                async with asyncio.timeout(settings.llm_request_timeout_s):
                    try:
                        data = await send()
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 400:
                            raise
                        name = traits.refused_field(exc.response.json(), config, ("temperature",))
                        if name is None:
                            raise
                        traits.traits(self.model).refused.add(name)
                        config.pop(name)
                        data = await send()
                result = _result(self.model, data, json_mode=json_mode)
            _record(span, result, system, user)
        return result
