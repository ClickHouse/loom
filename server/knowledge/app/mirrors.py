"""A namespace's mirror, for the planes outside knowledge that must reach it: the erasure
saga erases it with the namespace it belongs to (docs/architecture/document-sources.md)."""

from __future__ import annotations

from server.knowledge.store import mirrors as store


async def mirrors_of(org: str, namespaces: list[str]) -> dict[str, str]:
    """Each of `namespaces` that has a mirror, with its mirror's name."""
    return await store.mirrors_of(org, namespaces)


async def forget_mirrors(org: str, mirrors: dict[str, str]) -> None:
    """Retire these registry rows once their mirrors' rows are erased."""
    await store.forget_mirrors(org, mirrors)


__all__ = ["forget_mirrors", "mirrors_of"]
