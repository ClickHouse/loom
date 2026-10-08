"""Wire models of `llm.complete`: the main Loom's LLM, served to its read-replicas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LlmCompleteInput(BaseModel):
    system: str
    user: str
    max_tokens: int = Field(default=1024, ge=1, le=200_000)
    # OpenAI's `{"type": "json_object"}`; providers without it ignore the hint.
    response_format: dict[str, str] | None = None
    temperature: float | None = 0.0
    # The model the replica routed to (a stage override such as the rerank
    # model); empty = the main Loom's default LLM.
    model: str = ""
    # The replica's cascade leg (`llm_stage`), so the main Loom's generation
    # reports under the stage that asked for it.
    stage: str = Field(default="", max_length=64)


class LlmCompleteOutput(BaseModel):
    text: str
    tokens_in: int
    tokens_out: int
    provider: str
    model: str
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    stop_reason: str = ""
