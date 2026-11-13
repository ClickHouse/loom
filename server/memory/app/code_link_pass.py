"""What one pass of the code-link worker reads of a repository, what it counts, and the
cursors it resumes from."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from server.code.app import memory_links as code
from server.code.domain.anchor import (
    Definition,
    Link,
)
from server.code.domain.successor import Successor
from server.memory.store.code_links import LinkRow
from server.memory.store.key_write_lock import KeyWriteLocks
from server.settings import settings

log = logging.getLogger(__name__)

# One host rechecks a repository at a time; the lease outlives the worker's pass budget.
repository_locks = KeyWriteLocks(wait_s=1.0, ttl_s=900)


@dataclass
class PassResult:
    queued: int = 0  # queue entries read
    linked: int = 0
    unlinked: int = 0
    rechecked: int = 0
    suggested: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "queued": self.queued,
            "linked": self.linked,
            "unlinked": self.unlinked,
            "rechecked": self.rechecked,
            "suggested": self.suggested,
            "errors": len(self.errors),
        }


@dataclass
class Budget:
    judgements: int


@dataclass(frozen=True)
class Range:
    """The paths changed from `base` to a pass's head: path -> its path there ('' =
    deleted)."""

    base: str
    paths: dict[str, str]


# parsed files a pass keeps: a full recheck reads every linked file of a repository
_FILES_CACHED = 512


class Recent[K, V](OrderedDict[K, V]):
    """A mapping that keeps its last `size` used entries."""

    def __init__(self, size: int) -> None:
        super().__init__()
        self.size = size

    def used(self, key: K) -> V | None:
        if key not in self:
            return None
        self.move_to_end(key)
        return self[key]

    def keep(self, key: K, value: V) -> None:
        self[key] = value
        while len(self) > self.size:
            self.popitem(last=False)


class RepoReads:
    """One repository's git reads for a pass (files, history, ancestry, successors), each
    made once; parsed files while they are among the last `_FILES_CACHED` read."""

    def __init__(self, org: str, cns: str) -> None:
        self.org, self.cns = org, cns
        self._parsed: Recent[tuple[str, str], code.FileAt | None] = Recent(_FILES_CACHED)
        self._read: dict[tuple[Any, ...], Any] = {}
        # ("here" | "elsewhere", …): the same-file search and the one in another module
        self.successors: dict[tuple[str, ...], Successor] = {}

    async def _once[T](self, key: tuple[Any, ...], read: Callable[[], Awaitable[T]]) -> T:
        if key not in self._read:
            self._read[key] = await read()
        return cast(T, self._read[key])

    async def at(self, commit: str, path: str) -> code.FileAt | None:
        key = (commit, path)
        if key in self._parsed:
            return self._parsed.used(key)
        parsed = await code.parsed(self.org, self.cns, commit, path)
        self._parsed.keep(key, parsed)
        return parsed

    async def has(self, commit: str) -> bool:
        """The mirror still holds `commit`: an old one may be gone after a history rewrite."""
        return await self._once(
            ("has", commit), lambda: code.has_commit(self.org, self.cns, commit)
        )

    async def range(self, base: str, head: str) -> Range | None:
        """None after a force-push or when git cannot tell."""

        async def read() -> Range | None:
            kept = await code.kept_paths(self.org, self.cns, base, head)
            return None if kept is None else Range(base, kept)

        return await self._once(("range", base, head), read)

    async def renamed_from(self, base: str, head: str, paths: list[str]) -> dict[str, str] | None:
        """Path at `head` -> its path at `base` for those of `paths` renamed since."""
        return await self._once(
            ("renamed", base, head, *paths),
            lambda: code.renamed_from(self.org, self.cns, base, head, paths),
        )

    async def containing(
        self, base: str, head: str, literal: str, *, limit: int
    ) -> list[str] | None:
        """The files `base..head` added or changed whose text at `head` holds the word
        `literal`, fewer than `limit`, those the ingest would not index left out; an empty
        list when the range's diff cannot be read (a force-push, a diff git fails on),
        more than `memory_code_links_mirror_changed_files` changed or `limit` or more
        hold it; None when the grep cannot be run. A file git then cannot read fails the
        recheck, as any other file read does."""

        async def read() -> list[str] | None:
            touched = await self.touched(base, head) if base else None
            if not touched:
                return []
            if len(touched) > settings.memory_code_links_mirror_changed_files:
                log.info(
                    "memory links: %s not searched for in the mirror: %d files changed in "
                    "%s..%s of %s, past memory_code_links_mirror_changed_files",
                    literal,
                    len(touched),
                    base[:12],
                    head[:12],
                    self.cns,
                )
                return []
            scope = await self._once(
                ("scope", head), lambda: code.ingest_scope(self.org, self.cns, head)
            )
            if scope is None:
                return None

            async def taken() -> list[str]:
                return sorted(p for p in touched if scope.takes(p))

            paths = await self._once(("indexed paths", base, head), taken)
            found = await code.grep_words(scope, head, literal, paths, limit=limit)
            if found is None:
                return None
            if len(found) >= limit:  # a cut list is never taken as whole
                log.info(
                    "memory links: %s named by %d or more changed files of %s, past "
                    "memory_code_links_mirror_naming_files: the index decides",
                    literal,
                    limit,
                    self.cns,
                )
                return []
            return [p for p in found if await self._indexed_names(scope, head, p) is not None]

        return await self._once(("containing", head, literal, base), read)

    async def _indexed_names(
        self, scope: code.IngestScope, head: str, path: str
    ) -> set[str] | None:
        """`code.indexed_names`, once per pass: one read answers both the content refusal
        and the names, which then serve `names` too. None for a file that does not parse
        as well: the ingest indexes nothing of it."""

        async def read() -> set[str] | None:
            try:
                return await code.indexed_names(scope, head, path)
            except code.ParseFailed:
                return None

        names = await self._once(("indexed", head, path), read)
        if names is not None:
            self._read.setdefault(("names", head, path), names)
        return names

    async def names(self, commit: str, path: str) -> set[str] | None:
        """The qualified names `path` defines at `commit`, read without fingerprints."""
        if (commit, path) in self._parsed:  # parsed in full already
            parsed = self._parsed.used((commit, path))
            return {d.qualified_name for d in parsed.defs} if parsed is not None else None
        return await self._once(
            ("names", commit, path), lambda: code.names_at(self.org, self.cns, commit, path)
        )

    async def edits_of(
        self, base: str, head: str, path: str, name: str
    ) -> list[tuple[str, str]] | None:
        """`gitrepo.edits_of`: the commits of `base..head` that can change whether `path`
        defines the dotted `name`."""
        return await self._once(
            ("edits", base, head, path, name),
            lambda: code.edits_of(self.org, self.cns, base, head, path, name),
        )

    async def touched(self, base: str, head: str) -> set[str] | None:
        """The paths at `head` that `base..head` added or changed."""
        return await self._once(
            ("touched", base, head), lambda: code.touched_paths(self.org, self.cns, base, head)
        )

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether `ancestor` is in `descendant`'s history."""
        return await self._once(
            ("descends", ancestor, descendant),
            lambda: code.descends(self.org, self.cns, ancestor, descendant),
        )

    async def defs(self, commit: str, path: str, qualified_name: str) -> list[Definition]:
        f = await self.at(commit, path)
        return [d for d in f.defs if d.qualified_name == qualified_name] if f else []


