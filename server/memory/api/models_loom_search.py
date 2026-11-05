"""search.query — one question over memory, profile, knowledge, conversations and,
for a defect report under a code scope, the tracker record.

The same fan-out the MCP `loom_search` tool answers with (the op's tool name
is `loom_search`; MCP serves it through a synthetic descriptor that keeps its
hand-written schema and renders a text summary and a lean result). Over HTTP
and the SDKs the op returns the assembled sections as they are.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer


class SearchInput(BaseModel):
    """Search every plane of the caller's Loom content in one call.

    `namespace` is the namespace the search addresses (and is authorized
    against); `namespace_scope="all_mine"` also reads the caller's own
    memory-kind namespaces, see
    docs/concepts/identities-and-namespaces.md#reading-several-namespaces. `page` is a
    `memory.next_page` handle from an earlier answer."""

    org: str
    namespace: str
    agent: str = "-"
    # The question, searched in every plane at once.
    query: str = Field(min_length=1)
    # At most this many hits per section (1-100); the tracker leg caps it at 10.
    top_k: int = Field(default=5, ge=1, le=100)
    # Tag filter for the memory and knowledge legs; profile and conversations ignore it.
    tags: list[str] = Field(default_factory=list)
    # `this` searches `namespace` only; `all_mine` (the default) also reads the
    # caller's own memory-kind namespaces.
    namespace_scope: Literal["this", "all_mine"] = "all_mine"
    # Named `memory.search` preset for the memory leg (`fast`, `balanced`, `quality`, ...);
    # empty = the server's defaults.
    recipe: Literal["", "fast", "balanced", "quality", "verbatim", "code", "temporal", "graph"] = ""
    # What each memory hit carries: `excerpt` (the first 200 characters; a decision or a
    # constraint as stored), `full` (the stored clip), `none` (keys and scores only) or
    # `unit` (whole values within a budget).
    bodies: Literal["full", "excerpt", "none", "unit"] = "excerpt"
    # Every section unwithheld, and the ranking terms of each memory hit.
    explain: bool = False
    # A `memory.next_page` handle from an earlier answer, to read the memory leg's next page;
    # empty = the first page.
    page: str = Field(default="", max_length=120)


class SearchOutput(BaseModel):
    """The sections, each `{"results": [...], ...}` as its plane's search
    returns it, or `{"error": ..., "results": []}` when that leg failed.
    `tracker` (the duplicate-check verdict) leads when the question was a
    defect report; `withheld` counts what the answer-first rules held back
    and why."""

    @model_serializer(mode="wrap")
    def _drop_absent_sections(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in ("tracker", "withheld"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    # The duplicate-check verdict for a defect-shaped query under a code scope;
    # absent otherwise, or when that leg failed or timed out.
    tracker: dict[str, Any] | None = None
    # The `memory.search` result plus the scoped repository's decisions, rendered as a
    # reader block, without the hits the calibration flagged below its threshold
    # (unless every hit is flagged).
    memory: dict[str, Any] = Field(default_factory=dict)
    # The `knowledge.search` result plus the scoped repository's documents, hits below
    # the knowledge similarity floor dropped.
    knowledge: dict[str, Any] = Field(default_factory=dict)
    # Profile sections whose label or text matches the query words, then by meaning.
    profile: dict[str, Any] = Field(default_factory=dict)
    # The `conversation.search` hits under `results`.
    conversations: dict[str, Any] = Field(default_factory=dict)
    # Counts of hits held back per section by the answer-first rules, with `reason`;
    # absent when nothing was withheld or `explain` is set.
    withheld: dict[str, Any] | None = None
    # The caller's namespaces no leg read because the fan-out stops at
    # `memory_search_fanout_max_namespaces`; address one by name to search it.
    namespaces_skipped: list[str] = Field(default_factory=list)
