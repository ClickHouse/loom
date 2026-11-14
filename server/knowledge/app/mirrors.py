"""A namespace's mirrors, for the erasure saga, which erases them with the namespace they
belong to (docs/architecture/document-sources.md)."""

from server.knowledge.store.mirrors import forget_mirrors, mirrors_of

__all__ = ["forget_mirrors", "mirrors_of"]
