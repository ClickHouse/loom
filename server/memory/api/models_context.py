"""Context packing operations.

`context.pack` builds the bounded startup context an agent wants at the
beginning of a session: editable working memory, always-loaded profile,
relevant durable memories, matching procedures, and optional knowledge /
conversation snippets.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from server.core.naming import SENTINEL
from server.memory.api.models_common import _Identity
from server.operations.base import MCP_HIDDEN


class ContextPackInput(_Identity):
    # The user the pack reads for; "-" reads every user in the memories, profile,
    # procedures and runs sections, and the rows filed under no user in the stack and
    # working-memory sections.
    user_id: str = Field(default=SENTINEL)
    # The session the pack reads for; "-" reads every session in the memories section,
    # and the rows filed under no session in the stack and working-memory sections.
    session_id: str = Field(default=SENTINEL)
    query: str = Field(
        default="",
        description="Optional task/question used to select relevant memories and docs.",
    )
    situation: str = Field(
        default="",
        description="Optional situation description used to match procedures.",
    )
    # Exploration rate for the procedures section; None = 0 (never). With
    # probability `rate` the top-matched procedure is withheld and named in
    # `procedure_keys_withheld` instead of `procedure_keys`.
    procedure_explore_rate: float | None = Field(
        default=None, ge=0.0, le=1.0, json_schema_extra=MCP_HIDDEN
    )
    # Size cap of the whole rendered pack in tokens (about 4 characters each);
    # a longer pack is cut and `truncated` is set.
    token_budget: int = Field(default=2000, ge=200, le=12000)
    # How many hits each search-backed section asks for (procedures and the stack's
    # knowledge layer cap it at 5).
    top_k: int = Field(default=5, ge=1, le=20)
    # Same semantics as memory.search.read_scope; binds to this op's user_id /
    # session_id. The token's claim is the ceiling.
    read_scope: Literal["namespace", "own_user", "own_session"] = "namespace"
    # Section budgets and search recipe, with no LLM call in any mode: `startup` (fast
    # recall; with no query it serves pins and recent memories), `task` and `handoff`
    # (balanced), `debug` (quality). The recipe sets the search planes, never LLM steps.
    mode: Literal["startup", "task", "debug", "handoff"] = "task"
    # Add a `knowledge` section of matching document chunks; needs a `query`.
    include_knowledge: bool = False
    # Add a `conversations` section of matching past messages; needs a `query`.
    include_conversations: bool = False
    # The runs manifest — one line per recent session of this
    # identity (date, run status and goal, tool calls by kind with errors,
    # files touched, digest), newest first, `runs_limit` rows. The
    # shortlist an agent reads before it opens a session with
    # memory.evidence or memory.digests. Three indexed reads, no model.
    include_runs: bool = False
    # How many recent sessions the runs manifest lists when `include_runs` is set (1-50).
    runs_limit: int = Field(default=12, ge=1, le=50)
    # Widen the memories search's reranker pool with 20 recent candidates;
    # no effect without a reranker.
    include_chunks: bool = False
    # Per-section token budgets that override the mode's defaults, keyed by
    # section name; a section never gets fewer than 80 tokens.
    section_token_budgets: dict[str, int] = Field(
        default_factory=dict, json_schema_extra=MCP_HIDDEN
    )
    # Optional caller-specified block ordering. Values are section names:
    # working_memory, profile, namespace_health, namespace_guide, mentor, memory_stack,
    # memories, procedures, decisions, knowledge, conversations, runs.
    # Empty keeps Loom's default ordering.
    block_order: list[str] = Field(
        default_factory=list, max_length=16, json_schema_extra=MCP_HIDDEN
    )
    # The caller's own session bookmark (`session:<project>`,
    # the plugin's Stop-hook note). Set, the pack leaves out every OTHER
    # project's `session:*` bookmark, so the agent-scope layer does not serve
    # one repository's goal, tree and last reply to a session in another.
    session_block: str = Field(default="", max_length=80, json_schema_extra=MCP_HIDDEN)
    # Restrict the pack to these sections. Empty = every section the mode allows.
    # A caller that already holds the session primer asks for memories, decisions, procedures.
    only_sections: list[
        Literal[
            "working_memory",
            "memory_stack",
            "profile",
            "memories",
            "procedures",
            "decisions",
            "knowledge",
            "conversations",
            "runs",
            "namespace_health",
            "namespace_guide",
            "mentor",
        ]
    ] = Field(default_factory=list, max_length=12)
    # Decision-typed hits leave the memories block and render as their own
    # tagged <decisions> section carrying the structured record (scenario,
    # outcome, realized outcome). Same search, one more plane on the wire.
    include_decisions: bool = True


class ContextBlock(BaseModel):
    label: str
    section: str
    rendered: str
    tokens_estimate: int
    citations: list[str] = Field(default_factory=list)
    priority: int = 0
    truncated: bool = False
    source_count: int = 0


class ContextPackOutput(BaseModel):
    # The (namespace, memory_key) of each stored row whose item line a section body of the
    # answer still holds, which the op records as served to the agent. Private, never on
    # the wire.
    _served_rows: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    # The pack as one text: each non-empty section in a `<name>` tag, in block order,
    # cut to `token_budget`.
    rendered: str
    # Each section's body by name, in block order; sections that rendered nothing are absent.
    sections: dict[str, str]
    # One entry per non-empty section: label, body, token estimate, citations,
    # source count, and `priority` (its position in block order).
    blocks: list[ContextBlock] = Field(default_factory=list)
    # Per-section details by name: source count, citations, whether it was cut, its
    # size, its token budget and fill ratio (above 1.0 means it was cut).
    section_meta: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # How many sections rendered something.
    section_count: int
    # True when any section, or the whole pack, was cut to its budget.
    truncated: bool = False
    # Echo of the input `token_budget`.
    token_budget: int
    # Echo of the input `mode`.
    mode: str = "task"
    # Budget pressure. `tokens_estimate` is the rendered
    # pack's size on the same estimator the blocks use; `fill_ratio` is that
    # over `token_budget`. A harness that watches the ratio climb across
    # sessions knows the always-loaded sections are due a rewrite before the
    # pack starts truncating: a size trigger exposed as a number instead of
    # acted on server-side.
    tokens_estimate: int = 0
    # `tokens_estimate / token_budget`, to three places.
    fill_ratio: float = 0.0
    # The leading run of `rendered` that does not depend on `query` or
    # `situation` — the same bytes for every question this identity asks
    # until its working memory, profile or pins change. A consumer places
    # its provider cache breakpoint after `stable_prefix_chars`; the hash
    # tells it whether the prefix moved without diffing.
    stable_sections: list[str] = Field(default_factory=list)
    # Length in characters of that prefix of `rendered`; 0 when the first section
    # depends on the query.
    stable_prefix_chars: int = 0
    # SHA-256 of the stable prefix; empty when `stable_prefix_chars` is 0.
    stable_hash: str = ""
    # Procedures the pack put in front of the agent: the
    # "shown" side of the ledger a utility needs. Rides agent_events as
    # out_procedure_keys so a judged session outcome can find what it saw.
    procedure_keys: list[str] = Field(default_factory=list)
    # Procedures the pack matched and deliberately withheld:
    # the "not shown" side of the same ledger. Rides agent_events as
    # out_procedure_keys_withheld.
    procedure_keys_withheld: list[str] = Field(default_factory=list)
