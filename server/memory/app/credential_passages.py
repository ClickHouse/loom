"""Embedding raw passages (conversation chunks, chunks of a long value, session digests):
a passage that reads as a credential is stored as written and never sent to the embedder."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from server.memory.domain.sensitivity import reads_high


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
    """One entry per text; a text that reads as a credential, or that `held` marks (a chunk
    holding part of one), never reaches `embed`."""
    marked = held or [False] * len(texts)
    holds = [h or reads_high(text) for text, h in zip(texts, marked, strict=True)]
    plain = [text for text, h in zip(texts, holds, strict=True) if not h]
    produced = await embed(plain) if plain else []
    vectors = iter(produced)
    return [
        Embedded(vector=[], held=True) if h else Embedded(vector=list(next(vectors)), held=False)
        for h in holds
    ]
