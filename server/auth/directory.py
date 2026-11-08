"""The namespace facts the authorization gate reads, the ones an all_mine
fan-out narrows by (kind, visibility), and whether a name a path would register
as new is already in use.

The gate is platform code; what it decides on belongs to the domains: which
namespaces of an org are restricted (the directory rows, the code-plane repo
config and the tracker sources), which are configured code-plane entries, and
what kind each one is. The identity domain answers through a
`NamespaceDirectory`; the composition root (`server/wiring.py`) hands it in
with `use_directory` before the process serves.

A process nobody wired has no directory: every lookup raises
`DirectoryNotWired`, a `ClassificationUnavailable`, so each gate that denies
on an unreadable directory denies on it too.
"""

from __future__ import annotations

from typing import Protocol

from server.core.repo_base import _CHClient


class ClassificationUnavailable(RuntimeError):
    """The restricted set for an org could not be read and no snapshot exists."""


class DirectoryNotWired(ClassificationUnavailable):
    """No `NamespaceDirectory` was handed in: this process cannot tell a
    restricted namespace from any other."""


class CodeEntry(Protocol):
    """A configured code-plane entry, as far as authorization reads it."""

    @property
    def classification(self) -> str: ...

    @property
    def org_readable(self) -> bool: ...


class NamespaceDirectory(Protocol):
    async def is_restricted(self, org: str, namespace: str) -> bool:
        """Raises `ClassificationUnavailable` when the directory cannot be
        read and no snapshot exists."""
        ...

    async def restricted_set(self, org: str) -> frozenset[str]:
        """Namespaces the directory rows restrict; raises like `is_restricted`."""
        ...

    def is_restricted_cached(self, org: str, namespace: str) -> bool:
        """The configuration plus the org's cached snapshot, stale or not; no
        I/O, False with no snapshot. For writers that run after the gate."""
        ...

    def config_classification(self, org: str, namespace: str) -> str:
        """The classification the repo config or the tracker sources give, `standard`
        when neither restricts it. No I/O."""
        ...

    def code_entry(self, org: str, namespace: str) -> CodeEntry | None:
        """The configured code-plane entry for (org, namespace). No I/O."""
        ...

    def restricted_config_namespaces(self, org: str) -> set[str]:
        """The namespaces of `org` the configuration classifies restricted: code-plane
        entries and tracker namespaces. No I/O."""
        ...

    async def fanout_namespaces(
        self, org: str, namespaces: list[str], kinds: str | None = None
    ) -> list[str]:
        """`namespaces` in order, narrowed to `kinds` (a comma list; the
        memory fan-out's by default)."""
        ...

    async def public_namespaces(self, org: str, namespaces: list[str]) -> set[str]:
        """Those of `namespaces` whose visibility is public; one of unknown
        visibility is not."""
        ...

    async def in_use(self, org: str, namespace: str, *, ch: _CHClient | None = None) -> bool:
        """Whether (org, namespace) holds state a later owner would inherit, registered or
        not: data (a memory only Postgres holds included), a live row of
        `namespace_census.STATE_TABLES`, or a configured code-plane repository or tracker
        namespace. Raises when it cannot be read."""
        ...


_directory: NamespaceDirectory | None = None


def use_directory(directory: NamespaceDirectory | None) -> None:
    """Hand in the process's directory (None takes it away again)."""
    global _directory
    _directory = directory


def directory() -> NamespaceDirectory:
    if _directory is None:
        raise DirectoryNotWired("no namespace directory is wired into this process")
    return _directory


__all__ = [
    "ClassificationUnavailable",
    "CodeEntry",
    "DirectoryNotWired",
    "NamespaceDirectory",
    "directory",
    "use_directory",
]
