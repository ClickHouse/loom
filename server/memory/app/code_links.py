"""Code-linked memories: link a memory to the symbols and files it names, follow the links
as the code changes, and serve each memory with the state of its code.

A memory is linked against the trunk code namespace of the repository its `repo:` tag
names, or of the code namespace it is written in (the team's file rules). Whether a
definition is there, and its fingerprint, is what tree-sitter finds in the file at the
repository's indexed head in the bare mirror; the index only proposes candidates. The
rules are `server.code.domain.anchor`; the policies `server.memory.domain.code_change`.

Loom never writes a memory: search leaves out one whose linked code is all gone, and
`revise` attaches a suggested correction to a changed link.

Work is acknowledged only once written: a memory whose repository failed waits behind
a `*` marker, a repository's checked head moves only once all its links were rechecked
there, and a link that changed under `revise` carries `policy_due` until it was judged.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import time
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, cast

from server.code.app import memory_links as code
from server.code.domain.anchor import (
    Definition,
    Link,
    Mention,
    bare_name,
    local_name,
    locate,
    mentions,
    names_symbol,
    recheck,
    resolve,
)
from server.code.domain.declaration import fingerprint_version
from server.code.domain.index.repos import repo_for_namespace
from server.code.domain.successor import Side, Successor, close_to, successor
from server.core.datetimes import naive_utc_now
from server.core.errors import PreconditionFailedError
from server.memory.app import code_link_judge
from server.memory.app.code_link_reads import (
    SERVED,
    code_namespace_for,
    current,
    inputs_now,
    memory_inputs,
)
from server.memory.domain import code_change
from server.memory.domain.code_change import REVISE
from server.memory.store import code_links as store
from server.memory.store.key_write_lock import KeyWriteLocks
from server.settings import settings

log = logging.getLogger(__name__)

_MAX_MENTIONS = 64
_MAX_LINKS = 32
_AWAITING_INDEX = "*"
# One host rechecks a repository at a time; the lease outlives the worker's pass budget.
_repository_locks = KeyWriteLocks(wait_s=1.0, ttl_s=900)
# recheck_all passes so far: each starts one repository further on
_passes = itertools.count()


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


@dataclass(frozen=True)
class _Read:
    """A link as a recheck read it: the range it is followed over, its file at the head
    and what is there, and what the search in that file found."""

    row: dict[str, Any]
    range: _Range | None
    before: Link
    now: str
    defs: list[Definition]
    renamed: Successor
    lost: bool  # not found in its file: what other files hold may be it


@dataclass
class Move:
    before: dict[str, Any]  # the link row
    after: Link


# The links a policy updated and those it left due, by (path, qualified_name).
_Revised = tuple[dict[tuple[str, str], dict[str, Any]], set[tuple[str, str]]]


@dataclass
class Budget:
    judgements: int


@dataclass(frozen=True)
class _Range:
    """The paths changed from `base` to a pass's head: path -> its path there ('' =
    deleted)."""

    base: str
    paths: dict[str, str]


# parsed files a pass keeps: a full recheck reads every linked file of a repository
_FILES_CACHED = 512
# a link not found keeps the commit it was last read at
_LOST_STATES = ("unresolved", "missing")


class _Recent[K, V](OrderedDict[K, V]):
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


class Files:
    """One repository's files and history, each read once per pass; parsed files while
    they are among the last `_FILES_CACHED` read."""

    def __init__(self, org: str, cns: str) -> None:
        self.org, self.cns = org, cns
        self._parsed: _Recent[tuple[str, str], code.FileAt | None] = _Recent(_FILES_CACHED)
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

    async def range(self, base: str, head: str) -> _Range | None:
        """None after a force-push or when git cannot tell."""

        async def read() -> _Range | None:
            kept = await code.kept_paths(self.org, self.cns, base, head)
            return None if kept is None else _Range(base, kept)

        return await self._once(("range", base, head), read)

    async def renamed_from(self, base: str, head: str, paths: list[str]) -> dict[str, str] | None:
        """Path at `head` -> its path at `base` for those of `paths` renamed since."""
        return await self._once(
            ("renamed", base, head, *paths),
            lambda: code.renamed_from(self.org, self.cns, base, head, paths),
        )

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

    async def descends(self, ancestor: str, descendant: str) -> bool:
        return await self._once(
            ("descends", ancestor, descendant),
            lambda: code.descends(self.org, self.cns, ancestor, descendant),
        )

    async def defs(self, commit: str, path: str, qualified_name: str) -> list[Definition]:
        f = await self.at(commit, path)
        return [d for d in f.defs if d.qualified_name == qualified_name] if f else []


def _claim(value: Any) -> str | None:
    """A stored value's text (the column holds JSON); None when it is not text."""
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        decoded = value
    return decoded if isinstance(decoded, str) else None


def _text(value: Any) -> str:
    claim = _claim(value)
    return claim if claim is not None else str(value)


def _link_of(row: dict[str, Any]) -> Link:
    return Link(
        path=str(row["path"]),
        qualified_name=str(row["qualified_name"]),
        body_hash=str(row.get("body_hash") or ""),
        checked_hash=str(row.get("checked_hash") or ""),
        state=str(row.get("state") or "fresh"),
        changed_since=str(row.get("changed_since") or ""),
        start_line=int(row.get("start_line") or 0),
        end_line=int(row.get("end_line") or 0),
        carried=bool(row.get("carried")),
        anchored_path=str(row.get("anchored_path") or ""),
        anchored_name=str(row.get("anchored_name") or ""),
        checked_path=str(row.get("checked_path") or ""),
    )


# ---- link -----------------------------------------------------------------------------


async def link_queued(
    after: store.QueuePosition | None, limit: int, result: PassResult
) -> store.QueuePosition | None:
    """Link the memories queued after `after`: a new or rewritten memory gets the links
    its text and files name, a deleted or no longer served one loses them. Returns the
    cursor: the last position read, or the read's cutoff when nothing was queued."""
    heads, positions, cutoff = await store.queued_memories(
        after, limit, lag_s=int(settings.memory_code_links_settle_seconds)
    )
    if not positions:
        return cutoff
    result.queued += len(positions)
    await link_memories(heads, result)
    return positions[-1]


async def link_memories(heads: list[dict[str, Any]], result: PassResult) -> None:
    """Link these memory heads, org by org."""
    by_org: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in heads:
        by_org[str(r["org"])].append(r)
    for org, org_rows in by_org.items():
        await _link_org(org, org_rows, result)


