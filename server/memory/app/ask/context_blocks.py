"""The per-ask context blocks the synthesizer reads beside the window: knowledge
passages and the caller's recent turns."""

from __future__ import annotations

import logging
from typing import Any

from server.memory.app.agent_rules import request_rule_agent, served_hits
from server.settings import settings

logger = logging.getLogger(__name__)


def knowledge_block_text(hits: list[Any]) -> str:
    """Render knowledge.search hits as one labelled block, each passage
    bounded, the block bounded (lowest-ranked dropped first). Empty when there
    are no hits with content."""
    passage_max = settings.memory_ask_knowledge_passage_max_chars
    block_max = settings.memory_ask_knowledge_block_max_chars
    lines: list[str] = []
    for h in hits:
        content = str(getattr(h, "content", "") or "").strip()
        if not content:
            continue
        if len(content) > passage_max:
            content = content[: passage_max - 12].rstrip() + " …[truncated]"
        src = str(getattr(h, "source_uri", "") or "document")
        idx = getattr(h, "chunk_idx", 0)
        title = str(getattr(h, "title", "") or getattr(h, "section", "") or "").strip()
        head = f"[knowledge:{src}#{idx}]" + (f" ({title})" if title else "")
        lines.append(f"{head} {content}")
    while lines and sum(len(x) + 1 for x in lines) > block_max:
        lines.pop()
    if not lines:
        return ""
    return "KNOWLEDGE PASSAGES (documents this namespace holds; not memories):\n" + "\n".join(lines)


async def knowledge_passages(
    search_input: Any, question: str, *, enabled: bool | None = None
) -> tuple[str, int]:
    """One knowledge.search over the ask's namespace, rendered
    for the synthesizer. On unless the request turns it off (`knowledge`);
    fail-open (a plane that errors is a plane that returned nothing)."""
    if enabled is not None and not enabled:
        return "", 0
    try:
        from server.knowledge.api.models_knowledge import KnowledgeSearchInput
        from server.knowledge.app.knowledge_search import handle_knowledge_search

        async def search(top_k: int) -> list[Any]:
            res = await handle_knowledge_search(
                KnowledgeSearchInput(
                    org=search_input.org,
                    namespace=search_input.namespace,
                    query=question,
                    top_k=top_k,
                    namespace_scope="this",
                    # The ask's hard tag filter reaches this leg too.
                    tags=(
                        []
                        if bool(getattr(search_input, "tags_soft", False))
                        else [str(t) for t in (getattr(search_input, "tags", None) or []) if t]
                    ),
                    # The ask's synthesis reads these passages; a rerank call would run
                    # before its cost cap and synthesis switch are checked.
                    rerank="none",
                )
            )
            return list(getattr(res, "results", None) or [])

        top_k = int(settings.memory_ask_knowledge_top_k)
        # Another bot's promoted rule is not a rule about this agent.
        hits = await served_hits(
            await search(top_k),
            search,
            lambda h: list(getattr(h, "tags", None) or []),
            request_rule_agent(str(getattr(search_input, "agent", "") or "")),
            top_k,
        )
    except Exception as e:  # noqa: BLE001 — additive plane
        logger.warning("knowledge plane on ask failed (non-fatal): %s", e)
        return "", 0
    block = knowledge_block_text(hits)
    return block, (len(hits) if block else 0)


def recent_turns_block(messages: list[dict[str, Any]]) -> str:
    """Render the caller's recent turns as one block, newest last, bounded
    per turn and in total (the oldest turns are dropped first). Empty when
    there are no turns with content."""
    turn_max = settings.memory_ask_recent_turn_max_chars
    block_max = settings.memory_ask_recent_block_max_chars
    turns: list[str] = []
    for m in messages[-settings.memory_ask_recent_max_turns :]:
        content = str((m or {}).get("content") or "").strip()
        if not content:
            continue
        if len(content) > turn_max:
            content = content[: turn_max - 12].rstrip() + " …[truncated]"
        role = str(m.get("role") or "user").strip() or "user"
        name = str(m.get("name") or "").strip()
        ts = str(m.get("timestamp") or "").strip()
        who = f"{role} {name}".strip()
        head = f"[{who} @ {ts}]" if ts else f"[{who}]"
        turns.append(f"{head} {content}")
    while turns and sum(len(t) + 1 for t in turns) > block_max:
        turns.pop(0)
    if not turns:
        return ""
    head = "RECENT TURNS (the conversation this question is part of; not memories):"
    return head + "\n" + "\n".join(turns)
