"""Streaming model contract for a bounded tool loop, separate from one-shot synthesis."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from server.core import request_tail, shielded
from server.core.errors import AssistantUpstreamError


@dataclass
class Usage:
    input: int
    output: int
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    reasoning: int | None = None

    @property
    def total_input(self) -> int:
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


@dataclass
class Turn:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    stop: str = ""
    sent: bool = False
    usage_complete: bool = False

    def reported_usage(self) -> Usage | None:
        if self.usage_complete and self.usage is not None and self.usage.total_input > 0:
            return self.usage
        return None

    def check(self) -> None:
        if self.reported_usage() is None:
            raise AssistantUpstreamError("the model ended without complete usage")
        if self.stop not in ("stop", "tool_calls"):
            raise AssistantUpstreamError("the model refused or cut the answer")
        if bool(self.calls) != (self.stop == "tool_calls"):
            raise AssistantUpstreamError("the model's tool frame is incomplete")
        if not self.calls and not self.text.strip():
            raise AssistantUpstreamError("the model returned no answer")
        ids = [call.id for call in self.calls]
        if len(set(ids)) != len(ids) or any(not i for i in ids):
            raise AssistantUpstreamError("the model repeated or omitted a tool call id")


class AgentModel(Protocol):
    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        turn: Turn,
    ) -> AsyncGenerator[str]: ...


@asynccontextmanager
async def model_stream(client: Any, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
    """Close the provider response despite repeated cancellation before accounting."""
    context = client.stream(*args, **kwargs)
    response = await context.__aenter__()
    try:
        yield response
    finally:

        async def closed() -> None:
            async with asyncio.timeout(request_tail.REQUIRED_STEP_TIMEOUT_S):
                await context.__aexit__(None, None, None)

        await shielded.to_its_end(closed(), name="assistant.model.response")


def count(raw: Any) -> int:
    if type(raw) is not int or raw < 0:
        raise AssistantUpstreamError("the model returned invalid usage")
    return raw


async def sse(response: Any) -> AsyncGenerator[dict[str, Any] | None]:
    """Bound every frame before parsing; None is the explicit OpenAI terminal marker."""
    # aiter_bytes can buffer an unbounded line; consume bounded network chunks instead.
    pending = b""
    async for chunk in response.aiter_bytes():
        pending += chunk
        if len(pending) > 262144:
            raise AssistantUpstreamError("the model returned an oversized stream frame")
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                yield None
                continue
            try:
                frame = json.loads(data)
            except (ValueError, UnicodeError) as e:
                raise AssistantUpstreamError("the model returned a malformed stream frame") from e
            if not isinstance(frame, dict) or frame.get("error"):
                raise AssistantUpstreamError("the model returned a stream error")
            yield frame
    if pending.strip():
        raise AssistantUpstreamError("the model stream ended inside a frame")


class FakeAgentModel:
    """An explicit, deterministic offline route; never a missing-key fallback."""

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        turn: Turn,
    ) -> AsyncGenerator[str]:
        del tools, max_tokens
        turn.sent = True
        turn.text = "Nothing stored in Loom answers this."
        turn.usage = Usage(input=len(json.dumps(messages).encode()), output=10)
        turn.usage_complete = True
        turn.stop = "stop"
        yield turn.text


def model_for(provider: str, model: str) -> AgentModel:
    if provider == "fake":
        return FakeAgentModel()
    if provider in ("openai", "anthropic", "local"):
        from server.llm.agent_http import HTTPAgentModel

        return HTTPAgentModel(provider, model)
    if provider == "bedrock":
        from server.llm.agent_bedrock import BedrockAgentModel

        return BedrockAgentModel(model)
    raise ValueError("unsupported assistant provider")
