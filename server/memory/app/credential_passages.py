"""Embedding raw passages (conversation chunks, chunks of a long value, session digests):
a passage that reads as a credential is stored as written and never sent to the embedder."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from server.memory.domain.sensitivity import credential_spans, reads_high


class Embedded(NamedTuple):
    """One raw passage's vector, [] when it has none; `held` when it reads as a credential
    and so is stored with no vector."""

    vector: list[float]
    held: bool

    @property
    def written(self) -> bool:
        """Stored: with its vector, or held without one. A failed embed is not."""
        return self.held or bool(self.vector)


def overlaps_credential(source: str, ranges: list[tuple[int, int]]) -> list[bool]:
    """For each [start, end) range of `source` (a chunk's place in the text it was cut from),
    whether it overlaps a credential in `source`. A chunk cut through a key holds only its
    head or its tail, which does not read as a credential on its own."""
    spans = credential_spans(source)
    return [
        any(start < s_end and s_start < end for s_start, s_end in spans) for start, end in ranges
    ]


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
