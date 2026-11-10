"""Wire models of `assistant.ask`: one question to the Loom assistant, answered whole."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# What one request carries: the chat route holds its messages to the same bounds.
MAX_HISTORY = 50  # not a setting: part of the op's wire contract
MAX_MESSAGE_CHARS = 20_000  # not a setting: part of the op's wire contract


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"] = Field(
        ..., description="Who said it: the caller (`user`) or the assistant."
    )
    content: str = Field(
        ...,
        max_length=MAX_MESSAGE_CHARS,
        description="What was said, as text, at most 20,000 characters.",
    )


class AssistantAskInput(BaseModel):
    org: str
    namespace: str
    question: str = Field(
        ...,
        min_length=1,
        max_length=MAX_MESSAGE_CHARS,
        description=(
            "The question about this namespace, answered as the caller, at most 20,000 characters."
        ),
    )
    history: list[AssistantMessage] = Field(
        default_factory=list,
        max_length=MAX_HISTORY,
        description=(
            "Earlier turns of the conversation, oldest first, at most 50; empty by default. "
            "Loom stores none."
        ),
    )


class AssistantCitation(BaseModel):
    """One citation an answer wrote, as Loom checked it when the answer ended."""

    kind: Literal["memory", "doc", "code", "sql"]
    # The citation as the answer wrote it.
    text: str
    # True when the answer's own tool calls read the memory key or ran the statement, or
    # the document holds the section.
    verified: bool = False
    # Why it is not verified ('' when it is).
    reason: str = ""
    # The memory key, document source or code path it names ('' for sql).
    ref: str = ""


class AssistantAskOutput(BaseModel):
    # The assistant's answer, with its citations as the agent wrote them.
    answer: str = ""
    # The answer's id: the run its tool calls are ledgered under (agent_events.run_id).
    run_id: str = ""
    # The model that answered.
    model: str = ""
    # Each citation in `answer`, checked when the answer ended.
    citations: list[AssistantCitation] = Field(default_factory=list)
    # How far the answer rests on what Loom holds: supported, unsupported, unchecked or none
    # (no claim, such as a greeting or an abstention); default unchecked.
    support: str = "unchecked"