async def backfill(
    after: tuple[str, ...] | None, limit: int, result: PassResult
) -> tuple[str, ...] | None:
    """Link a page of the memories the queue did not deliver (the first pass, or a worker
    that fell behind the queue's TTL), and drop the links of memories no longer stored.
    Returns where the next page starts; None when the scan is done."""
    idents, heads = await store.backfill_page(after, limit, code.code_namespaces())
    stored = {tuple(str(h[c]) for c in store.IDENTITY) for h in heads}
    gone = await store.for_identities([i for i in idents if i not in stored])
    await store.insert(store.tombstones(gone))
    result.unlinked += len(gone)
    await link_memories(heads, result)
    return idents[-1] if len(idents) == limit else None


async def _link_org(
    org: str, rows: list[dict[str, Any]], result: PassResult, *, rereads: int = 2
) -> None:
    """Link one org's memories. A repository that cannot be read parks its memories
    behind the `*` marker, as before its first index: every pass retries them, until the
    marker expires, and the queue moves on.
    Links are last-write-wins, and a worker that read an older value of a memory may
    write after the one that linked its newer value; so each writer re-reads the heads it
    linked and relinks those its links no longer match, `rereads` times at most."""
    existing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in await store.for_memories(org, [str(r["mid"]) for r in rows]):
        existing[str(a["memory_id"])].append(a)
    todo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stale: list[dict[str, Any]] = []
    deleted: set[str] = set()
    for r in rows:
        links = existing.get(str(r["mid"]), [])
        if r.get("tombstoned_at") is not None:
            if links:
                deleted.add(str(r["mid"]))  # its links go outright, not as tombstones
            continue
        tags = [str(t) for t in r.get("tags") or []]
        cns = code_namespace_for(org, str(r["namespace"]), tags)
        if not cns and links and _names_a_repository(r, tags, links):
            continue  # a repository this host does not know: its links stay as last made
        r["inputs"] = memory_inputs(r, cns)
        served = r["status"] in SERVED
        if served and links and all(a["inputs_hash"] == r["inputs"] for a in links):
            continue  # a status or usage write: the links still hold
        names_code = bool(r.get("files")) or bool(mentions(_text(r["value"])))
        if cns and names_code and served:
            todo[cns].append(r)
        else:
            stale.extend(links)
    if deleted:
        result.unlinked += await store.drop_gone(org, deleted)
    out: list[dict[str, Any]] = store.tombstones(stale)
    result.unlinked += len(stale)
    linked: list[str] = []
    for cns, memories in todo.items():
        try:
            rows_out, gone = await _link(org, cns, memories, existing, Files(org, cns))
        except Exception as e:  # noqa: BLE001 — one repository's failure never stops the pass
            result.errors.append(f"link {org}/{cns}: {e}")
            out += [_awaiting(r, cns) for r in memories]
            continue
        out += rows_out + store.tombstones(gone)
        result.linked += len(rows_out)
        result.unlinked += len(gone)
        linked += [str(r["mid"]) for r in memories]
    await store.insert(out)
    if linked and rereads:
        await _link_org(org, await store.memory_heads(org, linked), result, rereads=rereads - 1)


def _names_a_repository(
    memory: dict[str, Any], tags: list[str], links: list[dict[str, Any]]
) -> bool:
    """Whether `memory` still names a repository: a `repo:` tag, or written in the code
    namespace its links are in."""
    in_code = any(a["code_namespace"] == memory["namespace"] for a in links)
    return in_code or any(t.startswith("repo:") for t in tags)


