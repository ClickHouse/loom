"""Bedrock Converse streaming over the same guarded HTTP pool as other models."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from server.clients import bedrock
from server.clients._http import get_model_client, refuse_off_gateway
from server.core import request_tail, shielded
from server.core.errors import AssistantUpstreamError, GatewayRequiredError
from server.llm import customer
from server.llm.agent import ToolCall, Turn, Usage, count, model_stream
from server.llm.agent_http import anthropic_messages
from server.observability.provider_health import recording
from server.settings import settings


def _usage(raw: dict[str, Any]) -> Usage:
    written = count(raw.get("cacheWriteInputTokens", 0))
    split = {"5m": 0, "1h": 0}
    details = raw.get("cacheDetails") or []
    for part in details:
        ttl = part.get("ttl")
        if ttl not in split:
            raise AssistantUpstreamError("Bedrock returned an unknown cache TTL")
        split[ttl] += count(part.get("inputTokens"))
    if written != sum(split.values()):
        raise AssistantUpstreamError("Bedrock omitted its cache-write price breakdown")
    return Usage(
        count(raw.get("inputTokens")),
        count(raw.get("outputTokens")),
        count(raw.get("cacheReadInputTokens", 0)),
        split["5m"],
        split["1h"],
    )


def converse_body(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int
) -> dict[str, Any]:
    converted = anthropic_messages(messages)
    out = []
    for message in converted:
        blocks = []
        for block in message["content"]:
            if block["type"] == "text":
                blocks.append({"text": block["text"]})
            elif block["type"] == "tool_use":
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": block["id"],
                            "name": block["name"],
                            "input": block["input"],
                        }
                    }
                )
            else:
                blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": block["tool_use_id"],
                            "content": [{"text": block["content"]}],
                        }
                    }
                )
        out.append({"role": message["role"], "content": blocks})
    return {
        "messages": out,
        "system": [{"text": messages[0]["content"]}],
        "inferenceConfig": {"maxTokens": max_tokens},
        "toolConfig": {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["function"]["name"],
                        "description": t["function"]["description"],
                        "inputSchema": {"json": t["function"]["parameters"]},
                    }
                }
                for t in tools
            ]
        },
    }


async def _signed_request(
    payload: dict[str, Any], headers: dict[str, str]
) -> tuple[str, bytes, dict[str, str]]:
    # SDK credential refresh can block. Keep it off the event loop and join it
    # before a cancelled producer can finish; it never sends a model request.
    task = asyncio.create_task(
        asyncio.to_thread(
            bedrock._signed, "converse_stream", payload, headers, settings.bedrock_region
        )
    )
    try:
        return await asyncio.shield(task)
    finally:

        async def joined() -> None:
            async with asyncio.timeout(request_tail.REQUIRED_STEP_TIMEOUT_S):
                await asyncio.gather(task, return_exceptions=True)

        await shielded.to_its_end(joined(), name="assistant.bedrock.signing")


class BedrockAgentModel:
    def __init__(self, model: str) -> None:
        self.model = model

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        turn: Turn,
    ) -> AsyncGenerator[str]:
        from botocore.eventstream import EventStreamBuffer

        who = await customer.require()
        if settings.gateway_required():
            raise GatewayRequiredError(
                "Native Bedrock cannot bypass the required inference gateway"
            )
        payload = {
            "modelId": bedrock.model_id(self.model),
            **converse_body(messages, tools, max_tokens),
        }
        url, body, headers = await _signed_request(
            payload, {**customer.headers(who), "Accept": "application/vnd.amazon.eventstream"}
        )
        refuse_off_gateway(url)
        parser = BedrockStream(turn)
        buffer = EventStreamBuffer()
        with recording("bedrock:assistant"):
            turn.sent = True
            async with model_stream(
                get_model_client(),
                "POST",
                url,
                content=body,
                headers=headers,
                timeout=settings.llm_request_timeout_s,
                follow_redirects=False,
                extensions={"loom_identity_retried": True},
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    buffer.add_data(chunk)
                    # Reject the announced frame length before the parser can accumulate it.
                    pending = buffer._data
                    if len(pending) >= 4 and int.from_bytes(pending[:4], "big") > 262144:
                        raise AssistantUpstreamError("Bedrock returned an oversized frame")
                    for event in buffer:
                        if event.headers.get(":message-type") != "event":
                            raise AssistantUpstreamError("Bedrock returned a stream error")
                        kind = event.headers.get(":event-type")
                        text = parser.feed(str(kind), json.loads(event.payload))
                        if text:
                            turn.text += text
                            if len(turn.text) > settings.assistant_max_output_chars:
                                raise AssistantUpstreamError("the answer exceeded its output bound")
                            yield text
                if buffer._data:
                    raise AssistantUpstreamError("Bedrock ended inside a frame")
            parser.finish()


class BedrockStream:
    def __init__(self, turn: Turn) -> None:
        self.turn = turn
        self.calls: dict[int, ToolCall] = {}
        self.open: set[int] = set()
        self.closed: set[int] = set()
        self.started = self.done = self.usage_seen = False

    def feed(self, kind: str, frame: dict[str, Any]) -> str:
        if kind == "messageStart" and not self.started:
            self.started = True
        elif not self.started:
            raise AssistantUpstreamError("Bedrock omitted messageStart")
        elif kind == "contentBlockStart" and not self.done:
            index = count(frame.get("contentBlockIndex"))
            if (
                index in self.open
                or index in self.closed
                or len(self.calls) >= settings.assistant_max_tool_calls
            ):
                raise AssistantUpstreamError("Bedrock repeated or exceeded tool blocks")
            tool = frame.get("start", {}).get("toolUse")
            if not isinstance(tool, dict):
                raise AssistantUpstreamError("Bedrock returned an unsupported content block")
            self.calls[index] = ToolCall(tool["toolUseId"], tool["name"], "")
            self.open.add(index)
        elif kind == "contentBlockDelta" and not self.done:
            return self._delta(frame)
        elif kind == "contentBlockStop" and not self.done:
            index = count(frame.get("contentBlockIndex"))
            if index not in self.open:
                raise AssistantUpstreamError("Bedrock closed an unopened block")
            self.open.remove(index)
            self.closed.add(index)
        elif kind == "messageStop" and not self.done:
            if self.open:
                raise AssistantUpstreamError("Bedrock left content blocks open")
            self.done = True
            stop = frame.get("stopReason")
            self.turn.stop = {"end_turn": "stop", "tool_use": "tool_calls"}.get(
                str(stop), str(stop)
            )
        elif kind == "metadata" and self.done and not self.usage_seen:
            self.usage_seen = True
            # Converse reports uncached input separately from both cache counters.
            self.turn.usage = _usage(frame["usage"])
            self.turn.usage_complete = True
        else:
            raise AssistantUpstreamError("Bedrock returned an unexpected stream frame")
        return ""

    def _delta(self, frame: dict[str, Any]) -> str:
        index = count(frame.get("contentBlockIndex"))
        if index in self.closed:
            raise AssistantUpstreamError("Bedrock reopened a closed content block")
        delta = frame["delta"]
        if "text" in delta and index not in self.calls:
            self.open.add(index)
            if isinstance(delta["text"], str):
                return delta["text"]
        if "toolUse" in delta and index in self.open and index in self.calls:
            call = self.calls[index]
            call.arguments += delta["toolUse"]["input"]
            if len(call.arguments) <= 20000:
                return ""
        raise AssistantUpstreamError("Bedrock returned an invalid content delta")

    def finish(self) -> None:
        if not self.done or not self.usage_seen:
            raise AssistantUpstreamError("Bedrock ended before its terminal usage")
        self.turn.calls = list(self.calls.values())
        self.turn.check()
