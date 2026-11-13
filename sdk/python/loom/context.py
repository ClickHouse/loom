"""Context-pack client.

`context.pack` is the one-call task-context loader: working memory,
memory stack, profile, relevant durable memories, matching procedures,
and optional knowledge / conversation snippets — assembled server-side
into one token-budgeted block. The MCP surface exposes it in the `core`
and `coding` toolsets (`loom_context_pack`) and tells agents to call it
first in each task; this client is the same op for Python callers.

Use it when you want Loom to decide the context mix. When you want
granular control (e.g. profile cached once per conversation, memories
re-searched per turn), compose `profile.system_prompt()` +
`memory.search()` yourself — see `examples/claude_agent_loop.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


@dataclass
class ContextPack:
    """The assembled context. `str(pack)` (or `.rendered`) is the string
    to drop into the LLM call; the rest is per-section introspection."""

    rendered: str
    sections: dict[str, str] = field(default_factory=dict)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    section_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    section_count: int = 0
    truncated: bool = False
    token_budget: int = 0
    mode: str = "task"
    # Budget pressure: rendered size on the server's estimator and its share
    # of token_budget. Watch it climb across sessions to schedule a rewrite
    # of the always-loaded sections before the pack starts truncating.
    tokens_estimate: int = 0
    fill_ratio: float = 0.0
    # Leading run of `rendered` that is the same for every query this
    # identity asks (working memory, profile, unseeded stack, startup pins).
    # Split the prompt there for a provider cache breakpoint:
    # `pack.rendered[:pack.stable_prefix_chars]` is the cacheable part.
    stable_sections: list[str] = field(default_factory=list)
    stable_prefix_chars: int = 0
    stable_hash: str = ""

    def __str__(self) -> str:
        return self.rendered

    @property
    def stable_prefix(self) -> str:
        return self.rendered[: self.stable_prefix_chars]

    @property
    def volatile_suffix(self) -> str:
        return self.rendered[self.stable_prefix_chars :]


@dataclass
class OffloadedBlob:
    """What `context.offload` handed back. `pointer` is the line to keep in
    the transcript in place of the content."""

    blob_key: str
    content_bytes: int
    content_chars: int
    sha256: str
    summary: str
    expires_at: str
    pointer: str

    def __str__(self) -> str:
        return self.pointer


@dataclass
class FetchedSlice:
    """One `context.fetch` answer: a character slice, or the lines matching
    a pattern. `next_offset` is -1 when exhausted; `found` is False when
    the blob expired or never existed."""

    found: bool
    blob_key: str
    content: str = ""
    content_type: str = ""
    summary: str = ""
    total_chars: int = 0
    total_lines: int = 0
    offset: int = 0
    next_offset: int = -1
    matched_lines: int = 0

    def __str__(self) -> str:
        return self.content


class ContextClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def offload(
        self,
        content: str,
        *,
        blob_key: str = "",
        summary: str = "",
        content_type: str = "text/plain",
        ttl_seconds: int = 7 * 86_400,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> OffloadedBlob:
        """Park a large tool result outside the model context.

        Returns the pointer line (`str(result)`) to keep in the transcript
        instead of the content. Empty `blob_key` makes the key
        content-addressed, so the same output parked twice is one row.
        `summary` is a caller one-liner (the tool and what it returned);
        the server does not call a model for it.
        """
        payload: dict[str, Any] = dict(
            self._agent.memory._identity(
                user_id,
                session_id,
                run_id=run_id,
                agent_id=agent_id,
            )
        )
        payload.update(
            {
                "content": content,
                "blob_key": blob_key,
                "summary": summary,
                "content_type": content_type,
                "ttl_seconds": ttl_seconds,
            }
        )
        r = await self._agent.transport.post("/v1/context.offload", payload)
        return OffloadedBlob(
            blob_key=str(r.get("blob_key", "")),
            content_bytes=int(r.get("content_bytes", 0)),
            content_chars=int(r.get("content_chars", 0)),
            sha256=str(r.get("sha256", "")),
            summary=str(r.get("summary", "")),
            expires_at=str(r.get("expires_at", "")),
            pointer=str(r.get("pointer", "")),
        )

    async def fetch(
        self,
        blob_key: str,
        *,
        offset: int = 0,
        limit: int = 4000,
        pattern: str = "",
        context_lines: int = 0,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> FetchedSlice:
        """Read a parked result back: a character slice (`offset`/`limit`),
        or with `pattern` the matching lines as `lineno: text` with
        `context_lines` around each — `offset` then counts matches. Page
        with `next_offset` until it is -1."""
        payload: dict[str, Any] = dict(
            self._agent.memory._identity(
                user_id,
                session_id,
                run_id=run_id,
                agent_id=agent_id,
            )
        )
        payload.update(
            {
                "blob_key": blob_key,
                "offset": offset,
                "limit": limit,
                "pattern": pattern,
                "context_lines": context_lines,
            }
        )
        r = await self._agent.transport.post("/v1/context.fetch", payload)
        return FetchedSlice(
            found=bool(r.get("found")),
            blob_key=str(r.get("blob_key", blob_key)),
            content=str(r.get("content", "")),
            content_type=str(r.get("content_type", "")),
            summary=str(r.get("summary", "")),
            total_chars=int(r.get("total_chars", 0)),
            total_lines=int(r.get("total_lines", 0)),
            offset=int(r.get("offset", 0)),
            next_offset=int(r.get("next_offset", -1)),
            matched_lines=int(r.get("matched_lines", 0)),
        )

    async def pack(
        self,
        query: str = "",
        *,
        situation: str = "",
        token_budget: int = 2000,
        top_k: int = 5,
        mode: str = "task",
        include_knowledge: bool = False,
        include_conversations: bool = False,
        include_chunks: bool = False,
        section_token_budgets: dict[str, int] | None = None,
        block_order: list[str] | None = None,
        only_sections: list[str] | None = None,
        include_decisions: bool = True,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> ContextPack:
        """Build the bounded context an agent wants at a session/task start.

        Args:
          query: task/question used to select relevant memories and docs.
          situation: free-text situation used to match procedures.
          token_budget: overall budget — the server REJECTS values outside
            200-12000 (422), it does not clamp. Sections get per-mode
            shares, overridable via `section_token_budgets` (keys:
            working_memory, memory_stack, profile, memories, procedures,
            decisions, knowledge, conversations). `top_k` is likewise bounded 1-20.
          mode: "startup" | "task" | "debug" | "handoff" — shifts the
            per-section budget mix (e.g. the handoff mode, a session passed to
            another agent, weights working memory and conversations).
          include_knowledge / include_conversations / include_chunks:
            opt-in sections beyond the always-on core.
          block_order: override section placement; unlisted sections keep
            Loom's default order (working memory first).
          only_sections: restrict the pack to these sections — a per-prompt
            caller that already holds the session primer asks for
            ["memories", "decisions", "procedures"] and pays for nothing else.
          include_decisions: decision-typed hits render as their own
            <decisions> block with scenario / outcome / realized outcome
            instead of riding the memories block (default True).

        Sections are assembled best-effort server-side — a failing section
        degrades to empty rather than failing the pack.
        """
        payload: dict[str, Any] = self._agent.memory._identity(
            user_id, session_id, run_id=run_id, agent_id=agent_id
        )
        payload.update(
            {
                "query": query,
                "situation": situation,
                "token_budget": token_budget,
                "top_k": top_k,
                "mode": mode,
                "include_knowledge": include_knowledge,
                "include_conversations": include_conversations,
                "include_chunks": include_chunks,
                "include_decisions": include_decisions,
            }
        )
        if only_sections:
            payload["only_sections"] = list(only_sections)
        if section_token_budgets:
            payload["section_token_budgets"] = dict(section_token_budgets)
        if block_order:
            payload["block_order"] = list(block_order)
        result = await self._agent.transport.post("/v1/context.pack", payload)
        return ContextPack(
            rendered=str(result.get("rendered") or ""),
            sections=dict(result.get("sections") or {}),
            blocks=list(result.get("blocks") or []),
            section_meta=dict(result.get("section_meta") or {}),
            section_count=int(result.get("section_count") or 0),
            truncated=bool(result.get("truncated") or False),
            token_budget=int(result.get("token_budget") or 0),
            mode=str(result.get("mode") or "task"),
            tokens_estimate=int(result.get("tokens_estimate") or 0),
            fill_ratio=float(result.get("fill_ratio") or 0.0),
            stable_sections=list(result.get("stable_sections") or []),
            stable_prefix_chars=int(result.get("stable_prefix_chars") or 0),
            stable_hash=str(result.get("stable_hash") or ""),
        )
