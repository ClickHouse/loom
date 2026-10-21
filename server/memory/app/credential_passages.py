"""Embedding raw passages (conversation chunks, chunks of a long value, session digests):
a passage that reads as a credential is stored as written and never sent to the embedder."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from server.memory.domain import chunk_shape
from server.memory.domain.sensitivity import overlaps_credential, reads_high


class Embedded(NamedTuple):
    """One raw passage's vector, [] when it has none; `held` when it reads as a credential
    and so is stored with no vector."""

    vector: list[float]
    held: bool

    @property
    def written(self) -> bool:
        """Stored: with its vector, or held without one. A failed embed is not."""
        return self.held or bool(self.vector)


async def embed_plain(
    texts: list[str],
    embed: Callable[[list[str]], Awaitable[list[list[float]]]],
    *,
    held: list[bool] | None = None,
) -> list[Embedded]:
    """One entry per text; a text that reads as a credential never reaches `embed`. A caller
    that passes `held` has already checked each text, both by its own text and by whether a
    credential in the text it was cut from overlaps it, so `held` decides alone. Otherwise
    each text is checked in a thread, off the event loop."""
    holds = held
    if holds is None:
        holds = await asyncio.to_thread(lambda: [reads_high(text) for text in texts])
    plain = [text for text, h in zip(texts, holds, strict=True) if not h]
    produced = await embed(plain) if plain else []
    vectors = iter(produced)
    return [
        Embedded(vector=[], held=True) if h else Embedded(vector=list(next(vectors)), held=False)
        for h in holds
    ]


async def embed_chunks_of(
    text: str,
    ranges: list[tuple[int, int]],
    chunks: list[str],
    embed: Callable[[list[str]], Awaitable[list[list[float]]]],
) -> list[Embedded]:
    """`embed_plain` for the `chunks` cut from `text` at `ranges`. A chunk that holds a
    credential, or a part of one cut at a chunk boundary, is held: no search leg reads a
    chunk without a vector. Only the chunked text is read, plus one chunk past its end for a
    key that crosses the last chunk's end, and the check runs in a thread, off the event
    loop."""
    bound = ranges[-1][1] + chunk_shape.CHUNK_SIZE
    held = await asyncio.to_thread(overlaps_credential, text[:bound], ranges)
    return await embed_plain(chunks, embed, held=held)
