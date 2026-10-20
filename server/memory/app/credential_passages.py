"""Raw passages (conversation chunks, chunks of a long value, session digests) that read as
a credential: stored as written, never embedded, and never served to every reader of a
namespace. `reads_high` decides from the text alone, so a passage an earlier build embedded
is held back too."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import NamedTuple

from server.memory.domain.sensitivity import reads_high

# Spare rows a read asks for, so a passage it holds back does not leave the page short.
HELD_MARGIN = 2


class Embedded(NamedTuple):
    """One passage's vector, [] when it has none; `held` when it reads as a credential."""

    vector: list[float]
    held: bool

    @property
    def written(self) -> bool:
        """Stored: with its vector, or held without one. A failed embed is not."""
        return self.held or bool(self.vector)


async def embed_plain(
    texts: list[str], embed: Callable[[list[str]], Awaitable[list[list[float]]]]
) -> list[Embedded]:
    """One entry per text; a text that reads as a credential never reaches `embed`."""
    held = [reads_high(text) for text in texts]
    plain = [text for text, h in zip(texts, held, strict=True) if not h]
    vectors = iter(await embed(plain) if plain else [])
    return [Embedded([], True) if h else Embedded(list(next(vectors)), False) for h in held]


def without_credentials[T](
    items: Iterable[T], text: Callable[[T], str], limit: int | None = None
) -> list[T]:
    """`items`, in order, whose text does not read as a credential, the first `limit` of
    them. The text is checked only as far as `limit` reaches."""
    kept: list[T] = []
    for item in items:
        if limit is not None and len(kept) >= limit:
            break
        if not reads_high(text(item)):
            kept.append(item)
    return kept
