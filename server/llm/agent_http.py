"""OpenAI and Anthropic streaming tool requests over the guarded model transport."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from server.clients._http import get_model_client
from server.core.errors import AssistantUpstreamError
from server.core.provider_keys import provider_key
from server.llm import customer, gateway
from server.llm.agent import ToolCall, Turn, Usage, count, model_stream, sse
from server.observability.provider_health import recording
from server.settings import settings


def anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            continue
        if role == "tool":
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message["content"],
                }
            ]
            role = "user"
        else:
            blocks = (
                [{"type": "text", "text": message["content"]}] if message.get("content") else []
            )
            for call in message.get("tool_calls", []):
                fn = call["function"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": fn["name"],
                        "input": json.loads(fn["arguments"]),
                    }
                )
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return out


class HTTPAgentModel:
    def __init__(self, provider: str, model: str) -> None:
        self.provider, self.model = provider, model

    async def _request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        who = await customer.require()
        anthropic = self.provider == "anthropic"
        root = (
            settings.anthropic_base_url
            if anthropic
            else settings.local_llm_base_url
            if self.provider == "local"
            else settings.openai_base_url
        ).rstrip("/")
        url = root + ("/v1/messages" if anthropic else "/chat/completions")
        key = provider_key(self.provider) if self.provider != "local" else ""
        bearer = await gateway.bearer(key, url)
        headers = {"Authorization": f"Bearer {bearer}", **customer.headers(who)}
        if anthropic:
            headers.update({"x-api-key": bearer, "anthropic-version": "2023-06-01"})
            body = {
                "model": self.model,
                "stream": True,
                "max_tokens": max_tokens,
                "system": messages[0]["content"],
                "messages": anthropic_messages(messages),
                "tools": [
                    {
                        "name": t["function"]["name"],
                        "description": t["function"]["description"],
                        "input_schema": t["function"]["parameters"],
                    }
                    for t in tools
                ],
            }
        else:
            # No temperature or speculative capability negotiation. An incompatible
            # endpoint refuses this route; no paid 400 is retried with another field.
            cap = "max_tokens" if self.provider == "local" else "max_completion_tokens"
            body = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "stream": True,
                "stream_options": {"include_usage": True},
                cap: max_tokens,
            }
        return url, headers, body

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        turn: Turn,
    ) -> AsyncGenerator[str]:
        url, headers, body = await self._request(messages, tools, max_tokens)
        client = get_model_client()
        with recording(f"{self.provider}:assistant"):
            turn.sent = True
            async with model_stream(
                client,
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=settings.llm_request_timeout_s,
                follow_redirects=False,
                extensions={"loom_identity_retried": True},
            ) as response:
                # A streaming refusal's body may be endless or contain credentials.
                response.raise_for_status()
                parser = (
                    AnthropicStream(turn) if self.provider == "anthropic" else OpenAIStream(turn)
                )
                async for frame in sse(response):
                    text = parser.feed(frame)
                    if text:
                        turn.text += text
                        if len(turn.text) > settings.assistant_max_output_chars:
                            raise AssistantUpstreamError("the answer exceeded its output bound")
                        yield text
                parser.finish()


class OpenAIStream:
    def __init__(self, turn: Turn) -> None:
        self.turn = turn
        self.calls: dict[int, ToolCall] = {}
        self.done = False

    def feed(self, frame: dict[str, Any] | None) -> str:
        if self.done:
            raise AssistantUpstreamError("the model sent data after its terminal marker")
        if frame is None:
            self.done = True
            return ""
        if frame.get("usage") is not None:
            self._usage(frame["usage"])
        choices = frame.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise AssistantUpstreamError("the model returned invalid choices")
        if not choices:
            return ""
        choice = choices[0]
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict) or delta.get("refusal"):
            raise AssistantUpstreamError("the model refused the answer")
        if self.turn.stop:
            raise AssistantUpstreamError("the model sent choices after its stop")
        for call in delta.get("tool_calls", []):
            self._call(call)
        if choice.get("finish_reason") is not None:
            self.turn.stop = str(choice["finish_reason"])
        text = delta.get("content") or ""
        if not isinstance(text, str):
            raise AssistantUpstreamError("the model returned non-text content")
        return text

    def _usage(self, raw: dict[str, Any]) -> None:
        if self.turn.usage is not None:
            raise AssistantUpstreamError("the model repeated usage")
        total = count(raw.get("prompt_tokens"))
        cached = count((raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
        if cached > total:
            raise AssistantUpstreamError("the model returned invalid cache usage")
        details = raw.get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens")
        self.turn.usage = Usage(
            total - cached,
            count(raw.get("completion_tokens")),
            cached,
            reasoning=count(reasoning) if reasoning is not None else None,
        )
        self.turn.usage_complete = True

    def _call(self, raw: dict[str, Any]) -> None:
        index = count(raw.get("index"))
        if index >= settings.assistant_max_tool_calls:
            raise AssistantUpstreamError("the model requested too many tools")
        if index not in self.calls:
            self.calls[index] = ToolCall("", "", "")
        call = self.calls[index]
        call.id += str(raw.get("id") or "")
        fn = raw.get("function") or {}
        call.name += str(fn.get("name") or "")
        call.arguments += str(fn.get("arguments") or "")
        if len(call.arguments) > 20000 or len(call.name) > 200 or len(call.id) > 256:
            raise AssistantUpstreamError("the model returned an oversized tool request")

    def finish(self) -> None:
        if not self.done:
            raise AssistantUpstreamError("the model stream ended without its terminal marker")
        if sorted(self.calls) != list(range(len(self.calls))):
            raise AssistantUpstreamError("the model omitted a tool frame")
        self.turn.calls = list(self.calls.values())
        self.turn.check()


class AnthropicStream:
    def __init__(self, turn: Turn) -> None:
        self.turn = turn
        self.blocks: dict[int, ToolCall | None] = {}
        self.open: set[int] = set()
        self.started = self.done = False

    def feed(self, frame: dict[str, Any] | None) -> str:
        if frame is None or self.done:
            raise AssistantUpstreamError("the model returned an invalid terminal frame")
        kind = frame.get("type")
        if kind == "ping":
            return ""
        if kind == "message_start":
            if self.started:
                raise AssistantUpstreamError("the model repeated message_start")
            self.started = True
            self._usage(frame["message"]["usage"], initial=True)
        elif not self.started:
            raise AssistantUpstreamError("the model omitted message_start")
        elif kind == "content_block_start":
            self._start(frame)
        elif kind == "content_block_delta":
            return self._delta(frame)
        elif kind == "content_block_stop":
            index = count(frame.get("index"))
            if index not in self.open:
                raise AssistantUpstreamError("the model closed an unopened block")
            self.open.remove(index)
        elif kind == "message_delta":
            if self.turn.stop:
                raise AssistantUpstreamError("the model repeated its stop")
            stop = frame["delta"].get("stop_reason")
            self.turn.stop = {"end_turn": "stop", "tool_use": "tool_calls"}.get(stop, str(stop))
            self._usage(frame["usage"], initial=False)
        elif kind == "message_stop":
            self.done = True
        else:
            raise AssistantUpstreamError("the model returned an unsupported stream frame")
        return ""

    def _start(self, frame: dict[str, Any]) -> None:
        index = count(frame.get("index"))
        if index in self.blocks or len(self.blocks) >= 64:
            raise AssistantUpstreamError("the model repeated or exceeded its content blocks")
        block = frame["content_block"]
        kind = block.get("type")
        if kind == "text" and not block.get("text"):
            self.blocks[index] = None
        elif kind == "tool_use" and not block.get("input"):
            self.blocks[index] = ToolCall(str(block["id"]), str(block["name"]), "")
        else:
            raise AssistantUpstreamError("the model returned unsupported content")
        self.open.add(index)

    def _delta(self, frame: dict[str, Any]) -> str:
        index = count(frame.get("index"))
        if index not in self.open:
            raise AssistantUpstreamError("the model wrote to an unopened block")
        delta, call = frame["delta"], self.blocks[index]
        if delta.get("type") == "text_delta" and call is None:
            text = delta["text"]
            if isinstance(text, str):
                return text
        elif delta.get("type") == "input_json_delta" and call is not None:
            call.arguments += delta["partial_json"]
            if len(call.arguments) <= 20000:
                return ""
        raise AssistantUpstreamError("the model returned an invalid block delta")

    def _usage(self, raw: dict[str, Any], *, initial: bool) -> None:
        if initial:
            writes = count(raw.get("cache_creation_input_tokens", 0))
            split = raw.get("cache_creation") or {}
            hour = count(split.get("ephemeral_1h_input_tokens", 0))
            if hour > writes:
                raise AssistantUpstreamError("the model returned invalid cache usage")
            self.turn.usage = Usage(
                count(raw.get("input_tokens")),
                0,
                count(raw.get("cache_read_input_tokens", 0)),
                writes - hour,
                hour,
            )
        else:
            if self.turn.usage is None:
                raise AssistantUpstreamError("the model omitted input usage")
            self.turn.usage.output = count(raw.get("output_tokens"))
            self.turn.usage_complete = True

    def finish(self) -> None:
        if not self.done or self.open:
            raise AssistantUpstreamError("the model stream ended before message_stop")
        self.turn.calls = [b for b in self.blocks.values() if b is not None]
        self.turn.check()
