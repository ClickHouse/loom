"""Answer-local provider reservations enforced by the model HTTP sender."""

from __future__ import annotations

import contextvars
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx

from server.core.errors import AssistantUpstreamError
from server.observability import cost_accumulator


@dataclass
class Send:
    model: str
    reserved: float
    reported: bool = False
    actual: float = 0.0


@dataclass
class Budget:
    allowance: float
    sends: list[Send] = field(default_factory=list)
    reserved: float = 0.0
    refused: bool = False
    unreserved: float = 0.0

    @property
    def cost_usd(self) -> float:
        return self.unreserved + sum(max(sent.reserved, sent.actual) for sent in self.sends)

    def refuse(self) -> None:
        self.refused = True
        raise AssistantUpstreamError("the answer exhausted its tool cost budget")

    def reserve(self, model: str, dollars: float) -> None:
        if (
            self.refused
            or not math.isfinite(self.allowance)
            or not cost_accumulator.is_priced(model)
            or not math.isfinite(dollars)
            or dollars < 0
            or self.reserved + dollars > self.allowance
        ):
            self.refuse()
        # No await between admission and debit: child tasks share this allowance.
        self.reserved += dollars
        sent = Send(model, dollars)
        self.sends.append(sent)
        _last_send.set(sent)

    def observed(self, model: str, dollars: float) -> None:
        sent = _last_send.get()
        if (
            sent is not None
            and any(item is sent for item in self.sends)
            and sent.model == model
            and not sent.reported
        ):
            sent.reported = dollars > 0 or sent.reserved == 0
            sent.actual = dollars if math.isfinite(dollars) else sent.reserved
            self.refused |= not math.isfinite(dollars) or dollars > sent.reserved
            return
        # A charge without a bounded send cannot become free answer work.
        self.refused = True
        if math.isfinite(dollars):
            self.unreserved += max(0.0, dollars)

    def report_unknown(self) -> None:
        # Known charges stay in their operation's ledger. Missing usage is an
        # estimate on the answer ledger, with no fabricated token counts.
        for sent in self.sends:
            if not sent.reported:
                cost_accumulator.report_charge(sent.model, sent.reserved, estimated=True)


_last_send: contextvars.ContextVar[Send | None] = contextvars.ContextVar(
    "loom_provider_send", default=None
)

_current: contextvars.ContextVar[Budget | None] = contextvars.ContextVar(
    "loom_provider_budget", default=None
)


@contextmanager
def limited(allowance: float) -> Iterator[Budget]:
    budget = Budget(allowance)
    token = _current.set(budget)
    send_token = _last_send.set(None)
    try:
        yield budget
        if budget.refused:
            budget.refuse()
    finally:
        _current.reset(token)
        _last_send.reset(send_token)
        budget.report_unknown()


def observed(model: str, dollars: float) -> None:
    budget = _current.get()
    if budget is not None:
        budget.observed(model, dollars)


def _text(value: object) -> bool:
    if isinstance(value, str):
        return True
    return isinstance(value, list) and all(
        isinstance(block, dict)
        and not set(block) - {"type", "text", "cache_control"}
        and block.get("type", "text") == "text"
        and isinstance(block.get("text"), str)
        for block in value
    )


def _completion_input(payload: dict[str, object]) -> bool:
    # These are the fields emitted by the text-only completion clients. External
    # tools and multimodal inputs need a different input and billing bound.
    fields = {
        "model",
        "messages",
        "system",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "reasoning_effort",
        "response_format",
        "output_config",
        "inferenceConfig",
    }
    messages = payload.get("messages")
    return (
        not set(payload) - fields
        and _text(payload.get("system", ""))
        and isinstance(messages, list)
        and all(
            isinstance(message, dict)
            and not set(message) - {"role", "content"}
            and _text(message.get("content"))
            for message in messages
        )
    )


def _price(request: httpx.Request) -> tuple[str, float]:
    payload = json.loads(request.content)
    if not isinstance(payload, dict):
        raise ValueError("provider payload is not an object")
    path = request.url.path.rstrip("/")
    model = payload.get("model")
    if "/model/" in path:
        # Native routing identity is distinct from a first-party model name.
        model = "bedrock:" + unquote(path.split("/model/", 1)[1].rsplit("/", 1)[0])
    if (
        not isinstance(model, str)
        or model.startswith("fake-")
        or not cost_accumulator.is_priced(model)
    ):
        raise ValueError("provider has no price")
    # Serialized UTF-8 bytes include text and protocol framing and bound input
    # tokens. Cache writes use the largest supported multiplier.
    size = len(request.content)
    if (
        path.endswith("/embeddings")
        and cost_accumulator.is_priced_for(model, embedding=True)
        and isinstance(payload.get("input"), (str, list))
    ):
        return model, cost_accumulator.embed_cost_usd(model, size)
    if (
        path.endswith("/invoke")
        and cost_accumulator.is_priced_for(model, embedding=True)
        and isinstance(payload.get("texts"), list)
    ):
        return model, cost_accumulator.embed_cost_usd(model, size)
    if path.endswith(
        ("/chat/completions", "/messages", "/converse")
    ) and cost_accumulator.is_priced_for(model, embedding=False):
        if not _completion_input(payload):
            raise ValueError("provider input has no text-only token bound")
        config = payload.get("inferenceConfig", {})
        maximum = payload.get(
            "max_completion_tokens", payload.get("max_tokens", config.get("maxTokens"))
        )
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("provider output is not bounded")
        return model, cost_accumulator.turn_cost_usd(
            model, input_tokens=0, output_tokens=maximum, cache_creation_1h_tokens=size
        )
    raise ValueError("provider protocol has no bounded price")


def sending(request: httpx.Request) -> None:
    budget = _current.get()
    if budget is None:
        return
    from server.llm import gateway
    from server.settings import settings

    # The gateway token exchange is authentication, not inference.
    if gateway.exchanging() and request.url == httpx.URL(settings.inference_token_url):
        return
    try:
        model, dollars = _price(request)
    except (ValueError, TypeError, AttributeError, httpx.RequestNotRead):
        budget.refuse()
        return
    budget.reserve(model, dollars)
