"""memory.demonstrate — the nearest labelled examples, a vote, and a few-shot
block. Examples are ordinary memories tagged
`<example_tag>` plus `<label_prefix><label>`; nothing new is stored."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MemoryDemonstrateInput(BaseModel):
    org: str
    namespace: str
    agent: str = "*"
    # The input to classify; the example memories nearest to it are retrieved.
    query: str = Field(min_length=1, max_length=4000)
    # At most this many labelled examples feed the vote and the block, 1-50.
    k: int = Field(default=8, ge=1, le=50)
    # Only memories carrying this tag are examples.
    example_tag: str = Field(default="example", min_length=1, max_length=64)
    # The label rides as a second tag: "<label_prefix><label>".
    label_prefix: str = Field(default="label:", min_length=1, max_length=32)
    # `this` searches `namespace`; `all_mine` every namespace the caller belongs to,
    # but labels are read from `namespace` only.
    namespace_scope: Literal["this", "all_mine"] = "this"
    # Below this normalised top-vote margin the prediction is reported but
    # `abstain` is set, so a caller can fall back to its own reasoning.
    min_margin: float = Field(default=0.0, ge=0.0, le=1.0)
    # Character budget of `few_shot_block`; the nearest example is always included.
    max_block_chars: int = Field(default=6000, ge=200, le=60000)


class DemonstrationRow(BaseModel):
    memory_key: str
    text: str
    label: str
    similarity: float


class MemoryDemonstrateOutput(BaseModel):
    # The query, echoed.
    query: str
    # The labelled examples used, nearest first, at most `k`.
    demonstrations: list[DemonstrationRow]
    # label -> normalised similarity-weighted vote (sums to 1 when any)
    votes: dict[str, float] = Field(default_factory=dict)
    # The label with the largest vote; "" when no labelled example was found.
    predicted_label: str = ""
    # The top vote minus the runner-up (votes sum to 1); 1.0 when only one label voted.
    margin: float = 0.0
    # True when no labelled example was found or `margin` is below `min_margin`; the
    # prediction is still returned.
    abstain: bool = False
    # `no_examples`, `no_labelled_examples` or `low_margin`; "" when not abstaining.
    abstain_reason: str = ""
    # The examples as `Input:` / `Label:` pairs for a prompt, nearest first, within
    # `max_block_chars`.
    few_shot_block: str = ""
    # How many nearest memories were examined and how many carried a label.
    examined: int = 0
    # How many of them carried a label (the demonstrations used, at most `k`).
    labelled: int = 0
