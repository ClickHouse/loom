"""The per-ask context blocks the synthesizer reads beside the window: knowledge
passages and the caller's recent turns."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


_KNOWLEDGE_MAX_PASSAGE_CHARS = 800
_KNOWLEDGE_MAX_BLOCK_CHARS = 6000


def knowledge_block_text(hits: list[Any]) -> str:
    """Render knowledge.search hits as one labelled block,
    each passage bounded, the block bounded (lowest-ranked dropped first).
    Empty when there are no hits with content. Pure."""
    lines: list[str] = []
    for h in hits:
        content = str(getattr(h, "content", "") or "").strip()
        if not content:
            continue
        if len(content) > _KNOWLEDGE_MAX_PASSAGE_CHARS:
            content = content[: _KNOWLEDGE_MAX_PASSAGE_CHARS - 12].rstrip() + " …[truncated]"
        src = str(getattr(h, "source_uri", "") or "document")
        idx = getattr(h, "chunk_idx", 0)
        title = str(getattr(h, "title", "") or getattr(h, "section", "") or "").strip()
        head = f"[knowledge:{src}#{idx}]" + (f" ({title})" if title else "")
        lines.append(f"{head} {content}")
    while lines and sum(len(x) + 1 for x in lines) > _KNOWLEDGE_MAX_BLOCK_CHARS:
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
        from server.settings import settings as _ks

        res = await handle_knowledge_search(
            KnowledgeSearchInput(
                org=search_input.org,
                namespace=search_input.namespace,
                query=question,
                top_k=int(_ks.memory_ask_knowledge_top_k),
                namespace_scope="this",
                # The ask's hard tag filter reaches this leg too.
                tags=(
                    []
                    if bool(getattr(search_input, "tags_soft", False))
                    else [str(t) for t in (getattr(search_input, "tags", None) or []) if t]
                ),
            )
        )
        hits = list(getattr(res, "results", None) or [])
    except Exception as e:  # noqa: BLE001 — additive plane
        logger.warning("knowledge plane on ask failed (non-fatal): %s", e)
        return "", 0
    block = knowledge_block_text(hits)
    return block, (len(hits) if block else 0)


_RECENT_MAX_TURNS = 40
_RECENT_MAX_TURN_CHARS = 1500
_RECENT_MAX_BLOCK_CHARS = 12000


def recent_turns_block(messages: list[dict[str, Any]]) -> str:
    """Render the caller's recent turns as one block, newest
    last, bounded per turn and in total (the oldest turns are dropped first).
    Empty when there are no turns with content. Pure."""
    turns: list[str] = []
    for m in messages[-_RECENT_MAX_TURNS:]:
        content = str((m or {}).get("content") or "").strip()
        if not content:
            continue
        if len(content) > _RECENT_MAX_TURN_CHARS:
            content = content[: _RECENT_MAX_TURN_CHARS - 12].rstrip() + " …[truncated]"
        role = str(m.get("role") or "user").strip() or "user"
        name = str(m.get("name") or "").strip()
        ts = str(m.get("timestamp") or "").strip()
        who = f"{role} {name}".strip()
        head = f"[{who} @ {ts}]" if ts else f"[{who}]"
        turns.append(f"{head} {content}")
    while turns and sum(len(t) + 1 for t in turns) > _RECENT_MAX_BLOCK_CHARS:
        turns.pop(0)
    if not turns:
        return ""
    head = "RECENT TURNS (the conversation this question is part of; not memories):"
    return head + "\n" + "\n".join(turns)
