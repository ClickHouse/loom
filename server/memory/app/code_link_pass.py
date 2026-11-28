"""What one pass of the code-link worker reads of a repository, what it counts, and the
cursors it resumes from."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from server.code.app import memory_links as code
from server.code.domain.anchor import (
    Definition,
    Link,
    local_name,
    unnumbered,
)
from server.code.domain.successor import Side, Successor
from server.memory.store.code_links import LinkRow
from server.memory.store.key_write_lock import KeyWriteLocks
from server.settings import settings
from server.workers import pass_context

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
    # ranges that changed too many files to read whole for a definition renamed as it
    # moved: it is looked for in the files that hold its lines
    wide_ranges: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "queued": self.queued,
            "linked": self.linked,
            "unlinked": self.unlinked,
            "rechecked": self.rechecked,
            "suggested": self.suggested,
            "wide_ranges": self.wide_ranges,
            "errors": len(self.errors),
        }

    def failed(self, error: str, e: BaseException, leg: str) -> None:
        """A leg that raised (one repository's link or recheck): `error` joins the pass's
        errors, and `e` is noted on the worker's pass under `leg`, so the pass counts as
        partial, not clean."""
        self.errors.append(error)
        pass_context.note_failure(e, leg)


@dataclass
class Budget:
    judgements: int


@dataclass(frozen=True)
class Range:
    """The paths changed from `base` to a pass's head: path -> its path there ('' =
    deleted)."""

    base: str
    paths: dict[str, str]


@dataclass(frozen=True)
class Turnover:
    """The definitions a range brought in, took out and left, file by file: `new` the
    files at its head, each with the definitions whose name it did not define at the
    base, and `kept` with those whose name it did; `gone` the files at the base, each
    with the definitions whose name it no longer defines."""

    new: list[Side]
    gone: list[Side]
    kept: list[Side]


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
        # ("here" | "elsewhere" | "renamed", …): the same-file search, the one for its
        # name in another module and the one for it under another name
        self.successors: dict[tuple[str, ...], Successor] = {}
        # ranges `turnover` did not read whole: they changed more files than its limit
        self.wide_ranges = 0

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
            if not base:
                return []  # a link never checked at a commit: nothing to search from
            touched = await self.touched(base, head)
            if touched is None:
                log.info(
                    "memory links: %s not searched for in the mirror: %s..%s of %s cannot be read",
                    literal,
                    base[:12],
                    head[:12],
                    self.cns,
                )
                return []
            if not touched:
                return []
            scope = await self._once(
                ("scope", head), lambda: code.ingest_scope(self.org, self.cns, head)
            )
            if scope is None:
                return None

            async def in_scope() -> tuple[int, list[str] | None]:
                # by prefix first: the denylist is matched only on a range within the bound
                under = [p for p in touched if scope.under(p)]
                if len(under) > settings.memory_code_links_mirror_changed_files:
                    return len(under), None
                return len(under), sorted(filter(scope.takes, under))

            count, paths = await self._once(("indexed paths", base, head), in_scope)
            if paths is None:
                log.info(
                    "memory links: %s not searched for in the mirror: %d files changed "
                    "in %s..%s of %s, past memory_code_links_mirror_changed_files",
                    literal,
                    count,
                    base[:12],
                    head[:12],
                    self.cns,
                )
                return []
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

    async def turnover(self, base: str, head: str, *, limit: int) -> Turnover | None:
        """The definitions `base..head` brought in and took out of the files it changed,
        those the ingest would not index left out, a renamed file read as one. None when
        the range cannot be read (a force-push, a diff git fails on), or changed more
        than `limit` files under the entry's prefixes: counted in `wide_ranges`, and
        read only where a definition's lines are (`turnover_holding`)."""

        async def read() -> Turnover | None:
            paths = await self._changed(base, head)
            if paths is None:
                return None
            if len(paths[1]) > limit:
                self.wide_ranges += 1
                return None
            return await self._read_turnover(paths[0], base, head, paths[1])

        return await self._once(("turnover", base, head), read)

    async def turnover_holding(
        self, base: str, head: str, lines: Sequence[str], path: str, *, limit: int
    ) -> Turnover | None:
        """`turnover` of a range too wide to read whole, over `path` and the files that
        hold one of `lines` at either end: parsing thousands of files takes longer than
        a pass may run, and a definition that moved whole took its lines with it. None
        when the range cannot be read, those files cannot be told (`_holding`) or they
        are more than `limit` with `path`: no more is parsed than of a range read whole.
        A definition in a file that holds none of `lines` is not read: a removed or kept
        one that differs from the lost one in exactly those lines, or was rewrapped,
        contests nothing here, where in a range read whole it would leave the link
        unsure."""

        async def read() -> Turnover | None:
            paths = await self._changed(base, head)
            held = (
                await self._holding(paths[0], base, head, paths[1], lines, limit) if paths else None
            )
            if paths is None or held is None:
                return None
            some = {
                old: now
                for old, now in paths[1].items()
                if old in held or now in held or path in (old, now)
            }
            if len(some) > limit:
                self._unsearched(base, head, len(paths[1]), "its lines are in too many")
                return None
            return await self._read_turnover(paths[0], base, head, some)

        return await self._once(("turnover", base, head, path, *lines), read)

    async def replaced(self, base: str, head: str, kept: Sequence[Definition]) -> list[Definition]:
        """Those of `kept` (definitions at `head` under a name their file had at `base`)
        whose name held another text at `base`: the range put this text under it. One
        whose file at `base` does not parse stays a kept name."""
        changed = await self.range(base, head) if kept else None
        was_at = {now: old for old, now in changed.paths.items()} if changed else {}
        found = []
        for d in kept:
            try:
                there = await self.at(base, was_at.get(d.path, d.path))
            except code.ParseFailed:
                continue
            name = unnumbered(local_name(d.qualified_name, d.path))
            held = {
                x.shape.exact
                for x in (there.defs if there else ())
                if unnumbered(local_name(x.qualified_name, x.path)) == name
            }
            if held and d.shape.exact not in held:
                found.append(d)
        return found

    async def _changed(
        self, base: str, head: str
    ) -> tuple[code.IngestScope, dict[str, str]] | None:
        """The entry's ingest scope at `head` and the paths `base..head` changed under
        its prefixes (old -> its path at `head`, '' = deleted); None when the range
        cannot be read."""
        changed = await self.range(base, head)
        scope = await self._once(
            ("scope", head), lambda: code.ingest_scope(self.org, self.cns, head)
        )
        if changed is None or scope is None:
            return None
        # by prefix first: the denylist is matched only on the files read
        return scope, {o: n for o, n in changed.paths.items() if scope.under(n or o)}

    async def _read_turnover(
        self, scope: code.IngestScope, base: str, head: str, paths: dict[str, str]
    ) -> Turnover:
        """What each of `paths` (old -> its path at `head`, '' = deleted) gained, lost
        and kept."""
        found = Turnover([], [], [])
        for old, now in sorted(paths.items()):
            if not scope.takes(now or old):
                continue
            try:
                await self._file_turnover(scope, base, old, head, now, found)
            except code.ParseFailed:
                pass  # the index holds nothing of a file that does not parse
        return found

    async def _holding(
        self,
        scope: code.IngestScope,
        base: str,
        head: str,
        paths: dict[str, str],
        lines: Sequence[str],
        limit: int,
    ) -> set[str] | None:
        """The paths of a wide range (`paths`: old -> its path at `head`) whose text
        holds one of `lines`: at `head` under its path there, at `base` under its old
        one. None when that cannot be told: no line to look for, more than
        `memory_code_links_mirror_changed_files` paths (each is an argument to git),
        more than `limit` files holding one at either end (a line any file has), or a
        search git cannot run or finish, which is then not made again over this range.
        Each is logged, the second as a warning once a range."""
        if len(paths) > settings.memory_code_links_mirror_changed_files:
            if ("too wide", base, head) not in self._read:
                self._read["too wide", base, head] = True
                # a warning: `code.refresh` rechecks too and keeps no count
                log.warning(
                    "memory links: no renamed definition searched for across files: %d "
                    "files changed in %s..%s of %s, past memory_code_links_mirror_changed_files",
                    len(paths),
                    base[:12],
                    head[:12],
                    self.cns,
                )
            return None
        held: set[str] = set()
        why = "" if lines else "it has no line of its own"
        if ("unsearchable", base, head) in self._read:
            why = "git cannot search them"  # found out for another definition: not again
        ends = ((head, set(paths.values()) - {""}), (base, set(paths))) if not why else ()
        for commit, some in ends:
            takes = sorted(filter(scope.takes, some))
            # one past the bound: a cut list is never taken as whole
            found = await code.grep_lines(scope, commit, lines, takes, limit=limit + 1)
            if found is None:
                # the paths are the same for every definition of the range: the next
                # one's search would wait as long
                self._read["unsearchable", base, head] = True
            if found is None or len(found) > limit:
                why = "git cannot search them" if found is None else "its lines are in too many"
                break
            held.update(found)
        if why:
            self._unsearched(base, head, len(paths), why)
            return None
        return held

    def _unsearched(self, base: str, head: str, changed: int, why: str) -> None:
        log.info(
            "memory links: a definition not searched for across the %d files changed in "
            "%s..%s of %s: %s",
            changed,
            base[:12],
            head[:12],
            self.cns,
            why,
        )

    async def _file_turnover(
        self, scope: code.IngestScope, base: str, old: str, head: str, now: str, found: Turnover
    ) -> None:
        """Add to `found` what the file `old` at `base`, `now` at `head` ('' = deleted),
        gained, lost and kept, by the name inside its module. Its text at `base` is
        fingerprinted only when it lost a name. A file still there that the ingest does
        not index adds nothing: none of its names is known to have left it."""

        def local(qualified_name: str, path: str) -> str:
            return unnumbered(local_name(qualified_name, path))

        names = await self._indexed_names(scope, head, now) if now else set()
        if names is None:
            return
        before = {local(name, old) for name in await self.names(base, old) or ()}
        after = {local(name, now) for name in names}
        here = await self.at(head, now) if after else None
        there = await self.at(base, old) if before - after else None
        if here is not None:
            new = [d for d in here.defs if local(d.qualified_name, now) not in before]
            kept = [d for d in here.defs if local(d.qualified_name, now) in before]
            found.new.append(Side(now, new, here.lines))
            found.kept.append(Side(now, kept, here.lines))
        if there is not None:
            gone = [d for d in there.defs if local(d.qualified_name, old) not in after]
            found.gone.append(Side(old, gone, there.lines))

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
