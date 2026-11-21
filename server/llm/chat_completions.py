"""OpenAI's chat-completions request and reply (`POST <root>/chat/completions`).

`LocalLLMClient` speaks it to a server the operator runs, and the bench readers and
judges (`bench/judges.py`) build their requests with it. The `openai` provider speaks
the Responses API (server/llm/openai.py); the call around either is shared there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from server.llm import traits
from server.llm.openai import Call, Reply, count, effort_for

# The output cap's field OpenAI reads first.
CAP_FIELD = "max_completion_tokens"


@dataclass(frozen=True)
class ChatCompletions:
    """The chat-completions wire. `cap_name` is the output cap's field the server
    reads first; `max_tokens`, its older name, is never dropped."""

    cap_name: str = CAP_FIELD
    path: str = "/chat/completions"
    # Fields a model may refuse (server/llm/traits.py learns which from its 400s).
    optional: tuple[str, ...] = ("temperature", "reasoning_effort", "max_completion_tokens")

    def payload(self, model: str, call: Call) -> dict[str, Any]:
        """The request this call would like to send, minus what the model refused."""
        t = traits.traits(model)
        # Prompt caching here is automatic and prefix-based (≥ 1,024 tokens,
        # no breakpoint to place): the cached run is `system` followed by
        # however much of `user` repeats.
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                *([{"role": "system", "content": call.system}] if call.system else []),
                {"role": "user", "content": call.user},
            ],
        }
        # The output cap counts hidden reasoning, so the model's learned reserve
        # rides on top of the caller's cap. Endpoints that still call it
        # `max_tokens` refuse the newer name once and get the older one after.
        cap_field = "max_tokens" if "max_completion_tokens" in t.refused else self.cap_name
        payload[cap_field] = traits.output_cap(model, call.max_tokens)
        if "reasoning_effort" not in t.refused:
            payload["reasoning_effort"] = effort_for(model, call.effort)
        if call.temperature is not None and "temperature" not in t.refused:
            payload["temperature"] = call.temperature
        if call.response_format is not None:
            # {"type": "json_object"} requires the word "json" in the prompt.
            payload["response_format"] = call.response_format
        return payload

    def cap_field(self, payload: Mapping[str, Any]) -> str | None:
        return next((k for k in ("max_completion_tokens", "max_tokens") if k in payload), None)

    def effort(self, payload: Mapping[str, Any]) -> str:
        return str(payload.get("reasoning_effort") or "")

    def reply(self, data: Any) -> Reply:
        choice = (data.get("choices") or [{}])[0] or {}
        tokens_in = count(data, "usage", "prompt_tokens")
        cached_in = count(data, "usage", "prompt_tokens_details", "cached_tokens")
        return Reply(
            text=str((choice.get("message") or {}).get("content") or ""),
            tokens_in=tokens_in,
            tokens_out=count(data, "usage", "completion_tokens"),
            cached_in=min(cached_in, tokens_in),
            hidden=count(data, "usage", "completion_tokens_details", "reasoning_tokens"),
            stop_reason=str(choice.get("finish_reason") or ""),
        )
