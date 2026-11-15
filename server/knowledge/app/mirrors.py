"""A namespace's mirrors: the erasure saga erases them with the namespace they belong to,
and the sync writes a mirrored source's documents into one
(docs/architecture/document-sources.md, The mirror)."""

from collections.abc import Awaitable, Callable

from server.auth.directory import directory
from server.core.ch import sequential_reads
from server.core.naming import new_mirror_name
from server.knowledge.app.sources import sources_lease
from server.knowledge.store.mirrors import (
    add_mirror,
    forget_mirrors,
    mirrors_of,
)


async def live_mirror(org: str, namespace: str) -> str:
    """The namespace's live mirror, or "" when it has none."""
    with sequential_reads():
        live = await mirrors_of(org, [namespace], live_only=True)
    return next(iter(live), "")


async def ensure_mirror(
    org: str, namespace: str, *, still_current: Callable[[], Awaitable[None]]
) -> str:
    """The namespace's live mirror, made now if it has none, under the namespace's
    sources lease, so two turns never make two mirrors. `still_current` runs under the
    lease first and raises when the source asking was removed meanwhile (an erasure of
    the namespace, which takes the same lease), so no mirror outlives its namespace. A
    new name already in use is drawn again: no namespace of the form is ever taken for
    a mirror (rolling-upgrade rule 51)."""
    if live := await live_mirror(org, namespace):
        return live
    async with sources_lease(org, namespace):
        if live := await live_mirror(org, namespace):
            return live  # another turn made it while this one waited for the lease
        await still_current()
        return await _register_mirror(org, namespace)


async def _register_mirror(org: str, namespace: str) -> str:
    mirror = new_mirror_name()
    while await directory().in_use(org, mirror):
        mirror = new_mirror_name()
    await add_mirror(org, namespace, mirror)
    return mirror


__all__ = ["ensure_mirror", "forget_mirrors", "live_mirror", "mirrors_of"]
