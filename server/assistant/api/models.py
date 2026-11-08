"""Wire models of `assistant.ask`: one question to the Loom assistant, answered whole."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=20_000)


class AssistantAskInput(BaseModel):
    org: str
    namespace: str
    question: str = Field(
        ...,
        min_length=1,
        max_length=20_000,
        description="The question about this namespace, answered as the caller.",
    )
    history: list[AssistantMessage] = Field(
        default_factory=list,
        max_length=50,
        description="Earlier turns of the conversation, oldest first; Loom stores none.",
    )


class AssistantAskOutput(BaseModel):
    # The assistant's answer, with its citations as the agent wrote them.
    answer: str = ""
    # The answer's id: the run its tool calls are ledgered under (agent_events.run_id).
    run_id: str = ""
    # The model that answered.
    model: str = ""
