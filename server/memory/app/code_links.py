"""Code-linked memories: link a memory to the symbols and files it names. Following the
links as the code changes is `code_link_recheck`; serving them, `code_link_reads`.

A memory is linked against the trunk code namespace of the repository its `repo:` tag
names, or of the code namespace it is written in (the team's file rules). Whether a
definition is there, and its fingerprint, is what tree-sitter finds in the file at the
repository's indexed head in the bare mirror; the index only proposes candidates. The
rules are `server.code.domain.anchor`.

A memory whose repository cannot be read yet waits behind a `*` marker, and the queue
moves on. A raw tool observation is the session's record, not a lesson about the code
its output quotes: it is never linked.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Any

from server.code.app import memory_links as code
from server.code.domain.anchor import (
    Definition,
    Mention,
    bare_name,
    cited_fingerprint,
    cited_names,
    mentions,
    names_symbol,
    resolve,
)
from server.code.domain.declaration import fingerprint_version
from server.core.ch import sequential_reads
from server.memory.app import code_link_pass as link_pass
from server.memory.app.code_link_reads import (
    SERVED,
    code_namespace_for,
    inputs_now,
    memory_inputs,
)
from server.memory.domain.code_link_keys import IDENTITY, QueuePosition
from server.memory.store import code_links as store
from server.memory.store.code_links import LinkRow
from server.settings import settings

# names and links read of one memory at most
_MAX_MENTIONS = 64
_MAX_LINKS = 32
# the name of a link that waits for its repository's first index
AWAITING_INDEX = "*"


async def link_queued(
    after: QueuePosition | None, limit: int, result: link_pass.PassResult
) -> QueuePosition | None:
    """Link the memories queued after `after`: a new or rewritten memory gets the links
    its text and files name; a deleted or no longer served one, and a raw tool
    observation, loses them. Returns the cursor: the last position read, or the read's
    cutoff when nothing was queued."""
    heads, positions, cutoff = await store.queued_memories(
        after, limit, code.code_namespaces(), lag_s=int(settings.memory_code_links_settle_seconds)
    )
    if not positions:
        return cutoff
    result.queued += len(positions)
    await link_memories(heads, result)
    return positions[-1]


async def link_memories(heads: list[dict[str, Any]], result: link_pass.PassResult) -> None:
    """Link these memory heads, org by org."""
    by_org: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in heads:
        by_org[str(r["org"])].append(r)
    for org, org_rows in by_org.items():
        await _link_org(org, org_rows, result)


async def backfill(
    after: tuple[str, ...] | None, limit: int, result: link_pass.PassResult
) -> tuple[str, ...] | None:
    """Link a page of the memories the queue did not deliver (the first pass, or a worker
    that fell behind the queue's TTL), and drop the links of memories no longer stored.
    Returns where the next page starts; None when the scan is done."""
    idents, heads = await store.backfill_page(after, limit, code.code_namespaces())
    stored = {tuple(str(h[c]) for c in IDENTITY) for h in heads}
    gone = await store.for_identities([i for i in idents if i not in stored])
    await store.insert(store.tombstones(gone))
    result.unlinked += len(gone)
    await link_memories(heads, result)
    return idents[-1] if len(idents) == limit else None


async def _link_org(
    org: str, rows: list[dict[str, Any]], result: link_pass.PassResult, *, rereads: int = 2
) -> None:
    """Link one org's memories, parking those of an unreadable repository behind the `*`
    marker; relink, up to `rereads` times, the ones rewritten while they were linked."""
    existing: dict[str, list[LinkRow]] = defaultdict(list)
    for a in await store.for_memories(org, [r["mid"] for r in rows]):
        existing[a["memory_id"]].append(a)
    todo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stale: list[LinkRow] = []
    unlinkable: set[str] = set()
    for r in rows:
        links = existing.get(r["mid"], [])
        if r.get("tombstoned_at") is not None or is_observation(r):
            if links:
                unlinkable.add(r["mid"])  # its links go outright, not as tombstones
            continue
        tags = [str(t) for t in r.get("tags") or []]
        cns = code_namespace_for(org, str(r["namespace"]), tags)
        if not cns and links and _names_a_repository(r, tags, links):
            continue  # a repository this host does not know: its links stay as last made
        r["inputs"] = memory_inputs(r, cns)
        served = r["status"] in SERVED
        if served and links and all(a["inputs_hash"] == r["inputs"] for a in links):
            continue  # a status or usage write: the links still hold
        names_code = bool(r.get("files")) or bool(mentions(link_pass.text_of(r["value"])))
        if cns and names_code and served:
            todo[cns].append(r)
        else:
            stale.extend(links)
    if unlinkable:
        result.unlinked += await store.drop_unlinkable(org, unlinkable)
    out = store.tombstones(stale)
    result.unlinked += len(stale)
    linked: list[str] = []
    for cns, memories in todo.items():
        files = link_pass.RepoReads(org, cns)
        try:
            rows_out, gone, unparsed = await link_each(org, cns, memories, existing, files)
        except Exception as e:  # noqa: BLE001 — one repository's failure never stops the pass
            result.failed(f"link {org}/{cns}: {e}", e, f"{cns}:link")
            out += [_awaiting(r, cns) for r in memories]
            continue
        if unparsed:
            paths = ", ".join(sorted({path for _, path in unparsed}))
            result.errors.append(f"link {org}/{cns}: did not parse: {paths}")
            out += [_awaiting(r, cns) for r, _ in unparsed]
            waiting = {r["mid"] for r, _ in unparsed}
            memories = [r for r in memories if r["mid"] not in waiting]
        out += rows_out + store.tombstones(gone)
        result.linked += len(rows_out)
        result.unlinked += len(gone)
        linked += [r["mid"] for r in memories]
    await _insert_unseen(out)
    if linked and rereads:
        read = {r["mid"]: r["inputs"] for memories in todo.values() for r in memories}
        await _relink_rewritten(org, linked, read, result, rereads - 1)


async def _insert_unseen(inserted: list[LinkRow]) -> None:
    """Insert `inserted`, a link a recheck pass may have missed parked as `pending` first,
    promoted if the fence reads clear it and parked again if they change after
    (docs/architecture/code-index-architecture.md)."""
    guard = [
        r for r in inserted if r["path"] and r["state"] != "pending" and r["tombstoned_at"] is None
    ]
    parked = {id(r): store.link_row(r, follows=r["version"], state="pending") for r in guard}
    await store.insert([parked.get(id(r), r) for r in inserted])
    by_repo: dict[tuple[str, str], list[LinkRow]] = defaultdict(list)
    for r in guard:
        by_repo[r["org"], r["code_namespace"]].append(r)
    promoted: list[tuple[LinkRow, tuple[str, str], str]] = []
    with sequential_reads():
        fences = {k: await _fence(*k) for k in by_repo}
        for key, rows in by_repo.items():
            running, checked = fences[key]
            promoted += [
                (store.link_row(r, follows=parked[id(r)]["version"]), key, checked)
                for r in rows
                if not running and checked in ("", r["checked_commit"])
            ]
        if not promoted:
            return
        await store.insert([row for row, _, _ in promoted])
        # a pass skips pending rows: one that began between the fence reads and the promotion
        # left these fresh at the old head, so read again and park what moved
        after = {k: await _fence(*k) for k in {key for _, key, _ in promoted}}
        back = [
            store.link_row(row, follows=row["version"], state="pending")
            for row, key, checked in promoted
            if after[key] != (False, checked)
        ]
    if back:
        await store.insert(back)


async def _fence(org: str, cns: str) -> tuple[bool, str]:
    """Whether a recheck pass is in flight, and the head its last pass checked."""
    return bool(await link_pass.recheck_cursor(org, cns).read()), await code.checked_head(org, cns)


async def _relink_rewritten(
    org: str, linked: list[str], read: dict[str, str], result: link_pass.PassResult, rereads: int
) -> None:
    """Relink those of `linked` rewritten since they were read with inputs `read`: a
    writer of the newer value may have linked it before this one replaced its links."""
    heads = await store.memory_heads(org, linked)
    newer = [h for h in heads if inputs_now(org, h) != read.get(h["mid"])]
    if newer:
        await _link_org(org, newer, result, rereads=rereads)


def is_observation(row: Mapping[str, Any]) -> bool:
    """Whether `row` (a memory head or a link) is of a raw tool observation: any memory
    whose key starts `obs:`, as `observe.record` writes it."""
    return str(row["memory_key"]).startswith("obs:")


def _names_a_repository(memory: dict[str, Any], tags: list[str], links: list[LinkRow]) -> bool:
    """Whether `memory` still names a repository: a `repo:` tag, or written in the code
    namespace its links are in."""
    in_code = any(a["code_namespace"] == memory["namespace"] for a in links)
    return in_code or any(t.startswith("repo:") for t in tags)


async def link_each(
    org: str,
    cns: str,
    memories: list[dict[str, Any]],
    existing: dict[str, list[LinkRow]],
    files: link_pass.RepoReads,
) -> tuple[list[LinkRow], list[LinkRow], list[tuple[dict[str, Any], str]]]:
    """`_link`, and the memories it could not link, each with the file it names that does
    not parse; the others are linked all the same."""
    try:
        return *await _link(org, cns, memories, existing, files), []
    except code.ParseFailed:
        pass
    rows: list[LinkRow] = []
    gone: list[LinkRow] = []
    unparsed: list[tuple[dict[str, Any], str]] = []
    for m in memories:
        try:
            made, dropped = await _link(org, cns, [m], existing, files)
        except code.ParseFailed as e:
            unparsed.append((m, e.path))
            continue
        rows += made
        gone += dropped
    return rows, gone, unparsed


async def _link(
    org: str,
    cns: str,
    memories: list[dict[str, Any]],
    existing: dict[str, list[LinkRow]],
    files: link_pass.RepoReads,
) -> tuple[list[LinkRow], list[LinkRow]]:
    """The link rows of `memories` (one repository's) at its indexed head, and their
    existing links that no longer hold. Before the repository's first index each memory
    gets a `*` marker instead, resolved once the index exists."""
    head = await code.indexed_head(org, cns)
    if not head:
        markers = [_awaiting(r, cns) for r in memories]
        old = [a for r in memories for a in existing.get(r["mid"], [])]
        return markers, [a for a in old if a["qualified_name"] != AWAITING_INDEX]
    named = await asyncio.to_thread(_memory_mentions, memories)
    # a word of the prose links only inside the memory's files
    names = sorted(
        {
            bare_name(m.name)
            for r in memories
            for m in named[r["mid"]]
            if not m.in_prose or r.get("files")
        }
    )
    found = await code.candidates(org, cns, names=names) if names else []
    by_name: dict[str, list[Definition]] = defaultdict(list)
    for d in found:
        by_name[d.name].append(d)
    refs = sorted({str(f) for r in memories for f in r.get("files") or []})
    paths = await code.live_paths(org, cns, refs) if refs else {}
    # Name matching also runs here when a pending pre-index marker is relinked.
    plans = await asyncio.to_thread(_memory_plans, memories, named, by_name, paths)
    rows: list[LinkRow] = []
    gone: list[LinkRow] = []
    for r, (links, waiting, complete) in zip(memories, plans, strict=True):
        keep = set()
        cited = cited_names(named[r["mid"]][:_MAX_MENTIONS])
        for d in links:
            here = await at_head(files, head, d, cited)
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
            if (a["code_namespace"], a["path"], a["qualified_name"]) not in keep
        )
    return rows, gone


def _awaiting(memory: dict[str, Any], cns: str) -> LinkRow:
    """The `*` marker of a memory whose repository is not linked yet."""
    return _link_row(memory, cns, "", AWAITING_INDEX, "", state="pending")


async def at_head(
    files: link_pass.RepoReads, head: str, d: Definition, cited: Sequence[str] = ()
) -> Definition | None:
    """`d` as the head has it: its first overload, or for a file link the file,
    fingerprinted by its lines that hold one of the `cited` names; None when the head
    has no such definition or file."""
    if d.qualified_name:
        group = await files.defs(head, d.path, d.qualified_name)
        return group[0] if group else None
    file = await files.at(head, d.path)
    if file is None:
        return None
    # matching lines is CPU work: off the loop
    return replace(d, body_hash=await asyncio.to_thread(cited_fingerprint, file.lines, cited))


@dataclass(frozen=True)
class FileLink:
    """A file link read at a head."""

    # the fingerprint of the file's lines that hold a name its memory cites
    cited: str
    # the definitions its memory names that the file holds there and the memory has no
    # link for: each gets one
    defined: list[Definition]
    # a finer link takes its place: one of `defined`, or one the memory has already
    replaced: bool


async def file_links(
    org: str,
    cns: str,
    links: Sequence[tuple[LinkRow, str]],
    head: str,
    files: link_pass.RepoReads,
) -> list[FileLink | None]:
    """Each of `links` (a link's row and its file's path at `head`) as a file link read
    there; None for a symbol link, and for a file link whose file or memory is gone. A
    name the memory has no link for resolves as when the memory is linked (`_plan`),
    among the index's candidates and the file's own definitions, to a definition the
    file holds at `head`."""
    mine = [(a, path) for a, path in links if not a["qualified_name"] and path]
    if not mine:
        return [None] * len(links)
    memories = {
        h["mid"]: h for h in await store.memory_heads(org, [a["memory_id"] for a, _ in mine])
    }
    # reading names, matching them and the lines that hold them is CPU work: off the loop
    named = await asyncio.to_thread(
        lambda: {
            mid: mentions(link_pass.text_of(h["value"]), cap=_MAX_MENTIONS)
            for mid, h in memories.items()
        }
    )
    names = sorted({bare_name(m.name) for found in named.values() for m in found})
    by_name: dict[str, list[Definition]] = defaultdict(list)
    for d in await code.candidates(org, cns, names=names) if names else []:
        by_name[d.name].append(d)
    read: list[tuple[dict[str, Definition], str] | None] = []
    for a, path in links:
        memory = memories.get(a["memory_id"]) if not a["qualified_name"] else None
        file = await files.at(head, path) if memory and path else None
        if memory is None or file is None:
            read.append(None)
            continue
        # the link followed its file here: it is one of the memory's files, whatever
        # path they name
        refs = [*(str(f) for f in memory.get("files") or []), path]
        read.append(
            await asyncio.to_thread(_file_link, named[a["memory_id"]], by_name, refs, file, path)
        )
    naming = [a["memory_id"] for (a, _), f in zip(links, read, strict=True) if f and f[0]]
    held: dict[str, list[LinkRow]] = defaultdict(list)
    for a in await store.for_memories(org, naming):
        if a["code_namespace"] == cns and a["qualified_name"]:
            held[a["memory_id"]].append(a)
    return [
        _beside(*f, held[a["memory_id"]]) if f else None
        for (a, _), f in zip(links, read, strict=True)
    ]


def _file_link(
    names: list[Mention],
    by_name: dict[str, list[Definition]],
    refs: list[str],
    file: code.FileAt,
    path: str,
) -> tuple[dict[str, Definition], str]:
    """Of a memory naming `names` and the files `refs`, read in `file` at `path`: the
    file's definitions it names, under the name it gives each, and the fingerprint of
    the lines it cites. `by_name` holds the index's candidates under their bare names."""
    named: dict[str, Definition] = {}
    for m in names:
        bare = bare_name(m.name)
        own = [x for x in file.defs if x.name == bare]
        target = resolve(m.name, [*by_name.get(bare, []), *own], refs, in_prose=m.in_prose)
        if target is None or target.path != path:
            continue
        # the file's own, fingerprinted: one only the index has is not at the head
        here = [x for x in own if x.qualified_name == target.qualified_name]
        if here:
            named[m.name] = here[0]
    return named, cited_fingerprint(file.lines, cited_names(names))


def _beside(named: dict[str, Definition], cited: str, held: list[LinkRow]) -> FileLink:
    """The file link of a memory that names the file's definitions `named` and has the
    symbol links `held`. A definition linked already keeps its link, and a name another
    link was made for (it names where that link was written) stays with that link: a
    commit never moves a link to a namesake."""
    at = {(h["path"], h["qualified_name"]) for h in held}
    new: dict[str, Definition] = {}
    for name, d in named.items():
        theirs = any(names_symbol(name, h["anchored_name"], h["anchored_path"]) for h in held)
        if (d.path, d.qualified_name) not in at and not theirs:
            new.setdefault(d.qualified_name, d)
    linked = any((d.path, d.qualified_name) in at for d in named.values())
    return FileLink(cited, list(new.values()), replaced=bool(new) or linked)


async def cited(org: str, memory_ids: Sequence[str]) -> dict[str, list[str]]:
    """The names each of these memories cites in a file it is linked to, by memory id."""
    if not memory_ids:
        return {}
    heads = await store.memory_heads(org, list(memory_ids))
    # reading names is CPU work: off the loop
    return await asyncio.to_thread(
        lambda: {
            h["mid"]: cited_names(mentions(link_pass.text_of(h["value"]), cap=_MAX_MENTIONS))
            for h in heads
        }
    )


def _memory_mentions(memories: list[dict[str, Any]]) -> dict[str, list[Mention]]:
    # A memory with exactly the cap's mentions fits.
    return {
        r["mid"]: mentions(link_pass.text_of(r["value"]), cap=_MAX_MENTIONS + 1) for r in memories
    }


def _memory_plans(
    memories: list[dict[str, Any]],
    named: dict[str, list[Mention]],
    by_name: dict[str, list[Definition]],
    paths: dict[str, str],
) -> list[tuple[list[Definition], list[str], bool]]:
    return [
        _plan(named[r["mid"]], by_name, [str(f) for f in r.get("files") or []], paths)
        for r in memories
    ]


def _plan(
    names: list[Mention],
    by_name: dict[str, list[Definition]],
    files: list[str],
    paths: dict[str, str],
) -> tuple[list[Definition], list[str], bool]:
    """(links, pending names, complete) for one memory, from the candidates `by_name`
    holds under their bare names: each name that resolves to one target, each named file
    that holds none of them, the qualified names trunk does not define yet; `complete`
    is False when the caps cut any of them."""
    found: dict[tuple[str, str], Definition] = {}
    waiting: list[str] = []
    cut = len(names) > _MAX_MENTIONS
    for m in names[:_MAX_MENTIONS]:
        same = by_name.get(bare_name(m.name), [])
        d = resolve(m.name, same, files, in_prose=m.in_prose)
        if d is not None:
            found.setdefault((d.path, d.qualified_name), d)
        elif ("::" in m.name or "." in m.name) and not any(
            names_symbol(m.name, x.qualified_name, x.path) for x in same
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
) -> LinkRow:
    start, end = (d.start_line, d.end_line) if d else (0, 0)
    return store.link_row(
        org=str(memory["org"]),
        code_namespace=cns,
        path=path,
        qualified_name=qualified_name,
        namespace=str(memory["namespace"]),
        memory_id=memory["mid"],
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


async def sweep_unlinkable(
    cursor: dict[str, Any], limit: int, result: link_pass.PassResult
) -> dict[str, Any]:
    """Drop the links of unlinkable memories (`store.sweep_page`) in the next page of the
    link table, a page a pass, a new cycle `memory_code_links_sweep_interval_s` after the
    last; the cursor to store."""
    if time.time() - float(cursor.get("done_at", 0)) < settings.memory_code_links_sweep_interval_s:
        return cursor
    try:
        nxt, dropped = await store.sweep_page(tuple(cursor.get("after") or ()) or None, limit)
    except Exception as e:  # noqa: BLE001 — the same page is swept next pass
        result.errors.append(f"sweep: {e}")
        return cursor
    result.unlinked += dropped
    return {"after": list(nxt)} if nxt else {"done_at": time.time()}


def queue_lease(*, wait_s: float | None = None) -> AbstractAsyncContextManager[None]:
    """The lease on reading the memory queue; LeaseBusy while another host holds it past
    `wait_s`."""
    return link_pass.repository_locks.lease("code_links/queue", wait_s=wait_s)