def claim_of(value: Any) -> str | None:
    """A stored value's text (the column holds JSON); None when it is not text."""
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        decoded = value
    return decoded if isinstance(decoded, str) else None


def text_of(value: Any) -> str:
    """A stored value as text: its claim, else the value as stored."""
    claim = claim_of(value)
    return claim if claim is not None else str(value)


def link_of(row: LinkRow) -> Link:
    return Link(
        path=row["path"],
        qualified_name=row["qualified_name"],
        body_hash=row["body_hash"],
        checked_hash=row["checked_hash"],
        state=row["state"],
        changed_since=row["changed_since"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        carried=bool(row["carried"]),
        anchored_path=row["anchored_path"],
        anchored_name=row["anchored_name"],
        checked_path=row["checked_path"],
    )


class Cursor[T]:
    """One of the link worker's cursors, kept as JSON; `empty` until it is written."""

    def __init__(self, name: str, empty: T) -> None:
        self.name, self.empty = name, empty

    async def read(self) -> T:
        raw = await code.link_cursor(self.name)
        return cast(T, json.loads(raw)) if raw else self.empty

    async def write(self, value: T) -> None:
        await code.set_link_cursor(self.name, json.dumps(value))

    async def clear(self) -> None:
        await code.set_link_cursor(self.name, "")
