"""The state a memory.set_from_messages pipeline threads through its steps,
and the step types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class WorkflowState:
    """Shared state threaded through pipeline steps.

    Steps mutate fields in place. Fields populated upstream are
    available to downstream steps; fields a step doesn't need can
    be ignored.
    """

    # Identity + caller config (set by handler before pipeline runs)
    org: str = ""
    namespace: str = ""
    agent: str = ""
    user_id: str = "-"
    session_id: str = "-"
    run_id: str = "-"

    # Input
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_messages: list[dict[str, Any]] = field(default_factory=list)
    observation_date: str | None = None
    extraction_instructions: str = ""
    # The operator's extraction policy for (org, namespace), read by the handler
    # (`extraction_policy.effective`), never from the request; "" when none is set.
    operator_policy: str = ""
    max_tokens: int = 4096
    extra_tags: list[str] = field(default_factory=list)
    # Per-call fallback for bi-temporal `valid_at`. Applied to every
    # emitted fact that the extractor didn't anchor itself. None =
    # leave the row's valid_at NULL.
    valid_at: datetime | None = None

    # Populated by fetch_existing_memories step
    existing_memories: list[dict[str, str]] = field(default_factory=list)

    # Populated by fetch_namespace_categories step. Each entry:
    # {name, description, summary}. The extractor sees these so emitted
    # tags anchor to the registered taxonomy instead of inventing synonyms.
    namespace_categories: list[dict[str, str]] = field(default_factory=list)

    # Populated by llm_extract step
    extracted_facts: list[dict[str, Any]] = field(default_factory=list)
    # Per-call override of the coverage sweep.
    coverage_sweep: bool | None = None
    # Per-call pin of the extractor's response cache
    # (None = the class policy, else off).
    llm_cache: bool | None = None
    # Whether assistant-turn learnings are on for this call: the deployment setting,
    # read once by the op, unless the request turned them off.
    agent_learnings: bool = False
    # The agent a filed rule is for (`agent_rules.request_rule_agent`):
    # a pinned token's signed agent, else the body's; '' files nothing.
    rule_agent: str = ""
    # Rules that wrote a new candidate or this session's support of one.
    learning_candidates: int = 0
    segments: int = 1
    # Facts written as time-bounded foresight.
    foresight: int = 0
    # Memories retired because the user asked to forget them.
    forgotten: int = 0
    forgotten_keys: list[str] = field(default_factory=list)
    # Requests to forget read from the user's own sentences and resolved by
    # content (a window's latest MAX_FORGET_REQUESTS).
    forget_requests: int = 0
    # Rows written with `about:other` (read from the sentence's subject).
    about_other: int = 0
    # The user's facts read from the assistant's premises.
    premise_facts: int = 0
    # Which planner produced the segments: "whole" (one
    # segment) or "size" (fixed windows).
    segmenter: str = "whole"
    tokens_in: int = 0
    tokens_out: int = 0
    # Subset of tokens_in the provider served from its prompt cache:
    # the static extraction prompt, when a segment's call
    # found it already warm.
    cached_tokens_in: int = 0
    llm_provider: str = ""
    llm_model: str = ""
    # The extractor prompts' cache keys (one per segment, no
    # text) and the response cache's hit / miss counts over the extraction.
    extract_prompt_keys: list[str] = field(default_factory=list)
    llm_cache_hits: int = 0
    llm_cache_misses: int = 0

    # Populated by bulk_write step
    written: list[dict[str, Any]] = field(default_factory=list)
    # The entries bulk_set refused: {memory_key, verdict} each.
    refused: list[dict[str, str]] = field(default_factory=list)

    # Pipeline metadata
    # Wall time per step (`step.<step_id>`) and, carried up from the bulk
    # write, per bulk_set phase (`bulk.<phase>`).
    stage_ms: dict[str, float] = field(default_factory=dict)
    skipped_dedup: int = 0
    skipped_recall_echo: int = 0
    skipped_activity: int = 0
    skipped_non_durable: int = 0
    skipped_unverified: int = 0
    skipped_session_echo: int = 0
    # Keys the session's Loom tools served (from the caller).
    recalled_keys: list[str] = field(default_factory=list)
    # Text the session's own Loom tool calls carried.
    session_values: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Drop accounting (see memory/app/drops.py for the durable ledger):
    # fact objects the parser discarded, facts the judge graded WRONG, and
    # extracted keys that collided after sanitisation (disambiguated, not
    # dropped — but counted, since the caller's key is not what was asked).
    dropped_parse: int = 0
    dropped_judge_wrong: int = 0
    key_collisions: int = 0


# A step is an async callable: (state, config) -> state (mutated in place)
StepCallable = Callable[
    [WorkflowState, dict[str, Any]],
    Awaitable[WorkflowState],
]


@dataclass
class PipelineStep:
    """One step in a workflow. `step_id` resolves to a callable via the
    registry; `config` is passed through to the step."""

    step_id: str
    config: dict[str, Any] = field(default_factory=dict)
