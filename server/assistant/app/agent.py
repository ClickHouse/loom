"""Native answer orchestration: host context, bounded adaptive tools and model turns."""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from server.assistant.app import service
from server.assistant.app.tools import Receipt, Tools
from server.assistant.domain import context
from server.core.ch import set_query_tag
from server.core.errors import AssistantUpstreamError
from server.llm import agent as models
from server.observability import cost_accumulator, genai
from server.settings import settings


@dataclass
class State:
    run_id: str = field(default_factory=lambda: "assistant-" + uuid.uuid4().hex)
    receipts: list[Receipt] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    spent: float = 0.0
    turns: int = 0
    estimated: bool = False


def _event(obj: str, state: State, **fields: Any) -> dict[str, Any]:
    return {"object": obj, "id": state.run_id, "choices": [], **fields}


def _size(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    return len(json.dumps([messages, tools], ensure_ascii=False).encode())


def _reserve(state: State, size: int, model: str) -> float:
    if size > settings.assistant_max_context_bytes:
        raise AssistantUpstreamError("the answer exceeded its context bound")
    output = settings.assistant_max_output_tokens
    if state.input_tokens + state.output_tokens + size + output > settings.assistant_max_tokens:
        raise AssistantUpstreamError("the answer exceeded its token budget")
    if not cost_accumulator.is_priced(model):
        raise AssistantUpstreamError("the assistant model has no configured price")
    # UTF-8 bytes bound token count; reserve the highest cache-write multiplier.
    reservation = cost_accumulator.turn_cost_usd(
        model, input_tokens=0, output_tokens=output, cache_creation_1h_tokens=size
    )
    if state.spent + reservation > settings.assistant_answer_ceiling_usd:
        raise AssistantUpstreamError("the answer exhausted its model cost budget")
    return reservation


def _account(
    state: State,
    turn: models.Turn,
    model: str,
    reserve: float,
    generation: Any,
    *,
    span_priced: bool = True,
) -> None:
    if not turn.sent:
        generation.record_accounting(usage_status="not_sent", price_model=model)
        return
    usage = turn.usage
    if usage is None or not turn.usage_complete:
        # A failed in-flight request has unknown spend. Its failed answer row holds
        # the reservation estimate, never invented token counts or a free call.
        state.estimated = True
        state.spent += reserve
        cost_accumulator.report_charge(model, reserve, estimated=True)
        generation.record_accounting(
            usage_status="unknown", price_model=model, estimated_cost=reserve
        )
        return
    generation.record_accounting(
        usage_status="reported", price_model=model, reasoning_tokens=usage.reasoning
    )
    state.input_tokens += usage.total_input
    state.output_tokens += usage.output
    state.spent += cost_accumulator.turn_cost_usd(
        model,
        input_tokens=usage.input,
        output_tokens=usage.output,
        cache_read_tokens=usage.cache_read,
        cache_creation_5m_tokens=usage.cache_write_5m,
        cache_creation_1h_tokens=usage.cache_write_1h,
    )
    cost_accumulator.report_llm(
        model,
        usage.input,
        usage.output,
        cache_read=usage.cache_read,
        cache_write_5m=usage.cache_write_5m,
        cache_write_1h=usage.cache_write_1h,
    )
    generation.record_usage(
        usage.total_input,
        usage.output,
        priced=span_priced,
        cached_input_tokens=usage.cache_read,
        cache_write_5m_tokens=usage.cache_write_5m,
        cache_write_1h_tokens=usage.cache_write_1h,
    )


async def _model(
    state: State,
    messages: list[dict[str, Any]],
    descriptors: list[dict[str, Any]],
    turn: models.Turn,
) -> AsyncGenerator[dict[str, Any]]:
    definition = service.current_definition()
    reserve = _reserve(state, _size(messages, descriptors), definition.price_model)
    state.turns += 1
    started = time.monotonic()
    with genai.generation_attrs(
        assistant_run_id=state.run_id,
        assistant_turn=str(state.turns),
        assistant_price_model=definition.price_model,
    ):
        with genai.generation_span(provider=definition.provider, model=definition.model) as span:
            stream = models.model_for(definition.provider, definition.model).stream(
                messages, descriptors, settings.assistant_max_output_tokens, turn
            )
            try:
                async with contextlib.aclosing(stream):
                    async for text in stream:
                        yield {
                            "object": "chat.completion.chunk",
                            "id": state.run_id,
                            "model": definition.model,
                            "choices": [{"index": 0, "delta": {"content": text}}],
                        }
                turn.check()
            finally:
                if turn.sent:
                    span.note_attempt()
                _account(
                    state,
                    turn,
                    definition.price_model,
                    reserve,
                    span,
                    span_priced=cost_accumulator.is_priced(definition.model),
                )
    if state.spent > settings.assistant_answer_ceiling_usd:
        raise AssistantUpstreamError("the model exceeded its reserved cost")
    if state.input_tokens + state.output_tokens > settings.assistant_max_tokens:
        raise AssistantUpstreamError("the model exceeded its token budget")
    if turn.usage is not None and turn.usage.output > settings.assistant_max_output_tokens:
        raise AssistantUpstreamError("the model exceeded its output token cap")
    yield _event(
        "loom.model",
        state,
        turn=state.turns,
        attempts=1,
        duration_ms=int((time.monotonic() - started) * 1000),
        usage_status="reported",
        reasoning_tokens=turn.usage.reasoning if turn.usage else None,
    )


def _append(
    messages: list[dict[str, Any]], calls: list[models.ToolCall], receipts: list[Receipt]
) -> None:
    messages.append(
        {"role": "assistant", "content": "", "tool_calls": [call.wire() for call in calls]}
    )
    for call, receipt in zip(calls, receipts, strict=True):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(receipt.result, ensure_ascii=False),
            }
        )


