"""Embedding raw passages (conversation chunks, chunks of a long value, session digests):
a passage that reads as a credential is stored as written and never sent to the embedder.
The read-side helpers live beside `reads_high` (`server/memory/domain/sensitivity.py`)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from server.memory.domain.sensitivity import Embedded, reads_high


async def embed_plain(
    texts: list[str], embed: Callable[[list[str]], Awaitable[list[list[float]]]]
) -> list[Embedded]:
    """One entry per text; a text that reads as a credential never reaches `embed`."""
    held = [reads_high(text) for text in texts]
    plain = [text for text, h in zip(texts, held, strict=True) if not h]
    produced = await embed(plain) if plain else []
    vectors = iter([v for _text, v in zip(plain, produced, strict=True)])
    return [Embedded([], True) if h else Embedded(list(next(vectors)), False) for h in held]
