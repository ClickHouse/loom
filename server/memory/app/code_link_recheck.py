"""Follow a repository's code-memory links from the commit they were last checked at to
its indexed head: moves, renames, edits, removals, pending names and parse failures."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
from collections import defaultdict
from dataclasses import dataclass

from server.code.app import memory_links as code
from server.code.domain.anchor import (
    Definition,
    Link,
    bare_name,
    local_name,
    locate,
    recheck,
    resolve,
)
from server.code.domain.declaration import fingerprint_version
from server.code.domain.index.repos import repo_for_namespace
from server.code.domain.successor import Side, Successor, close_to, successor
from server.core.datetimes import naive_utc_now
from server.memory.app import code_link_pass as link_pass
from server.memory.app import code_link_policy as policy
from server.memory.app import code_link_rehash as rehash
from server.memory.app import code_links as links
from server.memory.app.code_link_reads import (
    SERVED,
    current,
    memory_inputs,
)
from server.memory.store import code_links as store
from server.memory.store.code_links import Former, LinkRow, PendingLink
from server.memory.store.key_write_lock import LeaseBusy
from server.settings import settings

# recheck_all passes so far: each starts one repository further on
_passes = itertools.count()


@dataclass(frozen=True)
class _Read:
    """A link as a recheck read it: the range it is followed over, its file at the head
    and what is there, and what the search in that file found."""

    row: LinkRow
    range: link_pass.Range | None
    before: Link
    now: str
    defs: list[Definition]
    renamed: Successor
    lost: bool  # not found in its file: what other files hold may be it


@dataclass
class Move:
    """A link's stored row and the link it becomes."""

    row: LinkRow
    link: Link


# a link not found keeps the commit it was last read at
_LOST_STATES = ("unresolved", "missing")


async def recheck_all(result: link_pass.PassResult) -> None:
    """Follow every code namespace's links from the head they were checked at to its
    indexed head, settle its pending names, then run the policy of every memory a change
    left due. A repository that fails is retried from the same head next pass. Nothing
    while the code registry has never been read: which repositories are configured is
    not known yet."""
    from server.code.domain.index.repos import registry_unread

    if registry_unread():
        return
    # a repository no longer configured keeps its links as last checked
    repositories = [(o, c) for o, c in await store.code_namespaces() if repo_for_namespace(o, c)]
    # each pass starts one repository further on: those last in line are not always last
    start = next(_passes) % max(1, len(repositories))
    repositories = repositories[start:] + repositories[:start]
    # an even share of the judgements left each; what one leaves goes to the next
    left = int(settings.memory_code_links_revise_per_pass)
    for i, (org, cns) in enumerate(repositories):
        rest = len(repositories) - i
        granted = (left + rest - 1) // rest
        budget = link_pass.Budget(judgements=granted)
        try:
            async with link_pass.repository_locks.lease(_lock_name(org, cns)):
                await _recheck_repository(org, cns, budget, result)
        except LeaseBusy:
            pass  # another host is checking this repository
        except Exception as e:  # noqa: BLE001 — one repository's failure never stops the pass
            result.errors.append(f"recheck {org}/{cns}: {e}")
        left -= granted - budget.judgements


def _lock_name(org: str, cns: str) -> str:
    return "code_links/" + hashlib.sha256(f"{org}\x1f{cns}".encode()).hexdigest()[:40]


async def _recheck_repository(
    org: str, cns: str, budget: link_pass.Budget, result: link_pass.PassResult
) -> None:
    """One repository's recheck, settled names and policies, under its lease."""
    files = link_pass.RepoReads(org, cns)
    head = await code.indexed_head(org, cns)
    checked = await code.checked_head(org, cns)
    moves = await _resolve_pending(org, cns, head, files, result)
    await _write_moves(await _current_moves(org, moves), head)
    result.rechecked += len(moves)
    await rehash.refingerprint(org, cns, files, result)
    await _retry_unparsed(org, cns, head, files, result)
    if head and head != checked:
        cut_at = (await _progress(org, cns).read()).get("head", "")
        if cut_at and cut_at != head and not await files.has(cut_at):
            # gone with a rewrite: what that pass already wrote is in no diff, so every
            # link is rechecked, as after a force-push
            await _progress(org, cns).clear()
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
    await policy.apply_policies(org, cns, head, files, budget, result)


