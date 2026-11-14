"""A namespace's mirrors, for the erasure saga, which erases them with the namespace they
belong to (docs/architecture/document-sources.md)."""

from __future__ import annotations

from server.knowledge.store import mirrors as store


async def mirrors_of(org: str, namespaces: list[str]) -> dict[str, str]:
    """Every mirror the namespaces have had, with its namespace."""
    return await store.mirrors_of(org, namespaces)


async def forget_mirrors(org: str, mirrors: dict[str, str]) -> None:
    """Write these mirrors `deleted` once their rows are erased."""
    await store.forget_mirrors(org, mirrors)


__all__ = ["forget_mirrors", "mirrors_of"]
