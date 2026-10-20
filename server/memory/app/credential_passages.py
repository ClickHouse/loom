"""Raw passages (conversation chunks, chunks of a long value, session digests) that read as
a credential: stored as written, never embedded, and never served to every reader of a
namespace. `reads_high` decides from the text alone, so a passage an earlier build embedded
is held back too."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from server.memory.domain.sensitivity import reads_high


async def embed_plain(
    texts: list[str], embed: Callable[[list[str]], Awaitable[list[list[float]]]]
) -> list[list[float] | None]:
    """One entry per text: None for a text that reads as a credential (it never reaches
    `embed`), else its vector, [] when the embed failed."""
    held = [reads_high(text) for text in texts]
    plain = [text for text, h in zip(texts, held, strict=True) if not h]
    vectors = iter(await embed(plain) if plain else [])
    return [None if h else list(next(vectors)) for h in held]


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