def _progress(org: str, cns: str) -> link_pass.Cursor[dict[str, str]]:
    """The cursor a repository's recheck records its head and last path done in."""
    return link_pass.Cursor(f"recheck/{org}/{cns}", {})


# paths rechecked between two writes: a pass the deadline cuts keeps what it did
_PATHS_PER_WRITE = 50


# days a lost link's name is looked for in other files
_LOST_SEARCHED_DAYS = 30


# files of namesakes a lost link is weighed against: past it, the answer is unsure
_MAX_ELSEWHERE = 32


# versions of its file a lost link's code is looked for in: past it, likewise
_MAX_EDITS = 32


# (org, repository, start, path, name) -> (the commit that removed its code from its
# file, the last one it was in), the last 4,096 used
_anchors: link_pass.Recent[tuple[str, ...], tuple[str, str]] = link_pass.Recent(4096)


async def _follow(
    org: str,
    cns: str,
    checked: str,
    head: str,
    files: link_pass.RepoReads,
    result: link_pass.PassResult,
) -> None:
    """Recheck, path by path, the links `checked..head` may have changed (all of them
    without a range), writing every `_PATHS_PER_WRITE` paths and recording the last one
    done, so a cut pass resumes."""
    progress = _progress(org, cns)
    done = await progress.read()
    changed = await files.range(checked, head) if checked else None
    kept = changed.paths if changed is not None else None
    # old and new paths: a cut pass may have moved links to a new path already
    paths = None if kept is None else sorted(set(kept) | {p for p in kept.values() if p})
    if paths is None:
        rows = await store.in_code_namespace(org, cns)
    else:
        rows = await store.for_paths(org, [cns], paths)
    if kept is not None:
        rows += await _lost(org, cns, set(kept.values()) - {""})
        # made at a head the index reached first: this range need not show how their
        # files changed since
        made = [c for c in await code.commits_after(org, cns, checked, head) if c != head]
        if made:
            rows += await store.for_commits(org, [cns], made)
    unique = {(a["path"], a["qualified_name"], a["memory_id"]): a for a in rows}
    by_path: dict[str, list[LinkRow]] = defaultdict(list)
    for a in unique.values():
        if a["path"]:
            by_path[a["path"]].append(a)
    after = str(done.get("after", "")) if done.get("head") == head else ""
    paths = [p for p in sorted(by_path) if p > after]
    unparsed, failed = await _unparsed(org, cns), set[str]()
    for i in range(0, len(paths), _PATHS_PER_WRITE):
        part = paths[i : i + _PATHS_PER_WRITE]
        known = len(unparsed)
        moves = await _recheck_paths(
            org, cns, by_path, part, changed, head, files, unparsed, failed
        )
        await _write_moves(await _current_moves(org, moves), head)
        result.rechecked += len(moves)
        if len(unparsed) > known:  # owed before the cursor moves past them
            await _set_unparsed(org, cns, unparsed)
        await progress.write({"head": head, "after": part[-1]})
    await progress.clear()
    if failed:
        result.errors.append(f"recheck {org}/{cns}: did not parse: {', '.join(sorted(failed))}")


async def _recheck_paths(
    org: str,
    cns: str,
    by_path: dict[str, list[LinkRow]],
    paths: list[str],
    changed: link_pass.Range | None,
    head: str,
    files: link_pass.RepoReads,
    unparsed: set[str],
    failed: set[str],
) -> list[Move]:
    """The moves of the links on `paths`. A path whose own file does not parse keeps its
    links as they were and joins `unparsed`, to be retried; the file read joins `failed`.
    So does the path of a link the re-hash could not bring to these rules: no hash is
    compared with one made under other rules."""
    version = fingerprint_version()
    unparsed |= {p for p in paths for a in by_path[p] if a["fingerprint_version"] != version}
    ours = {p: [a for a in by_path[p] if a["fingerprint_version"] == version] for p in paths}
    rows = [a for p in paths for a in ours[p]]
    try:
        return await _recheck(org, cns, rows, changed, head, files)
    except code.ParseFailed:
        pass
    moves: list[Move] = []
    for p in paths:
        try:
            moves += await _recheck(org, cns, ours[p], changed, head, files)
        except code.ParseFailed as e:
            unparsed.add(p)
            failed.add(e.path)
    return moves


