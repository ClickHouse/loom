"""`llm.complete`: a read-replica's LLM calls, answered by the main Loom's client."""

from __future__ import annotations

from typing import Any

from server.insights.api.models_llm import LlmCompleteInput, LlmCompleteOutput
from server.insights.app.embedding_serve import require_replica_caller
from server.llm.client import get_llm_client_for_model
from server.observability.genai import llm_stage


async def handle_llm_complete(input: LlmCompleteInput) -> LlmCompleteOutput:
    require_replica_caller("llm.complete")
    client = get_llm_client_for_model(input.model.strip() or None)
    # The main Loom's provider client records the generation (cost, tokens)
    # under the replica's stage.
    # Every client takes `temperature`; the protocol leaves it to the extras
    # the wrappers (fallback, response cache) pass through.
    extra: dict[str, Any] = {"temperature": input.temperature}
    with llm_stage(input.stage or "other"):
        out = await client.complete(
            input.system,
            input.user,
            max_tokens=input.max_tokens,
            response_format=input.response_format,
            **extra,
        )
    return LlmCompleteOutput(
        text=out.text,
        tokens_in=out.tokens_in,
        tokens_out=out.tokens_out,
        provider=out.provider,
        model=out.model,
        cached_input_tokens=out.cached_input_tokens,
        cache_write_input_tokens=out.cache_write_input_tokens,
        stop_reason=out.stop_reason,
    )