async def _link(
    org: str,
    cns: str,
    memories: list[dict[str, Any]],
    existing: dict[str, list[dict[str, Any]]],
    files: Files,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The link rows of `memories` (one repository's) at its indexed head, and their
    existing links that no longer hold. Before the repository's first index each memory
    gets a `*` marker instead, resolved once the index exists."""
    head = await code.indexed_head(org, cns)
    if not head:
        markers = [_awaiting(r, cns) for r in memories]
        old = [a for r in memories for a in existing.get(r["mid"], [])]
        return markers, [a for a in old if a["qualified_name"] != _AWAITING_INDEX]
    # one past the cap: a memory with exactly the cap's mentions is not cut
    named = {r["mid"]: mentions(_text(r["value"]), cap=_MAX_MENTIONS + 1) for r in memories}
    names = sorted({bare_name(m.name) for ms in named.values() for m in ms})
    found = await code.candidates(org, cns, names=names) if names else []
    refs = sorted({str(f) for r in memories for f in r.get("files") or []})
    paths = await code.live_paths(org, cns, refs) if refs else {}
    rows: list[dict[str, Any]] = []
    gone: list[dict[str, Any]] = []
    for r in memories:
        files_named = [str(f) for f in r.get("files") or []]
        links, waiting, complete = _plan(named[r["mid"]], found, files_named, paths)
        keep = set()
        for d in links:
            here = await _at_head(files, head, d)
            if here is None:
                if d.qualified_name:
                    waiting.append(d.qualified_name)  # the index is ahead of the head
                continue
            keep.add((cns, here.path, here.qualified_name))
            rows.append(
                _link_row(r, cns, here.path, here.qualified_name, head, d=here, complete=complete)
            )
        for m in dict.fromkeys(waiting):
            keep.add((cns, "", m))
            rows.append(_link_row(r, cns, "", m, head, state="pending", complete=complete))
        gone.extend(
            a
            for a in existing.get(r["mid"], [])
            if (str(a["code_namespace"]), str(a["path"]), str(a["qualified_name"])) not in keep
        )
    return rows, gone


def _awaiting(memory: dict[str, Any], cns: str) -> dict[str, Any]:
    """The `*` marker of a memory whose repository is not linked yet."""
    return _link_row(memory, cns, "", _AWAITING_INDEX, "", state="pending")


async def _at_head(files: Files, head: str, d: Definition) -> Definition | None:
    """`d` as the head has it: its first overload, or the file for a file link; None
    when the head has no such definition or file."""
    if d.qualified_name:
        group = await files.defs(head, d.path, d.qualified_name)
        return group[0] if group else None
    return d if await files.at(head, d.path) is not None else None


def _plan(
    names: list[Mention], defs: list[Definition], files: list[str], paths: dict[str, str]
) -> tuple[list[Definition], list[str], bool]:
    """(links, pending names, complete) for one memory: each name that resolves to one
    target, each named file that holds none of them, the qualified names trunk does not
    define yet; `complete` is False when the caps cut any of them."""
    found: dict[tuple[str, str], Definition] = {}
    waiting: list[str] = []
    cut = len(names) > _MAX_MENTIONS
    for m in names[:_MAX_MENTIONS]:
        same = [x for x in defs if x.name == bare_name(m.name)]
        d = resolve(m.name, same, files, in_prose=m.in_prose)
        if d is not None:
            found.setdefault((d.path, d.qualified_name), d)
        elif ("::" in m.name or "." in m.name) and not any(
            names_symbol(m.name, x.qualified_name) for x in same
        ):
            waiting.append(m.name)  # not defined yet; a name defined twice is never linked
    # A file link only where no symbol link is finer: a removed function must not be
    # kept alive by the file it lived in.
    held = {p for p, _ in found}
    for f in files:
        p = paths.get(f)
        if p and p not in held:
            found.setdefault((p, ""), Definition(path=p, qualified_name="", name=""))
    wanted = list(found.values()) + [Definition("", w, "") for w in waiting]
    complete = not cut and len(wanted) <= _MAX_LINKS
    kept = wanted[:_MAX_LINKS]
    return [d for d in kept if d.path], [d.qualified_name for d in kept if not d.path], complete


def _link_row(
    memory: dict[str, Any],
    cns: str,
    path: str,
    qualified_name: str,
    head: str,
    *,
    d: Definition | None = None,
    state: str = "fresh",
    complete: bool = True,
) -> dict[str, Any]:
    start, end = (d.start_line, d.end_line) if d else (0, 0)
    return store.link_row(
        org=str(memory["org"]),
        code_namespace=cns,
        path=path,
        qualified_name=qualified_name,
        namespace=str(memory["namespace"]),
        memory_id=str(memory["mid"]),
        memory_key=str(memory["memory_key"]),
        agent=str(memory["agent"]),
        user_id=str(memory["user_id"]),
        session_id=str(memory["session_id"]),
        inputs_hash=str(memory["inputs"]),
        complete=int(complete),
        body_hash=d.body_hash if d else "",
        checked_hash=d.body_hash if d else "",
        start_line=start,
        end_line=end,
        state=state,
        checked_commit=head,
        anchored_commit=head,
        anchored_path=path,
        anchored_hash=d.body_hash if d else "",
        anchored_name=qualified_name,
        fingerprint_version=fingerprint_version(),
    )


# ---- recheck --------------------------------------------------------------------------


# a cycle of the sweep through the link table starts at most this often
_SWEEP_EVERY_S = 3600


async def sweep_erased(cursor: str, limit: int, result: PassResult) -> str:
    """Drop the links of gone memories in the next page of the link table, a page a pass,
    a new cycle an hour after the last; the cursor to store."""
    state = json.loads(cursor or "{}")
    if time.time() - float(state.get("done_at", 0)) < _SWEEP_EVERY_S:
        return cursor
    try:
        nxt, dropped = await store.sweep_page(tuple(state.get("after") or ()) or None, limit)
    except Exception as e:  # noqa: BLE001 — the same page is swept next pass
        result.errors.append(f"sweep: {e}")
        return cursor
    result.unlinked += dropped
    return json.dumps({"after": list(nxt)} if nxt else {"done_at": time.time()})


async def recheck_all(result: PassResult) -> None:
    """Follow every code namespace's links from the head they were checked at to its
    indexed head, settle its pending names, then run the policy of every memory a change
    left due. A repository that fails is retried from the same head next pass."""
    # a repository no longer configured keeps its links as last checked
    repositories = [(o, c) for o, c in await store.code_namespaces() if repo_for_namespace(o, c)]
    # each pass starts one repository further on: those last in line are not always last
    start = next(_passes) % max(1, len(repositories))
    repositories = repositories[start:] + repositories[:start]
    # each repository an even share of the judgements left, what one leaves going to the
    # next: one with a backlog starves no other, and the pass spends no more than the cap
    left = int(settings.memory_code_links_revise_per_pass)
    for i, (org, cns) in enumerate(repositories):
        rest = len(repositories) - i
        granted = (left + rest - 1) // rest
        budget = Budget(judgements=granted)
        try:
            async with _repository_locks.hold_name(_lock_name(org, cns)):
                await _recheck_repository(org, cns, budget, result)
        except PreconditionFailedError:
            pass  # another host is checking this repository
        except Exception as e:  # noqa: BLE001 — one repository's failure never stops the pass
            result.errors.append(f"recheck {org}/{cns}: {e}")
        left -= granted - budget.judgements


def queue_lease() -> AbstractAsyncContextManager[None]:
    """The lease on reading the memory queue; PreconditionFailedError while another
    host holds it."""
    return _repository_locks.hold_name("code_links/queue")


def _lock_name(org: str, cns: str) -> str:
    return "code_links/" + hashlib.sha256(f"{org}\x1f{cns}".encode()).hexdigest()[:40]


async def _recheck_repository(org: str, cns: str, budget: Budget, result: PassResult) -> None:
    """One repository's recheck, settled names and policies, under its lease."""
    files = Files(org, cns)
    head = await code.indexed_head(org, cns)
    checked = await code.checked_head(org, cns)
    moves = await _resolve_pending(org, cns, head, files, result)
    await _write_moves(await _current_moves(org, moves), head)
    result.rechecked += len(moves)
    await _refingerprint(org, cns, files, result)
    await _retry_unparsed(org, cns, head, files, result)
    if head and head != checked:
        cut_at = str(
            json.loads(await code.link_cursor(_progress(org, cns)) or "{}").get("head", "")
        )
        if cut_at and cut_at != head and not await files.has(cut_at):
            # gone with a rewrite: what that pass already wrote is in no diff, so every
            # link is rechecked, as after a force-push
            await code.set_link_cursor(_progress(org, cns), "")
            await code.set_checked_head(org, cns, "")  # a cut full recheck resumes as one
            checked = ""
        elif cut_at and cut_at != head:
            # a pass the deadline cut at another head finishes there first, so every
            # head is followed in turn and no move it made is left behind
            await _follow(org, cns, checked, cut_at, files, result)
            await code.set_checked_head(org, cns, cut_at)
            checked = cut_at
        await _follow(org, cns, checked, head, files, result)
        await code.set_checked_head(org, cns, head)
    await _apply_policies(org, cns, head, files, budget, result)


def _progress(org: str, cns: str) -> str:
    """The cursor a repository's recheck records its head and last path done in."""
    return f"recheck/{org}/{cns}"


# paths rechecked between two writes: a pass the deadline cuts keeps what it did
_PATHS_PER_WRITE = 50
# files of namesakes a lost link is weighed against: past it, the answer is unsure
_MAX_ELSEWHERE = 32
# versions of its file a lost link's code is looked for in: past it, likewise
_MAX_EDITS = 32
# (org, repository, start, path, name) -> (the commit that removed its code from its
# file, the last one it was in), the last `_ANCHORS_KEPT` used
_anchors: _Recent[tuple[str, ...], tuple[str, str]] = _Recent(4096)
# links re-fingerprinted per write after the fingerprint rules or grammars changed
_REFINGERPRINT_PAGE = 500


async def _refingerprint(org: str, cns: str, files: Files, result: PassResult) -> None:
    """Re-hash the links hashed under other rules or grammars than these, a page at a
    time, each page written as it is done: a pass the deadline cuts resumes with the
    rest, and the repository follows new commits only after it, so no hash is compared
    across rules."""
    version = fingerprint_version()
    while rows := await store.stale_fingerprints(org, cns, version, limit=_REFINGERPRINT_PAGE):
        rehashed = []
        for a in rows:
            fields = await _rehashed(a, files) if a["qualified_name"] else {}
            rehashed.append((a, fields, _owes(a, fields)))
        revised = await _revised_namespaces([a for a, _, owed in rehashed if owed])
        out = []
        for a, fields, owed in rehashed:
            if owed or "state" in fields:
                fields["policy_due"] = int(owed and (a["org"], a["namespace"]) in revised)
            out.append(
                store.link_row(a, follows=int(a["version"]), fingerprint_version=version, **fields)
            )
        await store.insert(out)
        result.rechecked += len(out)
        if len(rows) < _REFINGERPRINT_PAGE:
            break


async def _rehashed(a: dict[str, Any], files: Files) -> dict[str, Any]:
    """A symbol link's hashes, former baselines included, re-hashed under the current
    rules from the commits they were taken at, its judgement dropped, and its state set
    by what these rules see between the checked text and the baseline. No hash is ever
    compared with one made under other rules (`docs/concepts/memory.md`)."""

    async def read_at(commit: str, path: str, qualified_name: str) -> tuple[str, str] | None:
        """The definition's (hash, exact shape) at `commit`, ('', '') when it is not there;
        None when the mirror lost the commit."""
        if not commit or not await files.has(commit):
            return None
        try:
            found = await files.defs(commit, path, qualified_name)
        except code.ParseFailed:
            return None  # no more readable than a commit the mirror lost
        return (found[0].body_hash, found[0].shape.exact) if found else ("", "")

    link = _link_of(a)
    anchored = await read_at(str(a["anchored_commit"]), link.anchored_path, link.anchored_name)
    checked = await read_at(str(a["checked_commit"]), link.read_path, link.qualified_name)
    out: dict[str, Any] = {
        "anchored_hash": anchored[0] if anchored else "",
        "checked_hash": checked[0] if checked else "",
        "judged_hash": "",
        "former_hashes": [
            found[0] if (found := await read_at(*source)) else "" for source in _former_baselines(a)
        ],
    }
    # a check that matched the baseline read its text; else the text the baseline was
    # taken from, while these rules still see the rename that moved it as one
    rebased = await read_at(*_baseline_source(a)) if a.get("baseline_commit") else None
    if link.matched_baseline() and checked is not None and _agree(anchored, checked):
        out["body_hash"] = checked[0] or out["anchored_hash"]
    elif rebased is not None and rebased[0] and _agree(anchored, rebased):
        out["body_hash"] = rebased[0]
    else:
        out |= {"body_hash": out["anchored_hash"] or str(a["body_hash"]), "baseline_commit": ""}
    baseline = out["body_hash"]
    if a["state"] in ("fresh", "moved") and out["checked_hash"] not in ("", baseline):
        # these rules see a change the old ones did not
        out |= {"state": "changed", "changed_since": str(a["checked_commit"])}
    elif a["state"] == "changed" and not link.carried and out["checked_hash"] == baseline != "":
        # nor one they did: the checked text is the anchored one under these rules
        state = "moved" if link.moved_from_anchor(link.read_path, link.qualified_name) else "fresh"
        out |= {"state": state, "changed_since": "", "suggestion": ""}
    return out


def _baseline_source(a: dict[str, Any]) -> tuple[str, str, str]:
    """(commit, path, qualified name) of the text `a`'s baseline hashes: the anchor,
    unless a rename moved the baseline onto the renamed text."""
    if a.get("baseline_commit"):
        return str(a["baseline_commit"]), str(a["baseline_path"]), str(a["baseline_name"])
    return str(a["anchored_commit"]), str(a["anchored_path"]), str(a["anchored_name"])


def _former_baselines(a: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The source of each former identity's baseline, oldest first."""
    return list(
        zip(
            a.get("former_baseline_commits") or [],
            a.get("former_baseline_paths") or [],
            a.get("former_baseline_names") or [],
            strict=True,
        )
    )


def _owes(a: dict[str, Any], fields: dict[str, Any]) -> bool:
    """A re-hash leaves link `a` due for its policy, as a recheck would: it newly reads
    changed, or stays changed and was judged (a re-hash drops every judgement)."""
    if fields.get("state", a["state"]) != "changed":
        return False
    return "state" in fields or (bool(fields) and bool(a.get("judged_hash")))


def _agree(anchored: tuple[str, str] | None, checked: tuple[str, str]) -> bool:
    """Whether the checked text is still the anchored one under the current rules: the
    same hash or the same rename shape. True when either side cannot be read."""
    if not checked[0] or anchored is None:
        return True
    return anchored[0] == checked[0] or anchored[1] == checked[1] != ""


async def _follow(
    org: str, cns: str, checked: str, head: str, files: Files, result: PassResult
) -> None:
    """Recheck, path by path, the links `checked..head` may have changed (all of them
    without a range), writing every `_PATHS_PER_WRITE` paths and recording the last one
    done, so a cut pass resumes."""
    progress = _progress(org, cns)
    done = json.loads(await code.link_cursor(progress) or "{}")
    changed = await files.range(checked, head) if checked else None
    kept = changed.paths if changed is not None else None
    # old and new paths: a cut pass may have moved links to a new path already
    paths = None if kept is None else sorted(set(kept) | {p for p in kept.values() if p})
    rows = await store.for_code(org, [cns], paths=paths)
    if kept is not None:
        rows += await _lost(org, cns, set(kept.values()) - {""})
        # made at a head the index reached first: this range need not show how their
        # files changed since
        made = [c for c in await code.commits_after(org, cns, checked, head) if c != head]
        if made:
            rows += await store.for_code(org, [cns], checked_commits=made)
    unique = {(a["path"], a["qualified_name"], str(a["memory_id"])): a for a in rows}
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in unique.values():
        if a["path"]:
            by_path[str(a["path"])].append(a)
    after = str(done.get("after", "")) if done.get("head") == head else ""
    paths = [p for p in sorted(by_path) if p > after]
    unparsed: set[str] = set()
    for i in range(0, len(paths), _PATHS_PER_WRITE):
        part = paths[i : i + _PATHS_PER_WRITE]
        moves = await _recheck_paths(org, cns, by_path, part, changed, head, files, unparsed)
        await _write_moves(await _current_moves(org, moves), head)
        result.rechecked += len(moves)
        await code.set_link_cursor(progress, json.dumps({"head": head, "after": part[-1]}))
    await code.set_link_cursor(progress, "")
    if unparsed:
        result.errors.append(f"recheck {org}/{cns}: did not parse: {', '.join(sorted(unparsed))}")
        await _set_unparsed(org, cns, await _unparsed(org, cns) | unparsed)


async def _recheck_paths(
    org: str,
    cns: str,
    by_path: dict[str, list[dict[str, Any]]],
    paths: list[str],
    changed: _Range | None,
    head: str,
    files: Files,
    unparsed: set[str],
) -> list[Move]:
    """The moves of the links on `paths`. A path whose recheck reads a file that does
    not parse keeps its links as they were and joins `unparsed`, to be retried."""
    rows = [a for p in paths for a in by_path[p]]
    try:
        return await _recheck(org, cns, rows, changed, head, files)
    except code.ParseFailed:
        pass
    moves: list[Move] = []
    for p in paths:
        try:
            moves += await _recheck(org, cns, by_path[p], changed, head, files)
        except code.ParseFailed:
            unparsed.add(p)
    return moves


async def _retry_unparsed(org: str, cns: str, head: str, files: Files, result: PassResult) -> None:
    """Recheck the links on the paths a pass could not parse, each from the commit it was
    last checked at; a path that still does not parse is retried again next pass."""
    paths = await _unparsed(org, cns)
    if not paths or not head:
        return
    by_commit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in await store.for_code(org, [cns], paths=sorted(paths)):
        by_commit[str(a["checked_commit"])].append(a)
    still: set[str] = set()
    for commit, rows in by_commit.items():
        changed = await files.range(commit, head) if commit else None
        by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for a in rows:
            by_path[str(a["path"])].append(a)
        paths_now = sorted(by_path)
        moves = await _recheck_paths(org, cns, by_path, paths_now, changed, head, files, still)
        await _write_moves(await _current_moves(org, moves), head)
        result.rechecked += len(moves)
    if still:
        result.errors.append(f"recheck {org}/{cns}: did not parse: {', '.join(sorted(still))}")
    await _set_unparsed(org, cns, still)


async def _unparsed(org: str, cns: str) -> set[str]:
    return set(json.loads(await code.link_cursor(f"unparsed/{org}/{cns}") or "[]"))


async def _set_unparsed(org: str, cns: str, paths: set[str]) -> None:
    await code.set_link_cursor(f"unparsed/{org}/{cns}", json.dumps(sorted(paths)))


async def _lost(org: str, cns: str, paths: set[str]) -> list[dict[str, Any]]:
    """Every unresolved link (a rival candidate may have gone anywhere), and the missing
    ones whose name the index places in one of `paths`."""
    lost = await store.for_code(org, [cns], states=["missing", "unresolved"])
    unresolved = [a for a in lost if a["state"] == "unresolved"]
    missing = [a for a in lost if a["state"] == "missing" and a["qualified_name"]]
    names = sorted({bare_name(str(a["qualified_name"])) for a in missing})
    if not names or not paths:
        return unresolved
    found = {d.name for d in await code.candidates(org, cns, names=names) if d.path in paths}
    return unresolved + [a for a in missing if bare_name(str(a["qualified_name"])) in found]


async def _recheck(
    org: str,
    cns: str,
    rows: list[dict[str, Any]],
    changed: _Range | None,
    head: str,
    files: Files,
) -> list[Move]:
    """The moves of these links rechecked at `head`, each followed over `changed` or
    from its own commit."""
    found: list[_Read] = []
    lost: dict[str, str] = {}  # qualified name -> the path it was lost from
    for a in rows:
        checked, own = str(a["checked_commit"]), changed
        if changed is not None and checked not in ("", changed.base):
            if not await files.descends(checked, changed.base):
                # checked after the range starts: from its own commit, once head has it
                if await files.descends(head, checked):
                    continue
                own = await files.range(checked, head)
        before = _link_of(a)
        now = own.paths.get(before.path, before.path) if own else before.path
        here = await files.at(head, now) if now else None
        now, defs = (now, here.defs) if here is not None else ("", [])
        renamed, gone = Successor(None), False
        if before.qualified_name and locate(before, now, defs) is None:
            # a link already missing is not handed to a definition added since: only its
            # own name coming back, here or elsewhere, finds it again
            if defs and before.state != "missing":
                renamed = await _renamed(files, before, a, now, head)
            gone = renamed.definition is None
            if gone:
                lost[before.qualified_name] = before.read_path
        found.append(_Read(a, own, before, now, defs, renamed, gone))
    elsewhere = await _elsewhere(org, cns, lost)
    moves: list[Move] = []
    for r in found:
        renamed, far = r.renamed, []
        if r.lost and (candidates := elsewhere.get(r.before.qualified_name)):
            new = await _namesakes(files, r.before, r.row, candidates, r.range, head)
            if new is None and r.before.state != "missing":
                renamed = Successor(None, uncertain=True)  # cannot tell: a live link is unsure
            far = new or []
        # moved to another module with an edit: never for a link already missing, nor
        # one the same-file search left unsure
        moved_and_edited = (
            bool(far)
            and renamed.definition is None
            and not renamed.uncertain
            and r.before.state != "missing"
            and locate(r.before, r.now, r.defs, elsewhere=far) is None
        )
        if moved_and_edited:
            renamed = await _moved_and_edited(files, r.before, r.row, far, head)
        after = recheck(
            r.before,
            path_now=r.now,
            here=r.defs,
            renamed=renamed.definition,
            renamed_exactly=renamed.exact,
            elsewhere=far,
            uncertain=renamed.uncertain,
            carried=renamed.carried,
            head=head,
        )
        if after != r.before:
            moves.append(Move(r.row, after))
    return moves


async def _namesakes(
    files: Files,
    before: Link,
    a: dict[str, Any],
    candidates: list[Definition],
    changed: _Range | None,
    head: str,
) -> list[Definition] | None:
    """The namesakes in `candidates` (other files') that appeared after the lost link's
    code was last in its own file; None when that cannot be told, or when more than
    `_MAX_ELSEWHERE` files were touched since."""
    live = changed is not None and before.state not in _LOST_STATES
    start = changed.base if changed is not None and live else str(a["checked_commit"])
    local = local_name(before.qualified_name, before.read_path)
    anchor = await _last_in_place(files, before, start, head, local)
    touched = await files.touched(anchor, head) if anchor else None
    if anchor is None or touched is None:
        return None
    paths = {c.path for c in candidates if c.path in touched}
    if len(paths) > _MAX_ELSEWHERE:
        return None  # a name every test file defines, all of them edited: too many to weigh
    old_path = await files.renamed_from(anchor, head, sorted(paths)) if paths else {}
    if old_path is None:
        return None
    new = []
    for c in candidates:
        if c.path not in paths:
            continue
        origin = old_path.get(c.path, c.path)
        if origin != before.read_path and await _defines(files, anchor, origin, local):
            continue  # the name was there: another definition
        new += await files.defs(head, c.path, c.qualified_name)  # new, or its own file moved
    return new


async def _last_in_place(
    files: Files, before: Link, start: str, head: str, local: str
) -> str | None:
    """The last commit after `start` whose version of the link's file (followed through
    renames) still defines `local`: `head` when it still does; None when the history
    cannot tell."""
    key = (files.org, files.cns, start, before.read_path, local)
    if (known := _anchors.used(key)) is not None and await files.descends(known[0], head):
        return known[1]  # the commit that removed it is still in this history
    path, since = before.read_path, start
    while True:
        edits = await files.edits_of(since, head, path, local)
        if edits is None or len(edits) > _MAX_EDITS:
            return None  # too many versions to read: unsure
        for commit, parent in edits:
            if await _defines(files, commit, path, local):
                continue
            renamed = await files.range(parent, commit)
            moved_to = renamed.paths.get(path, "") if renamed else ""
            if moved_to and moved_to != path and await _defines(files, commit, moved_to, local):
                path, since = moved_to, commit  # the file renamed with it: follow it there
                break
            if not await _defines(files, parent, path, local):
                return None  # it went at a commit the filter missed: unsure
            _anchors.keep(key, (commit, parent))
            return parent
        else:
            # no commit read lost it: it is there, or it went in a way the filter missed
            return head if await _defines(files, head, path, local) else None


async def _defines(files: Files, commit: str, path: str, local: str) -> bool:
    """`path` at `commit` defines a name that is `local` inside its module."""
    names = await files.names(commit, path)
    return names is not None and any(local_name(qn, path) == local for qn in names)


async def _moved_and_edited(
    files: Files, before: Link, a: dict[str, Any], new: list[Definition], head: str
) -> Successor:
    """The one definition with its name in another module among `new` (namesakes new
    since the link was in place), edited as it moved: followed as changed when its text
    is close to the old, else (with several) unsure."""
    local = local_name(before.qualified_name, before.read_path)
    namesakes = list(
        {d.path: d for d in new if local_name(d.qualified_name, d.path) == local}.values()
    )
    if len(namesakes) != 1:
        return Successor(None, uncertain=bool(namesakes))
    (d,) = namesakes
    checked = str(a["checked_commit"])
    key = ("elsewhere", checked, before.read_path, before.qualified_name, head, d.path)
    if (known := files.successors.get(key)) is not None:
        return known
    old, here = await files.at(checked, before.read_path), await files.at(head, d.path)
    was = [x for x in old.defs if x.qualified_name == before.qualified_name] if old else []
    if old is None or here is None or not was:
        return Successor(None, uncertain=True)
    then, now = Side(before.read_path, old.defs, old.lines), Side(d.path, here.defs, here.lines)
    close = await asyncio.to_thread(close_to, was[0], d, then, now)
    files.successors[key] = Successor(d) if close else Successor(None, uncertain=True)
    return files.successors[key]


async def _renamed(files: Files, before: Link, a: dict[str, Any], now: str, head: str) -> Successor:
    """What the linked definition became at `head` in `now` when its name is gone there
    (`successor.successor`): read against the file as the link was last checked."""
    checked = str(a["checked_commit"])
    key = ("here", checked, before.read_path, before.qualified_name, head, now)
    if (known := files.successors.get(key)) is not None:
        return known
    if checked and not await files.has(checked):
        return Successor(None, uncertain=True)  # its last-read code went with a rewrite
    old = await files.at(checked, before.read_path)
    here = await files.at(head, now) if now else None
    was = [d for d in old.defs if d.qualified_name == before.qualified_name] if old else []
    if old is None or here is None or not was:
        return Successor(None)
    # comparing texts is CPU work: off the loop, once per definition and pass
    then, at_head = Side(before.read_path, old.defs, old.lines), Side(now, here.defs, here.lines)
    found = await asyncio.to_thread(successor, was[0], then, at_head)
    files.successors[key] = found
    return found


async def _elsewhere(org: str, cns: str, lost: dict[str, str]) -> dict[str, list[Definition]]:
    """qualified name -> the index's definitions that may be it in another file: the
    same qualified name, or the same name inside its module (moved into another
    module)."""
    out: dict[str, list[Definition]] = defaultdict(list)
    if not lost:
        return out
    local = {qn: local_name(qn, path) for qn, path in lost.items()}
    found = await code.candidates(org, cns, qualified_names=list(lost))
    found += await code.candidates(org, cns, names=sorted({bare_name(qn) for qn in lost}))
    for c in {(c.path, c.qualified_name): c for c in found}.values():
        for qn in lost:
            if c.qualified_name == qn or local_name(c.qualified_name, c.path) == local[qn]:
                out[qn].append(c)
    return out


async def _resolve_pending(
    org: str, cns: str, head: str, files: Files, result: PassResult
) -> list[Move]:
    """Every pass: drop pending names older than `memory_code_links_pending_days`; once
    the repository is indexed, relink the memories written before its first index and
    turn the pending names the head now defines into links."""
    rows = await store.pending(org, cns, days=int(settings.memory_code_links_pending_days))
    await store.insert(store.tombstones([r for r in rows if r["expired"]]))
    rows = [r for r in rows if not r["expired"]]
    if not head or not rows:
        return []
    awaiting = [r for r in rows if r["qualified_name"] == _AWAITING_INDEX]
    if awaiting:
        try:
            await _relink(org, cns, awaiting, files, result)
        except code.ParseFailed as e:
            result.errors.append(f"relink {org}/{cns}: {e}")  # they stay waiting

    names = [r for r in rows if r["qualified_name"] != _AWAITING_INDEX]
    if not names:
        return []
    wanted = sorted({bare_name(str(r["qualified_name"])) for r in names})
    defs = await code.candidates(org, cns, names=wanted)
    # the files each memory names pick between namesakes, as when it was first linked
    heads = await store.memory_heads(org, sorted({str(r["memory_id"]) for r in names}))
    named = {str(h["mid"]): [str(f) for f in h.get("files") or []] for h in heads}
    moves = []
    for r in names:
        m = str(r["qualified_name"])
        d = resolve(
            m, [x for x in defs if x.name == bare_name(m)], named.get(str(r["memory_id"]), [])
        )
        try:
            here = await _at_head(files, head, d) if d is not None else None
        except code.ParseFailed:
            continue  # stays pending: retried next pass
        if here is None:
            continue
        anchored: dict[str, Any] = {"anchored_commit": head, "anchored_path": here.path}
        anchored |= {"anchored_hash": here.body_hash, "anchored_name": here.qualified_name}
        anchored["baseline_commit"] = ""  # the baseline is the new anchor
        link = Link(
            path=here.path,
            qualified_name=here.qualified_name,
            body_hash=here.body_hash,
            start_line=here.start_line,
            end_line=here.end_line,
            checked_hash=here.body_hash,
        )
        moves.append(Move({**r, **anchored}, link))
    return moves


async def _relink(
    org: str, cns: str, markers: list[dict[str, Any]], files: Files, result: PassResult
) -> None:
    """Link the memories waiting behind a `*` marker (written before the repository's
    first index, or while it could not be read), dropping the links of their earlier
    values; their markers go in the same insert. Every row follows its marker's version:
    a relink the queue wrote since the marker was read is newer and stays."""
    ids = [str(m["memory_id"]) for m in markers]
    memories = await store.memory_heads(org, ids)
    served = [m for m in memories if m["status"] in SERVED and m.get("tombstoned_at") is None]
    for m in served:
        m["inputs"] = memory_inputs(m, cns)
    existing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in await store.for_memories(org, ids):
        if a["code_namespace"] == cns and a["qualified_name"] != _AWAITING_INDEX:
            existing[str(a["memory_id"])].append(a)
    rows, gone = await _link(org, cns, served, existing, files) if served else ([], [])
    read = {str(m["memory_id"]): int(m["version"]) for m in markers}
    for r in rows:
        r["version"] = read[str(r["memory_id"])] + 1
    await store.insert(store.tombstones(markers + gone) + rows)
    result.linked += len(rows)
    result.unlinked += len(gone)


async def _current_moves(org: str, moves: list[Move]) -> list[Move]:
    """The moves of links made from their memory as it is now: a link the queue replaced
    since the recheck read it is not moved back in (the policy step drops any that a
    race still lets through, as every move is due)."""
    if not moves:
        return []
    heads = {
        str(h["mid"]): h
        for h in await store.memory_heads(org, [str(m.before["memory_id"]) for m in moves])
    }
    out = []
    for m in moves:
        head = heads.get(str(m.before["memory_id"]))
        if head is not None and current(org, head, [m.before]):
            out.append(m)
    return out


# the earlier identities a link keeps: a branch may still have any of them
_FORMER = 16


_FORMER_COLUMNS = (
    "former_paths",
    "former_names",
    "former_hashes",
    "former_baseline_commits",
    "former_baseline_paths",
    "former_baseline_names",
)


def _former(a: dict[str, Any], moved_from: bool) -> dict[str, list[str]]:
    """The identities `a` had before, oldest first, each with the baseline it had there
    and where that baseline's text is, with the one it leaves when it moves."""
    out = {c: list(a.get(c) or []) for c in _FORMER_COLUMNS}
    if moved_from:
        source = _baseline_source(a)
        for column, value in zip(
            _FORMER_COLUMNS,
            (a["path"], a["qualified_name"], a.get("body_hash") or "", *source),
            strict=True,
        ):
            out[column].append(str(value))
    return {c: v[-_FORMER:] for c, v in out.items()}


def _baseline_moved(a: dict[str, Any], link: Link, head: str) -> dict[str, str]:
    """Where the moved link's baseline text is now: the renamed text at `head` when the
    move rebased the baseline onto it, else where it was."""
    if link.body_hash == str(a.get("body_hash") or "") or link.body_hash != link.checked_hash:
        return {}
    return {
        "baseline_commit": head,
        "baseline_path": link.path,
        "baseline_name": link.qualified_name,
    }


async def _write_moves(moves: list[Move], head: str) -> None:
    """One new version per moved link, above the one read (a newer write stays); a link
    whose key changed is replaced in the same insert."""
    rows: list[dict[str, Any]] = []
    gone: list[dict[str, Any]] = []
    revised = await _revised_namespaces([m.before for m in moves])
    for m in moves:
        a, link = m.before, m.after
        relinked = (link.path, link.qualified_name) != (a["path"], a["qualified_name"])
        if relinked:
            gone.append(a)
        moved_from = relinked and bool(a["path"])
        # a correction holds only for a changed method its class did not carry: the
        # method's text cannot show what changed around it
        corrected = link.state == "changed" and not link.carried
        rows.append(
            store.link_row(
                a,
                follows=int(a["version"]),
                path=link.path,
                qualified_name=link.qualified_name,
                body_hash=link.body_hash,
                checked_hash=link.checked_hash,
                state=link.state,
                changed_since=link.changed_since,
                start_line=link.start_line,
                end_line=link.end_line,
                carried=int(link.carried),
                # code not found: keep the commit it was last read at, so a later pass
                # (or a re-hash) still has the text the link was checked against
                checked_commit=(
                    str(a["checked_commit"])
                    if link.state in _LOST_STATES and a.get("checked_commit")
                    else head
                ),
                checked_path=link.checked_path,
                checked_at=naive_utc_now(),
                **_former(a, moved_from),
                **_baseline_moved(a, link, head),
                suggestion=a.get("suggestion", "") if corrected else "",
                judged_hash=a.get("judged_hash", "") if corrected else "",
                policy_due=int(
                    link.state == "changed"
                    and (a["org"], a["namespace"]) in revised
                    and (_edited(a, link) or bool(a.get("policy_due")))
                ),
            )
        )
    await store.insert(store.tombstones(gone) + rows)


def _edited(a: dict[str, Any], link: Link) -> bool:
    """The link reads changed for an edit not yet judged: newly, or edited again. A move
    or a shift of its lines is no edit."""
    return a["state"] != "changed" or str(a["checked_hash"]) != link.checked_hash


# ---- policy ---------------------------------------------------------------------------


async def _revised_namespaces(links: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """The (org, namespace) pairs among `links`' memories whose policy is `revise`."""
    by_org: dict[str, set[str]] = defaultdict(set)
    for a in links:
        by_org[str(a["org"])].add(str(a["namespace"]))
    out: set[tuple[str, str]] = set()
    for org, nss in by_org.items():
        policies = await store.code_change_policies(org, sorted(nss))
        out |= {(org, ns) for ns in nss if code_change.policy_of(policies.get(ns, "")) == REVISE}
    return out


async def _apply_policies(
    org: str, cns: str, head: str, files: Files, budget: Budget, result: PassResult
) -> None:
    """Run the policy of each memory a change in `cns` left due and clear the marks it
    settled; what fails or is past the judgement budget stays due."""
    due = await store.policy_due(org, cns)
    if not due:
        return
    links: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in await store.for_memories(org, due):
        if a["state"] != "pending":
            links[str(a["memory_id"])].append(a)
    heads = {str(h["mid"]): h for h in await store.memory_heads(org, due)}
    policies = await store.code_change_policies(
        org, sorted({h["namespace"] for h in heads.values()})
    )
    rows: list[dict[str, Any]] = []
    for mid in due:
        try:
            row = heads.get(mid)
            mine = links[mid]
            if row is not None:
                want = inputs_now(org, row)
                rows += store.tombstones([a for a in mine if a["inputs_hash"] != want])
                mine = [a for a in mine if a["inputs_hash"] == want]
            mine = [a for a in mine if a["code_namespace"] == cns]
            out: _Revised | None = {}, set()
            if row is not None and mine:
                policy = code_change.policy_of(policies.get(str(row["namespace"]), ""))
                out = await _revise(org, row, mine, policy, head, files, budget, result)
            if out is None:
                continue  # deferred: stays due
            updated, left = out
            if updated:
                now = await store.memory_heads(org, [mid])
                latest = now[0] if now else None
                if latest is None or len(current(org, latest, mine)) != len(mine):
                    continue  # rewritten meanwhile: its links are relinked first
                if not _judgeable(latest):
                    updated = {}  # held or labelled high while the last judgement ran
            rows += [
                store.link_row(
                    updated.get(k, a), follows=int(a["version"]), policy_due=int(k in left)
                )
                for a in mine
                for k in [(a["path"], a["qualified_name"])]
            ]
        except Exception as e:  # noqa: BLE001 — one memory's failure never stops the pass
            result.errors.append(f"policy {org}/{mid}: {e}")
    await store.insert(rows)


async def _revise(
    org: str,
    row: dict[str, Any],
    links: list[dict[str, Any]],
    policy: str,
    head: str,
    files: Files,
    budget: Budget,
    result: PassResult,
) -> _Revised | None:
    """The `revise` policy for one memory: the links it updated and those it left due
    (past the budget), by (path, qualified_name); None when the whole memory is deferred
    to the next pass. Each changed definition is judged on its own."""
    changed = [
        a
        for a in links
        if a["state"] == "changed" and a["qualified_name"] and int(a.get("policy_due") or 0)
    ]
    if policy != code_change.REVISE or not changed or not _judgeable(row):
        return {}, set()
    claim = _claim(row["value"])
    if claim is None:
        return {}, set()
    updated: dict[tuple[str, str], dict[str, Any]] = {}
    due: set[tuple[str, str]] = set()
    for a in changed:
        key = (a["path"], a["qualified_name"])
        if budget.judgements <= 0:
            due.add(key)
            continue
        # a judgement awaits the model: the memory may have changed since it was read
        now = await store.memory_heads(org, [str(row["mid"])])
        if not now or now[0]["sha"] != row["sha"] or not _judgeable(now[0]):
            return {}, set()
        link = await _judge(files, claim, a, head, budget)
        if link is not None:
            updated[key] = link
            result.suggested += bool(link["suggestion"])
    return None if len(due) == len(changed) else (updated, due)


def _judgeable(row: dict[str, Any]) -> bool:
    """Whether the judge may read memory `row`: served, not held by a person, and not
    labelled `high`, whose value never goes to the judge's model."""
    return (
        row.get("tombstoned_at") is None
        and row["status"] in SERVED
        and not code_change.held(row)
        and row.get("sensitivity") != "high"
    )


async def _judge(
    files: Files, claim: str, link: dict[str, Any], head: str, budget: Budget
) -> dict[str, Any] | None:
    """`link` judged against the code it was written for and the code now, spending one
    of `budget`'s judgements; None when it is not judged: a method its renamed class
    carried (its text cannot show what changed in the class), either side gone, the same
    text on both, text past the judge's reach, or an unsure verdict."""
    if int(link.get("carried") or 0):
        return None
    path, qn = str(link["path"]), str(link["qualified_name"])
    if not await files.has(str(link["anchored_commit"])):
        return None  # the code it was written against went with a rewrite
    anchored = str(link["anchored_commit"]), str(link["anchored_path"])
    was = await files.defs(*anchored, str(link["anchored_name"]))
    group = await files.defs(head, path, qn)
    if not was or not group:
        return None
    # what each side's fingerprint covers, overloads and conditions with it
    old, new = was[0].shown, group[0].shown
    if old == new:
        return None  # the change is in nothing the judge would read
    if not code_link_judge.readable(claim, old, new):
        return None
    budget.judgements -= 1  # only a claim the judge reads costs one
    return await code_link_judge.judge(claim, link, old, new, group[0], head)