async def _retry_unparsed(
    org: str, cns: str, head: str, files: link_pass.RepoReads, result: link_pass.PassResult
) -> None:
    """Recheck the links on the paths a pass could not parse, each from the commit it was
    last checked at; a path that still does not parse is retried again next pass."""
    paths = await _unparsed(org, cns)
    if not paths or not head:
        return
    by_commit: dict[str, list[LinkRow]] = defaultdict(list)
    for a in await store.for_paths(org, [cns], sorted(paths)):
        by_commit[a["checked_commit"]].append(a)
    still, failed = set[str](), set[str]()
    for commit, rows in by_commit.items():
        changed = await files.range(commit, head) if commit else None
        by_path: dict[str, list[LinkRow]] = defaultdict(list)
        for a in rows:
            by_path[a["path"]].append(a)
        paths_now = sorted(by_path)
        moves = await _recheck_paths(
            org, cns, by_path, paths_now, changed, head, files, still, failed
        )
        await _write_moves(await _current_moves(org, moves), head)
        result.rechecked += len(moves)
    if failed:
        result.errors.append(f"recheck {org}/{cns}: did not parse: {', '.join(sorted(failed))}")
    await _set_unparsed(org, cns, still)


async def _unparsed(org: str, cns: str) -> set[str]:
    return set(await link_pass.Cursor[list[str]](f"unparsed/{org}/{cns}", []).read())


async def _set_unparsed(org: str, cns: str, paths: set[str]) -> None:
    await link_pass.Cursor[list[str]](f"unparsed/{org}/{cns}", []).write(sorted(paths))


async def _lost(org: str, cns: str, paths: set[str]) -> list[LinkRow]:
    """The links lost for at most `_LOST_SEARCHED_DAYS`: every unresolved one (a rival
    candidate may have gone anywhere), and the missing ones whose name the index places
    in one of `paths`. One lost longer is rechecked only when its own file changes."""
    lost = await store.lost(org, cns, days=_LOST_SEARCHED_DAYS)
    unresolved = [a for a in lost if a["state"] == "unresolved"]
    missing = [a for a in lost if a["state"] == "missing" and a["qualified_name"]]
    names = sorted({bare_name(a["qualified_name"]) for a in missing})
    if not names or not paths:
        return unresolved
    found = {d.name for d in await code.candidates(org, cns, names=names) if d.path in paths}
    return unresolved + [a for a in missing if bare_name(a["qualified_name"]) in found]


async def _recheck(
    org: str,
    cns: str,
    rows: list[LinkRow],
    changed: link_pass.Range | None,
    head: str,
    files: link_pass.RepoReads,
) -> list[Move]:
    """The moves of these links rechecked at `head`, each followed over `changed` or
    from its own commit."""
    found: list[_Read] = []
    lost: dict[str, str] = {}  # qualified name -> the path it was lost from
    for a in rows:
        checked, own = a["checked_commit"], changed
        if changed is not None and checked not in ("", changed.base):
            if not await files.is_ancestor(checked, changed.base):
                # checked after the range starts: followed from its own commit
                if await files.is_ancestor(head, checked):
                    continue  # checked at or after head: nothing to do
                own = await files.range(checked, head)
        before = link_pass.link_of(a)
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
    files: link_pass.RepoReads,
    before: Link,
    a: LinkRow,
    candidates: list[Definition],
    changed: link_pass.Range | None,
    head: str,
) -> list[Definition] | None:
    """The namesakes in `candidates` (other files') that appeared after the lost link's
    code was last in its own file; None when that cannot be told (a file read does not
    parse), or when more than `_MAX_ELSEWHERE` files were touched since."""
    try:
        return await _new_namesakes(files, before, a, candidates, changed, head)
    except code.ParseFailed:
        return None


async def _new_namesakes(
    files: link_pass.RepoReads,
    before: Link,
    a: LinkRow,
    candidates: list[Definition],
    changed: link_pass.Range | None,
    head: str,
) -> list[Definition] | None:
    live = changed is not None and before.state not in _LOST_STATES
    start = changed.base if changed is not None and live else a["checked_commit"]
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
    files: link_pass.RepoReads, before: Link, start: str, head: str, local: str
) -> str | None:
    """The last commit after `start` whose version of the link's file (followed through
    renames) still defines `local`: `head` when it still does; None when the history
    cannot tell."""
    key = (files.org, files.cns, start, before.read_path, local)
    if (known := _anchors.used(key)) is not None and await files.is_ancestor(known[0], head):
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


