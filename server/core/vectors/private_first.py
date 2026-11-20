"""Private-first vector search over several namespaces, for every plane that
fans out (memory, knowledge): the addressed namespace and the private ones
first, the public ones only when that pass found nothing close enough."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any

from server.core.shielded import to_its_end

# not a setting: the read scheduler retains at most four child jobs per window.
_READ_WINDOW = 4

# A span around one pass, called as span(op=..., namespaces=n).
Span = Callable[..., AbstractContextManager[Any]]
# The visibility of each namespace ("private" or "public"), read on demand.
Visibility = Callable[[list[str]], Awaitable[dict[str, str]]]


def each[I, R](
    search_one: Callable[[I], Awaitable[R]],
) -> Callable[[list[I]], Awaitable[list[R]]]:
    """Read ordered inputs in bounded windows, joining every owned child on exit."""

    async def _many(inputs: list[I]) -> list[R]:
        results: list[R] = []
        for offset in range(0, len(inputs), _READ_WINDOW):
            tasks: list[asyncio.Future[R]] = []
            failure: BaseException | None = None
            try:
                for item in inputs[offset : offset + _READ_WINDOW]:
                    tasks.append(asyncio.ensure_future(search_one(item)))
                results.extend(await asyncio.gather(*tasks))
            except BaseException as error:
                failure = error
                raise
            finally:
                for task in tasks:
                    task.cancel()

                async def stopped(owned: list[asyncio.Future[R]]) -> None:
                    await asyncio.gather(*owned, return_exceptions=True)

                try:
                    await to_its_end(stopped(tasks), name="vector-read-window-cleanup")
                except asyncio.CancelledError:
                    # Cleanup cancellation must not replace the first failed read.
                    if failure is None:
                        raise
        return results

    return _many


async def search_private_first[H](
    namespace: str,
    namespaces: list[str],
    search: Callable[[list[str]], Awaitable[list[tuple[str, list[H]]]]],
    *,
    visibility: Visibility | None,
    threshold: float,
    span: Span,
    may_end: Callable[[H], bool] | None = None,
) -> list[tuple[str, list[H]]]:
    """`search` over `namespaces`, private first: the public ones are read only
    when no first-pass hit reaches `threshold` similarity. Every namespace is
    read in one pass when there is one, when `visibility` is None (not a
    fan-out), or when no private namespace can be read first. `search` reads
    a list of namespaces (`each` for one statement per namespace). `may_end` picks
    the hits that may end the search after the first pass; every hit when None."""
    passes = None
    if visibility is not None and len(namespaces) > 1:
        passes = private_first_passes(namespace, namespaces, await visibility(namespaces))
    if passes is None:
        with span(op="vector_search", namespaces=len(namespaces)):
            return await search(namespaces)
    first, second = passes
    with span(op="vector_search.private", namespaces=len(first)):
        results = await search(first)
    if second and best_similarity(results, may_end) < threshold:
        with span(op="vector_search.public", namespaces=len(second)):
            results += await search(second)
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


def best_similarity(
    results: Sequence[tuple[str, Sequence[Any]]], may_end: Callable[[Any], bool] | None = None
) -> float:
    """The highest similarity (1 - distance) of any hit in per-namespace results, of the
    hits `may_end` picks when given."""
    return max(
        (
            1.0 - getattr(h, "distance", 1.0)
            for _, hits in results
            for h in hits
            if may_end is None or may_end(h)
        ),
        default=0.0,
    )
