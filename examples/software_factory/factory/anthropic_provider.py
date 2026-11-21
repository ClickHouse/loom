"""Direct Messages API transport for the factory's bounded tool loop."""

from __future__ import annotations

import copy
import json

import httpx


def wire_messages(messages: list[dict]) -> list[dict]:
    output = []
    for message in messages:
        content = []
        for block in message["content"]:
            if "toolUse" in block:
                call = block["toolUse"]
                content.append(
                    {
                        "type": "tool_use",
                        "id": call["toolUseId"],
                        "name": call["name"],
                        "input": copy.deepcopy(call["input"]),
                    }
                )
            elif "toolResult" in block:
                result = block["toolResult"]
                values = result["content"]
                text = "\n".join(
                    json.dumps(value["json"]) if "json" in value else value["text"]
                    for value in values
                )
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result["toolUseId"],
                        "content": text,
                        "is_error": any("error" in value.get("json", {}) for value in values),
                    }
                )
            elif "anthropic_native" in block:
                content.append(copy.deepcopy(block["anthropic_native"]))
            else:
                content.append({"type": "text", "text": block["text"]})
        if message["role"] == "user":
            # The native API requires results before context text in the same user turn.
            content = [block for block in content if block["type"] == "tool_result"] + [
                block for block in content if block["type"] != "tool_result"
            ]
        output.append({"role": message["role"], "content": content})
    return output


def engine_blocks(content: list[dict]) -> list[dict]:
    output = []
    for block in content:
        if block["type"] == "tool_use":
            output.append(
                {
                    "toolUse": {
                        "toolUseId": block["id"],
                        "name": block["name"],
                        "input": block["input"],
                    }
                }
            )
        elif block["type"] == "text":
            output.append({"text": block["text"]})
        else:
            # Signed reasoning and opaque blocks must survive the next request unchanged.
            output.append({"anthropic_native": copy.deepcopy(block)})
    return output


class AnthropicFailure(RuntimeError):
    def __init__(self, reason: str, *, physical_requests: int, request_id: str | None):
        super().__init__(reason)
        self.physical_requests = physical_requests
        self.provider_request_id = request_id


class AnthropicProvider:
    def __init__(
        self,
        api_key: str,
        model_id: str,
        *,
        approved_model_id: str,
        output_limit: int = 128000,
        transport=None,
    ):
        if not api_key or not model_id or model_id != approved_model_id:
            raise PermissionError("Use the exact model frozen in the protocol")
        self.model_id = model_id
        self.output_limit = output_limit
        self.label = "anthropic-messages:" + model_id
        self.client = httpx.Client(
            base_url="https://api.anthropic.com",
            follow_redirects=False,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=httpx.Timeout(None, connect=5),
            transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
        )

    def close(self) -> None:
        self.client.close()

    def step(self, messages: list[dict], tools: list[dict], max_tokens: int) -> dict:
        if type(max_tokens) is not int or not 0 < max_tokens <= self.output_limit:
            raise ValueError("Output exceeds the model's declared API limit")
        payload = {
            "model": self.model_id,
            "messages": wire_messages(messages),
            "tools": copy.deepcopy(tools),
        }
        response = None
        try:
            response = self.client.post("/v1/messages", json={**payload, "max_tokens": max_tokens})
            response.raise_for_status()
            native = response.json()
            if native.get("model") != self.model_id:
                raise ValueError("Response model differs from the frozen model")
            return {
                "blocks": engine_blocks(native["content"]),
                "usage": native["usage"],
                "raw_content": native["content"],
                "stop_reason": native["stop_reason"],
                "provider_request_id": response.headers.get("request-id"),
                "physical_requests": 1,
                "retries": 0,
                "counting_physical_requests": 0,
                "counting_latency_s": 0,
                "cost_usd": None,
                "cost_status": "unknown",
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise AnthropicFailure(
                type(error).__name__,
                physical_requests=1,
                request_id=response.headers.get("request-id") if response is not None else None,
            ) from error


def accounting_usage(response: dict) -> dict:
    """Price only explicitly uncached standard usage; retain every raw field separately."""
    usage = response["usage"]
    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if type(usage.get(field)) is not int or usage[field] != 0:
            raise ValueError("Cache usage requires a separately frozen pricing rule")
    if usage.get("service_tier", "standard") != "standard":
        raise ValueError("Service tier differs from the standard price table")
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    if any(type(value) is not int or value < 0 for value in (incoming, outgoing)):
        raise ValueError("Provider usage is incomplete")
    return {"inputTokens": incoming, "outputTokens": outgoing, "totalTokens": incoming + outgoing}