async def _defines(files: link_pass.RepoReads, commit: str, path: str, local: str) -> bool:
    """`path` at `commit` defines a name that is `local` inside its module."""
    names = await files.names(commit, path)
    return names is not None and any(local_name(qn, path) == local for qn in names)


async def _moved_and_edited(
    files: link_pass.RepoReads, before: Link, a: LinkRow, new: list[Definition], head: str
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
    checked = a["checked_commit"]
    key = ("elsewhere", checked, before.read_path, before.qualified_name, head, d.path)
    if (known := files.successors.get(key)) is not None:
        return known
    try:
        old, here = await files.at(checked, before.read_path), await files.at(head, d.path)
    except code.ParseFailed:
        return Successor(None, uncertain=True)
    was = [x for x in old.defs if x.qualified_name == before.qualified_name] if old else []
    if old is None or here is None or not was:
        return Successor(None, uncertain=True)
    then, now = Side(before.read_path, old.defs, old.lines), Side(d.path, here.defs, here.lines)
    close = await asyncio.to_thread(close_to, was[0], d, then, now)
    files.successors[key] = Successor(d) if close else Successor(None, uncertain=True)
    return files.successors[key]


async def _renamed(
    files: link_pass.RepoReads, before: Link, a: LinkRow, now: str, head: str
) -> Successor:
    """What the linked definition became at `head` in `now` when its name is gone there
    (`successor.successor`): read against the file as the link was last checked."""
    checked = a["checked_commit"]
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
    by_local: dict[str, list[str]] = defaultdict(list)
    for qn, path in lost.items():
        by_local[local_name(qn, path)].append(qn)
    found = await code.candidates(org, cns, qualified_names=list(lost))
    found += await code.candidates(org, cns, names=sorted({bare_name(qn) for qn in lost}))
    for c in {(c.path, c.qualified_name): c for c in found}.values():
        same = [c.qualified_name] if c.qualified_name in lost else []
        for qn in dict.fromkeys(same + by_local.get(local_name(c.qualified_name, c.path), [])):
            out[qn].append(c)
    return out


async def _resolve_pending(
    org: str, cns: str, head: str, files: link_pass.RepoReads, result: link_pass.PassResult
) -> list[Move]:
    """Every pass: drop pending names older than `memory_code_links_pending_days`; once
    the repository is indexed, relink the memories written before its first index and
    turn the pending names the head now defines into links."""
    rows = await store.pending(org, cns, days=int(settings.memory_code_links_pending_days))
    await store.insert(store.tombstones([r for r in rows if r["expired"]]))
    rows = [r for r in rows if not r["expired"]]
    if not head or not rows:
        return []
    awaiting = [r for r in rows if r["qualified_name"] == links.AWAITING_INDEX]
    if awaiting:
        await _relink(org, cns, awaiting, files, result)

    names = [r for r in rows if r["qualified_name"] != links.AWAITING_INDEX]
    if not names:
        return []
    wanted = sorted({bare_name(r["qualified_name"]) for r in names})
    defs = await code.candidates(org, cns, names=wanted)
    by_name: dict[str, list[Definition]] = defaultdict(list)
    for x in defs:
        by_name[x.name].append(x)
    # the files each memory names pick between namesakes, as when it was first linked
    heads = await store.memory_heads(org, sorted({r["memory_id"] for r in names}))
    named = {h["mid"]: [str(f) for f in h.get("files") or []] for h in heads}
    moves = []
    for r in names:
        m = r["qualified_name"]
        d = resolve(m, by_name.get(bare_name(m), []), named.get(r["memory_id"], []))
        try:
            here = await links.at_head(files, head, d) if d is not None else None
        except code.ParseFailed:
            continue  # stays pending: retried next pass
        if here is None:
            continue
        anchored: PendingLink = {
            **r,
            "anchored_commit": head,
            "anchored_path": here.path,
            "anchored_hash": here.body_hash,
            "anchored_name": here.qualified_name,
            "baseline_commit": "",  # the baseline is the new anchor
            "fingerprint_version": fingerprint_version(),  # hashed just now
        }
        link = Link(
            path=here.path,
            qualified_name=here.qualified_name,
            body_hash=here.body_hash,
            start_line=here.start_line,
            end_line=here.end_line,
            checked_hash=here.body_hash,
        )
        moves.append(Move(anchored, link))
    return moves


async def _relink(
    org: str,
    cns: str,
    markers: list[PendingLink],
    files: link_pass.RepoReads,
    result: link_pass.PassResult,
) -> None:
    """Link the memories waiting behind a `*` marker (written before the repository's
    first index, or while it could not be read), dropping the links of their earlier
    values; their markers go in the same insert. Every row follows its marker's version:
    a relink the queue wrote since the marker was read is newer and stays."""
    ids = [m["memory_id"] for m in markers]
    memories = await store.memory_heads(org, ids)
    served = [m for m in memories if m["status"] in SERVED and m.get("tombstoned_at") is None]
    for m in served:
        m["inputs"] = memory_inputs(m, cns)
    existing: dict[str, list[LinkRow]] = defaultdict(list)
    for a in await store.for_memories(org, ids):
        if a["code_namespace"] == cns and a["qualified_name"] != links.AWAITING_INDEX:
            existing[a["memory_id"]].append(a)
    rows, gone, unparsed = (
        await links.link_each(org, cns, served, existing, files) if served else ([], [], [])
    )
    waiting = {m["mid"] for m, _ in unparsed}  # their markers stay
    read = {m["memory_id"]: m["version"] for m in markers}
    for r in rows:
        r["version"] = read[r["memory_id"]] + 1
    done: list[LinkRow] = [m for m in markers if m["memory_id"] not in waiting]
    await store.insert(store.tombstones(done + gone) + rows)
    result.linked += len(rows)
    result.unlinked += len(gone)


async def _current_moves(org: str, moves: list[Move]) -> list[Move]:
    """The moves of links made from their memory as it is now, still served: a link the
    queue replaced or dropped since the recheck read it is not moved back in."""
    if not moves:
        return []
    heads = {
        h["mid"]: h
        for h in await store.memory_heads(org, [m.row["memory_id"] for m in moves])
        if h["tombstoned_at"] is None and h["status"] in SERVED
    }
    out = []
    for m in moves:
        head = heads.get(m.row["memory_id"])
        if head is not None and current(org, head, [m.row]):
            out.append(m)
    return out


# the earlier identities a link keeps: a branch may still have any of them
_FORMER = 16


def _former(a: LinkRow, moved_from: bool) -> dict[str, list[Former]]:
    """The identities `a` had before, oldest first, with the one it leaves when it moves."""
    former = list(a["former"])
    if moved_from:
        commit, path, name = rehash.baseline_source(a)
        former.append(
            Former(
                path=a["path"],
                name=a["qualified_name"],
                hash=a["body_hash"],
                baseline_commit=commit,
                baseline_path=path,
                baseline_name=name,
            )
        )
    return {"former": former[-_FORMER:]}


def _baseline_moved(a: LinkRow, link: Link, head: str) -> dict[str, str]:
    """Where the moved link's baseline text is now: the renamed text at `head` when the
    move rebased the baseline onto it, else where it was."""
    if link.body_hash == a["body_hash"] or link.body_hash != link.checked_hash:
        return {}
    return {
        "baseline_commit": head,
        "baseline_path": link.path,
        "baseline_name": link.qualified_name,
    }


async def _write_moves(moves: list[Move], head: str) -> None:
    """One new version per moved link, above the one read (a newer write stays); a link
    whose key changed is replaced in the same insert."""
    rows: list[LinkRow] = []
    gone: list[LinkRow] = []
    revised = await policy.revised_namespaces([m.row for m in moves])
    for m in moves:
        a, link = m.row, m.link
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
                follows=a["version"],
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
                    a["checked_commit"]
                    if link.state in _LOST_STATES and a["checked_commit"]
                    else head
                ),
                checked_path=link.checked_path,
                checked_at=naive_utc_now(),
                **_former(a, moved_from),
                **_baseline_moved(a, link, head),
                suggestion=a["suggestion"] if corrected else "",
                judged_hash=a["judged_hash"] if corrected else "",
                policy_due=int(
                    link.state == "changed"
                    and (a["org"], a["namespace"]) in revised
                    and (_edited(a, link) or bool(a["policy_due"]))
                ),
            )
        )
    await store.insert(store.tombstones(gone) + rows)


def _edited(a: LinkRow, link: Link) -> bool:
    """The link reads changed for an edit not yet judged: newly, or edited again. A move
    or a shift of its lines is no edit."""
    return a["state"] != "changed" or a["checked_hash"] != link.checked_hash
