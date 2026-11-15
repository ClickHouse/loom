"""A namespace's mirrors: the erasure saga erases them with the namespace they belong to,
and the sync writes a mirrored source's documents into one
(docs/architecture/document-sources.md, The mirror)."""

from server.auth.directory import directory
from server.core.ch import sequential_reads
from server.knowledge.store.mirrors import (
    add_mirror,
    forget_mirrors,
    mirrors_of,
    new_mirror_name,
    record_listing,
)


async def live_mirror(org: str, namespace: str) -> str:
    """The namespace's live mirror, or "" when it has none."""
    with sequential_reads():
        live = await mirrors_of(org, [namespace], live_only=True)
    return next(iter(live), "")


async def ensure_mirror(org: str, namespace: str) -> str:
    """The namespace's live mirror, made now if it has none. The caller holds the
    namespace's sources lease, so two turns never make two mirrors. A new name already
    in use is drawn again: no namespace of the form is ever taken for a mirror
    (rolling-upgrade rule 51)."""
    if live := await live_mirror(org, namespace):
        return live
    mirror = new_mirror_name()
    while await directory().in_use(org, mirror):
        mirror = new_mirror_name()
    await add_mirror(org, namespace, mirror)
    return mirror


__all__ = ["ensure_mirror", "forget_mirrors", "live_mirror", "mirrors_of", "record_listing"]
