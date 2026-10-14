"""Private-first vector search over several namespaces, for every plane that
fans out (memory, knowledge): the addressed namespace and the private ones
first, the public ones only when that pass found nothing close enough."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any

# A span around one pass: (op, number of namespaces) -> context manager.
Span = Callable[[str, int], AbstractContextManager[Any]]


async def search_private_first[Hits: list[Any]](
    namespace: str,
    namespaces: list[str],
    visibility: dict[str, str],
    search_one: Callable[[str], Awaitable[tuple[str, Hits]]],
    *,
    threshold: float,
    span: Span,
) -> list[tuple[str, Hits]]:
    """`search_one` over `namespaces`, private first: the public ones are read
    only when no first-pass hit reaches `threshold` similarity. With no
    private namespace to read first, every namespace is read at once."""
    first, second = private_first_passes(namespace, namespaces, visibility)
    if not first:
        with span("vector_search", len(namespaces)):
            return list(await asyncio.gather(*map(search_one, namespaces)))
    with span("vector_search.private", len(first)):
        results = list(await asyncio.gather(*map(search_one, first)))
    if second and _best_similarity(results) < threshold:
        with span("vector_search.public", len(second)):
            results += await asyncio.gather(*map(search_one, second))
    return results


def private_first_passes(
    namespace: str, namespaces: list[str], visibility: dict[str, str]
) -> tuple[list[str], list[str]]:
    """The two passes of a private-first search over `namespaces`: the
    addressed `namespace` and the private ones, then the public ones. A
    namespace missing from `visibility` counts as private. Both passes are
    empty, meaning read them all at once, when `visibility` is empty or every
    one of `namespaces` is answered public."""
    private = {ns for ns in namespaces if visibility.get(ns, "private") == "private"}
    if not visibility or not private:
        return [], []
    first = [ns for ns in namespaces if ns in private or ns == namespace]
    return first, [ns for ns in namespaces if ns not in first]


def _best_similarity(results: Sequence[tuple[str, Sequence[Any]]]) -> float:
    return max(
        (1.0 - getattr(h, "distance", 1.0) for _, hits in results for h in hits), default=0.0
    )