async def _call(tools: Tools, op: str, args: dict[str, Any], *, origin: str) -> Receipt:
    # Host context and model tools pass the same operation transport and its spend
    # checks. The answer's admission bit and token are fixed before this work.
    from server.assistant.app.runtime import _afford

    await _afford(tools.org, tools.namespace)
    return await tools.call(op, args, origin=origin)


def _arguments(call: models.ToolCall, offered: set[str], seen: set[str]) -> dict[str, Any]:
    if call.name not in offered:
        raise AssistantUpstreamError("the model requested a tool outside its context")
    if call.id in seen:
        raise AssistantUpstreamError("the model reused a tool call id")
    try:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("not an object")
    except ValueError as e:
        raise AssistantUpstreamError("the model returned malformed tool arguments") from e
    seen.add(call.id)
    return arguments


async def run(
    org: str, namespace: str, token: str, asker: str, history: list[dict[str, str]], state: State
) -> AsyncGenerator[dict[str, Any]]:
    set_query_tag("assistant.native")
    tools = Tools(org, namespace, token, state.run_id)
    state.receipts = tools.receipts
    definition = service.current_definition()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": definition.instructions + "\nCaller context: " + asker},
        *history,
    ]
    plan = context.initial(history[-1]["content"])
    descriptors = tools.descriptors(context.relevant({op for op, _ in plan}))
    offered = {t["function"]["name"] for t in descriptors}
    call_ids = {f"host-context-{index}" for index in range(len(plan))}
    # Host-owned retrieval happens before the first model request. No model must
    # choose to load context, and no secondary model is invoked to make the plan.
    for index, (op, args) in enumerate(plan):
        if state.spent >= settings.assistant_answer_ceiling_usd:
            raise AssistantUpstreamError("the answer exhausted its tool cost budget")
        name = "loom_" + op.replace(".", "_")
        receipt = await _call(tools, name, args, origin="host_context")
        state.spent += receipt.cost_usd
        call = models.ToolCall(f"host-context-{index}", name, json.dumps(args))
        _append(messages, [call], [receipt])
        yield _event(
            "loom.context",
            state,
            operation=op,
            origin=receipt.origin,
            duration_ms=receipt.duration_ms,
        )
    for _ in range(settings.assistant_max_steps):
        turn = models.Turn()
        async with contextlib.aclosing(_model(state, messages, descriptors, turn)) as output:
            async for frame in output:
                yield frame
        if not turn.calls:
            yield _event(
                "chat.completion.chunk",
                state,
                model=definition.model,
                choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            )
            yield _event(
                "chat.completion.chunk",
                state,
                usage={
                    "prompt_tokens": state.input_tokens,
                    "completion_tokens": state.output_tokens,
                    "total_tokens": state.input_tokens + state.output_tokens,
                },
            )
            return
        if turn.text.strip():
            raise AssistantUpstreamError("the model mixed answer text with tool requests")
        if state.turns >= settings.assistant_max_steps:
            raise AssistantUpstreamError("the answer exhausted its model step budget")
        receipts = []
        for call in turn.calls:
            if state.spent >= settings.assistant_answer_ceiling_usd:
                raise AssistantUpstreamError("the answer exhausted its tool cost budget")
            args = _arguments(call, offered, call_ids)
            receipt = await _call(tools, call.name, args, origin="model_tool")
            state.spent += receipt.cost_usd
            receipts.append(receipt)
            yield _event(
                "loom.tool",
                state,
                operation=receipt.op,
                origin=receipt.origin,
                duration_ms=receipt.duration_ms,
            )
        _append(messages, turn.calls, receipts)
    raise AssistantUpstreamError("the answer exhausted its model step budget")
