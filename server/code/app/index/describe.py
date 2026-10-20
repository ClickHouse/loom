"""Chunk descriptions: the prompt and file card a chunk is described with, its memo
key, and the bounded fan-out over a file's new chunks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from server.code.app.index.ingest_stats import FileIngestStats
from server.code.domain.extract.chunker import Chunk
from server.llm import customer
from server.observability.genai import staged

logger = logging.getLogger("loom.code_index.ingest")


# A chunk is described from at most this many characters of its content. A
# longer one is described from its head (signature, leading comments, the
# opening of the body): the long function is often the one carrying the
# mechanism a conceptual question asks about, so it is not left undescribed.
_DESCRIBE_MAX_CHARS = 4000
_DESCRIBE_KINDS = {"function", "class_skeleton"}

_DESCRIBE_SYSTEM = (
    "You summarize source code for a retrieval index. Reply with ONE "
    "sentence (max 25 words) describing what the code does — plain, "
    "specific, no preamble, no code fences."
)

# The question-oriented description — the file card places the chunk in the file's role;
# the second sentence names the developer questions the chunk answers, in the
# developer's words.
_DESCRIBE_QUESTIONS_SYSTEM = (
    "You annotate source code for a retrieval index used by developers asking "
    "natural-language questions. Given a file's orientation card and one code "
    "chunk, reply with exactly two sentences, plain prose, no code fences: (1) "
    "what this chunk does, naming the mechanism it implements in the domain's "
    "words; (2) 'Answers:' followed by the one or two developer questions this "
    "chunk is the answer to, phrased as a developer would ask them (how does the "
    "server …, where is … enforced/decided). Do not describe what the file in "
    "general does; describe this chunk. Max 70 words."
)


def describe_system_prompt() -> str:
    """The description prompt the deployment asked for."""
    from server.settings import settings

    return (
        _DESCRIBE_QUESTIONS_SYSTEM
        if str(settings.code_context_description_prompt) == "questions"
        else _DESCRIBE_SYSTEM
    )


async def _describe_new_chunks(
    llm: Any,
    to_insert: list[Chunk],
    stats: FileIngestStats,
    *,
    card_source: tuple[Any, str, str, str, str] | None = None,
) -> dict[int, str]:
    """LLM descriptions for the function/class chunks about to be inserted (index ->
    description); nothing without a client. `card_source` is (repo, org, ns, model,
    path): the file's orientation card is read for the `questions` prompt."""
    descriptions: dict[int, str] = {}
    if llm is None:
        return descriptions
    card = await _card_for(*card_source, True) if card_source else ""
    targets = [(i, c) for i, c in enumerate(to_insert) if c.kind in _DESCRIBE_KINDS]
    # The calls are independent (one prompt per chunk, the same card), so they run
    # concurrently under a bound instead of one after another, so a commit's describe time
    # is not the sum of its chunks' LLM round trips. Same prompts, same index → description
    # mapping.
    gate = asyncio.Semaphore(describe_concurrency())

    # the round's prefetched answers, keyed by the exact call
    hints = getattr(card_source[0], "describe_hints", None) if card_source else None

    async def _one(chunk: Chunk) -> str:
        async with gate:
            return await _describe_chunk(llm, chunk, card=card, hints=hints)

    got = await asyncio.gather(*(_one(c) for _, c in targets))
    for (i, _c), desc in zip(targets, got, strict=True):
        if desc:
            descriptions[i] = desc
            stats.described += 1
    return descriptions


def describe_concurrency() -> int:
    """`code_describe_concurrency`, at least 1 (1 = a serial walk)."""
    from server.settings import settings

    return max(1, int(settings.code_describe_concurrency))


async def _card_for(
    repo: Any,
    org: str,
    ns: str,
    model: str,
    path: str,
    wanted: bool,
) -> str:
    """The file's orientation card for the `questions` prompt; "" when not describing,
    on the one-liner prompt, or on any read failure (the card is context, never a gate)."""
    if not wanted or describe_system_prompt() is not _DESCRIBE_QUESTIONS_SYSTEM:
        return ""
    try:
        return str((await repo.cards_for_paths(org, ns, model, [path])).get(path, ""))
    except Exception as e:  # noqa: BLE001 — the card is prompt context, never a gate
        logger.debug("code ingest: card read skipped for %s: %s", path, e)
        return ""


def describe_uses_cards() -> bool:
    """Whether a description reads the file's card (the `questions` prompt)."""
    return describe_system_prompt() is _DESCRIBE_QUESTIONS_SYSTEM


def describe_prompt(chunk: Chunk, card: str = "") -> tuple[str, str, int]:
    """(system, user, max_tokens) of a chunk's description call; a chunk over
    `_DESCRIBE_MAX_CHARS` sends its head and says it was cut. One function, so a
    round's prefetch and the file's own call send the same bytes."""
    system = describe_system_prompt()
    questions = system is _DESCRIBE_QUESTIONS_SYSTEM
    head = f"File card: {card or '(none)'}\n\n" if questions else ""
    content = chunk.content
    if len(content) > _DESCRIBE_MAX_CHARS:
        shown = content[:_DESCRIBE_MAX_CHARS]
        lines = content.count("\n") + 1
        content = f"{shown}\n… (the first {shown.count(chr(10)) + 1} of {lines} lines)"
    user = f"{head}File: {chunk.path}\nScope: {chunk.scope or '(file level)'}\n\n{content}"
    return system, user, 120 if questions else 60


def describe_line(system: str, text: str) -> str:
    """The stored description from the model's answer."""
    questions = system is _DESCRIBE_QUESTIONS_SYSTEM
    text = text.strip()
    first = text.splitlines()[0].strip() if text else ""
    line = " ".join(text.split()) if questions else first
    return line[: 400 if questions else 300]


def describe_key(prompt: tuple[str, str, int]) -> str:
    """A description's cache key: the exact call it would make."""
    system, user, max_tokens = prompt
    return hashlib.sha256(f"{max_tokens}\x00{system}\x00{user}".encode()).hexdigest()


@staged("code_describe")
async def _describe_chunk(llm: Any, chunk: Chunk, *, card: str = "", hints: Any = None) -> str:
    prompt = describe_prompt(chunk, card)
    if hints is not None:
        hit = hints.get(describe_key(prompt))
        if hit is not None:
            return str(hit)
    system, user, max_tokens = prompt
    try:
        res = await llm.complete(system, user, max_tokens=max_tokens)
        return describe_line(system, res.text)
    except customer.HOLDS:
        raise  # the org's model calls are held: the repository waits
    except Exception as e:  # noqa: BLE001 — description is best-effort
        logger.debug("code ingest: describe failed for %s: %s", chunk.scope, e)
        return ""
