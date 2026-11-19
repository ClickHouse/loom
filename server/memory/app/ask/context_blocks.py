"""The per-ask context blocks the synthesizer reads beside the window: knowledge
passages and the caller's recent turns."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from server.core.errors import VectorCapacityError
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.app.source_passages import CheckedPassages, collect
from server.memory.app.ask.source_context import PreparedKnowledge, prepare_knowledge
from server.observability.genai import current_stage, llm_stage
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


@dataclass
class AskKnowledge:
    passages: CheckedPassages | None = None

    @property
    def mirrored(self) -> bool:
        return self.passages is not None and self.passages.mirrored

    def fresh(self) -> bool:
        return self.passages is None or self.passages.fresh()

    @staticmethod
    def _render(hits: list[Any]) -> tuple[str, int]:
        block = knowledge_block_text(hits)
        return block, len(hits) if block else 0

    async def current(self) -> tuple[str, int]:
        if self.passages is None:
            return "", 0
        hits = await self.passages.current()
        if not self.passages.fresh():
            hits = await self.passages.ordinary()
        return self._render(hits)

    async def require(self) -> None:
        if self.passages is not None:
            await self.passages.current()
            if self.passages.lost or not self.passages.fresh():
                raise LostCorpus()

    async def ordinary(self) -> tuple[str, int]:
        if self.passages is None:
            return "", 0
        return self._render(await self.passages.ordinary())


@dataclass(frozen=True)
class CheckedAskLLM:
    client: Any
    knowledge: AskKnowledge

    @property
    def model(self) -> str:
        return str(getattr(self.client, "model", "") or "")

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        await self.knowledge.require()
        # The synthesis tier owns the stage; access renewal must not rename it.
        with llm_stage(current_stage()):
            result = await self.client.complete(*args, **kwargs)
        await self.knowledge.require()
        return result


async def knowledge_passages(
    search_input: Any,
    question: str,
    *,
    enabled: bool | None = None,
    prepared: PreparedKnowledge | None = None,
) -> AskKnowledge:
    """Retain the selected search proofs and both complete ordinary widths."""
    if enabled is False:
        return AskKnowledge()
    try:
        if prepared is None:
            prepared = await prepare_knowledge(search_input, question, enabled=enabled)
        if prepared.request is None:
            return AskKnowledge()
        passages = await collect(
            prepared.request, prepared.plan, str(getattr(search_input, "agent", "") or "")
        )
        return AskKnowledge(passages)
    except LostCorpus:
        raise
    except VectorCapacityError:
        raise
    except Exception:  # noqa: BLE001 — an unavailable additive plane contributes no passages
        logger.warning("knowledge plane on ask unavailable")
        return AskKnowledge()


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
