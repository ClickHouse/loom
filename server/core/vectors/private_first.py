"""Private-first vector search over several namespaces, for every plane that
fans out (memory, knowledge): the addressed namespace and the private ones
first, the public ones only when that pass found nothing close enough."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any

# A span around one pass, called as span(op=..., namespaces=n).
Span = Callable[..., AbstractContextManager[Any]]
# The visibility of each namespace ("private" or "public"), read on demand.
Visibility = Callable[[list[str]], Awaitable[dict[str, str]]]


async def search_private_first[H](
    namespace: str,
    namespaces: list[str],
    search_one: Callable[[str], Awaitable[tuple[str, list[H]]]],
    *,
    visibility: Visibility | None,
    threshold: float,
    span: Span,
) -> list[tuple[str, list[H]]]:
    """`search_one` over `namespaces`, private first: the public ones are read
    only when no first-pass hit reaches `threshold` similarity. Every
    namespace is read at once when there is one, when `visibility` is None
    (not a fan-out), or when no private namespace can be read first."""
    passes = None
    if visibility is not None and len(namespaces) > 1:
        passes = private_first_passes(namespace, namespaces, await visibility(namespaces))
    if passes is None:
        with span(op="vector_search", namespaces=len(namespaces)):
            return list(await asyncio.gather(*map(search_one, namespaces)))
    first, second = passes
    with span(op="vector_search.private", namespaces=len(first)):
        results = list(await asyncio.gather(*map(search_one, first)))
    if second and best_similarity(results) < threshold:
        with span(op="vector_search.public", namespaces=len(second)):
            results += await asyncio.gather(*map(search_one, second))
    return results


def private_first_passes(
    namespace: str, namespaces: list[str], visibility: dict[str, str]
) -> tuple[list[str], list[str]] | None:
    """The two passes of a private-first search over `namespaces`: the
    addressed `namespace` and the private ones, then the public ones. A
    namespace missing from `visibility` counts as private. None, meaning read
    them all at once, when `visibility` is empty or every one of `namespaces`
    is answered public."""
    private = {ns for ns in namespaces if visibility.get(ns, "private") == "private"}
    if not visibility or not private:
        return None
    first = [ns for ns in namespaces if ns in private or ns == namespace]
    return first, [ns for ns in namespaces if ns not in first]


def best_similarity(results: Sequence[tuple[str, Sequence[Any]]]) -> float:
    """The highest similarity (1 - distance) of any hit in per-namespace results."""
    return max(
        (1.0 - getattr(h, "distance", 1.0) for _, hits in results for h in hits), default=0.0
    )
