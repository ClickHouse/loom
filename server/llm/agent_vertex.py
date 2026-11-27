"""Vertex AI streaming and request-local Gemini tool history."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from server.clients._http import get_model_client
from server.core.errors import AssistantUpstreamError, GatewayRequiredError
from server.core.llm_provider import vertex_route
from server.llm import customer, gateway, vertex_auth
from server.llm.agent import ToolCall, Turn, Usage, count, model_stream, sse
from server.observability.provider_health import recording
from server.settings import settings


def _contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}
    host_calls: dict[str, dict[str, str]] = {}

    def append(role: str, parts: list[dict[str, Any]]) -> None:
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": list(parts)})

    for message in messages[1:]:
        role = message["role"]
        if role == "tool":
            call_id = message["tool_call_id"]
            result = json.loads(message["content"])
            if call_id in host_calls:
                call = host_calls[call_id]
                evidence = {
                    "tool": call["name"],
                    "arguments": json.loads(call["arguments"]),
                    "result": result,
                }
                append(
                    "user",
                    [
                        {
                            "text": "Loom retrieved context:\n"
                            + json.dumps(evidence, ensure_ascii=False)
                        }
                    ],
                )
            else:
                call = calls[call_id]
                response = {"name": call["name"], "response": {"output": result}}
                if provider_id := call.get("id"):
                    response["id"] = provider_id
                append("user", [{"functionResponse": response}])
        elif parts := message.get("vertex_parts"):
            provider_calls = (part["functionCall"] for part in parts if "functionCall" in part)
            calls.update(
                (local["id"], provider)
                for local, provider in zip(message["tool_calls"], provider_calls, strict=True)
            )
            # Signatures belong to individual parts. Preserve their order and contents.
            append("model", parts)
        elif host := message.get("tool_calls"):
            # Loom preloads context before the model can produce a signed tool call.
            host_calls.update((call["id"], call["function"]) for call in host)
        else:
            append("model" if role == "assistant" else "user", [{"text": message["content"]}])
    return contents


def _body(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": messages[0]["content"]}]},
        "contents": _contents(messages),
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": fn["name"],
                        "description": fn["description"],
                        "parametersJsonSchema": fn["parameters"],
                    }
                    for fn in (tool["function"] for tool in tools)
                ]
            }
        ]
    return body


def _usage(raw: dict[str, Any]) -> Usage:
    prompt = count(raw.get("promptTokenCount"))
    cached = count(raw.get("cachedContentTokenCount", 0))
    if cached > prompt:
        raise AssistantUpstreamError("Vertex returned invalid cache usage")
    output = count(raw.get("candidatesTokenCount", 0))
    thoughts = count(raw.get("thoughtsTokenCount", 0))
    tool_input = count(raw.get("toolUsePromptTokenCount", 0))
    return Usage(prompt + tool_input - cached, output + thoughts, cached, reasoning=thoughts)


class _VertexStream:
    def __init__(self, turn: Turn) -> None:
        self.turn = turn
        self.parts: list[dict[str, Any]] = []
        self.part_bytes = 0
        self.finished = False
        self.usage: dict[str, Any] | None = None

    def feed(self, frame: dict[str, Any] | None) -> str:
        if frame is None:
            raise AssistantUpstreamError("Vertex returned an invalid stream frame")
        candidates = frame.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 1:
            raise AssistantUpstreamError("Vertex returned invalid candidates")
        text = self._candidate(candidates[0]) if candidates else ""
        feedback = frame.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason") not in (
            None,
            "BLOCKED_REASON_UNSPECIFIED",
        ):
            self.turn.stop = "content_filter"
            self.finished = True
        usage = frame.get("usageMetadata")
        if self.finished and usage is not None:
            if not isinstance(usage, dict):
                raise AssistantUpstreamError("Vertex returned invalid usage metadata")
            self.usage = usage
        return text

    def _candidate(self, candidate: Any) -> str:
        if self.finished or not isinstance(candidate, dict) or candidate.get("index", 0) != 0:
            raise AssistantUpstreamError("Vertex returned an invalid candidate frame")
        content = candidate.get("content", {})
        if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
            raise AssistantUpstreamError("Vertex returned invalid content")
        text = "".join(self._part(part) for part in content.get("parts", []))
        reason = candidate.get("finishReason")
        if reason and reason != "FINISH_REASON_UNSPECIFIED":
            self.finished = True
            if reason == "STOP":
                self.turn.stop = "tool_calls" if self.turn.calls else "stop"
            else:
                self.turn.stop = "provider_stop"
        return text

    def _part(self, part: Any) -> str:
        if not isinstance(part, dict) or not isinstance(text := part.get("text", ""), str):
            raise AssistantUpstreamError("Vertex returned an invalid content part")
        self.part_bytes += len(json.dumps(part, ensure_ascii=False).encode())
        if self.part_bytes > settings.assistant_max_context_bytes:
            raise AssistantUpstreamError("Vertex returned too much model context")
        self.parts.append(part)
        if "functionCall" in part:
            self._call(part["functionCall"])
        return "" if part.get("thought") else text

    def _call(self, call: Any) -> None:
        if not isinstance(call, dict) or call.get("partialArgs") or call.get("willContinue"):
            raise AssistantUpstreamError("Vertex returned an incomplete tool request")
        if len(self.turn.calls) >= settings.assistant_max_tool_calls:
            raise AssistantUpstreamError("the model requested too many tools")
        name, args, call_id = call.get("name"), call.get("args", {}), call.get("id")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(args, dict)
            or (call_id is not None and not isinstance(call_id, str))
        ):
            raise AssistantUpstreamError("Vertex returned an invalid tool request")
        arguments = json.dumps(args, ensure_ascii=False)
        if len(name) > 200 or len(arguments) > 20000:
            raise AssistantUpstreamError("the model returned an oversized tool request")
        self.turn.calls.append(ToolCall(uuid.uuid4().hex, name, arguments))

    def finish(self) -> None:
        if not self.finished:
            raise AssistantUpstreamError("Vertex ended without a finish reason")
        # Before the finish reason, a usage snapshot can cover only part of the answer.
        if self.usage is not None:
            self.turn.usage = _usage(self.usage)
            # Tool-only turns can report no candidate tokens. Text still needs accounting.
            needs_candidate_usage = self.turn.stop == "stop" or (
                self.turn.stop == "tool_calls" and bool(self.turn.text)
            )
            self.turn.usage_complete = (
                not needs_candidate_usage or self.usage.get("candidatesTokenCount", 0) > 0
            )
        self.turn.check()
        if self.turn.calls:
            self.turn.vertex_parts = self.parts


class VertexAgentModel:
    def __init__(self, model: str) -> None:
        _, endpoint = vertex_route(model, settings.vertex_project, settings.vertex_location)
        self._url = endpoint.removesuffix(":generateContent") + ":streamGenerateContent"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        turn: Turn,
    ) -> AsyncGenerator[str]:
        await customer.require()
        if settings.gateway_required() or gateway.keyless():
            raise GatewayRequiredError("Native Vertex cannot bypass the required inference gateway")
        body = _body(messages, tools, max_tokens)
        if (
            len(json.dumps(body, ensure_ascii=False).encode())
            > settings.assistant_max_context_bytes
        ):
            raise AssistantUpstreamError("the answer exceeded its context bound")
        timeout = settings.llm_request_timeout_s
        try:
            headers = await vertex_auth.headers(self._url, timeout)
        except vertex_auth.VertexCredentialsError:
            raise AssistantUpstreamError(
                "Vertex AI credentials are unavailable. "
                "Check Google Application Default Credentials and IAM."
            ) from None
        parser = _VertexStream(turn)
        with recording("vertex:assistant"):
            turn.sent = True
            async with model_stream(
                get_model_client(),
                "POST",
                self._url,
                params={"alt": "sse"},
                json=body,
                headers={**headers, "accept": "text/event-stream"},
                timeout=timeout,
                follow_redirects=False,
                extensions={"loom_identity_retried": True, "loom_bounded_refusal": True},
            ) as response:
                response.raise_for_status()
                async for frame in sse(response):
                    text = parser.feed(frame)
                    if text:
                        turn.text += text
                        if len(turn.text) > settings.assistant_max_output_chars:
                            raise AssistantUpstreamError("the answer exceeded its output bound")
                        yield text
                parser.finish()
