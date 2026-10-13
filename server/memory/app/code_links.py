"""Code-linked memories: link a memory to the symbols and files it names. Following the
links as the code changes is `code_link_recheck`; serving them, `code_link_reads`.

A memory is linked against the trunk code namespace of the repository its `repo:` tag
names, or of the code namespace it is written in (the team's file rules). Whether a
definition is there, and its fingerprint, is what tree-sitter finds in the file at the
repository's indexed head in the bare mirror; the index only proposes candidates. The
rules are `server.code.domain.anchor`.

A memory whose repository cannot be read yet waits behind a `*` marker, and the queue
moves on.
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import AbstractAsyncContextManager
from typing import Any

from server.code.app import memory_links as code
from server.code.domain.anchor import (
    Definition,
    Mention,
    bare_name,
    mentions,
    names_symbol,
    resolve,
)
from server.code.domain.declaration import fingerprint_version
from server.memory.app import code_link_pass as link_pass
from server.memory.app.code_link_reads import (
    SERVED,
    code_namespace_for,
    inputs_now,
    memory_inputs,
)
from server.memory.store import code_links as store
from server.memory.store.code_links import LinkRow
from server.settings import settings

# names and links read of one memory at most
_MAX_MENTIONS = 64
_MAX_LINKS = 32
# the name of a link that waits for its repository's first index
AWAITING_INDEX = "*"


async def link_queued(
    after: store.QueuePosition | None, limit: int, result: link_pass.PassResult
) -> store.QueuePosition | None:
    """Link the memories queued after `after`: a new or rewritten memory gets the links
    its text and files name, a deleted or no longer served one loses them. Returns the
    cursor: the last position read, or the read's cutoff when nothing was queued."""
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
    stored = {tuple(str(h[c]) for c in store.IDENTITY) for h in heads}
    gone = await store.for_identities([i for i in idents if i not in stored])
    await store.insert(store.tombstones(gone))
    result.unlinked += len(gone)
    await link_memories(heads, result)
    return idents[-1] if len(idents) == limit else None


async def _link_org(
    org: str, rows: list[dict[str, Any]], result: link_pass.PassResult, *, rereads: int = 2
) -> None:
    """Link one org's memories. A repository that cannot be read parks its memories
    behind the `*` marker, as before its first index: every pass retries them, until the
    marker expires, and the queue moves on.
    Links are last-write-wins, and a worker that read an older value of a memory may
    write after the one that linked its newer value; so each writer re-reads the heads it
    linked and relinks those rewritten since it read them, `rereads` times at most."""
    existing: dict[str, list[LinkRow]] = defaultdict(list)
    for a in await store.for_memories(org, [r["mid"] for r in rows]):
        existing[a["memory_id"]].append(a)
    todo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stale: list[LinkRow] = []
    deleted: set[str] = set()
    for r in rows:
        links = existing.get(r["mid"], [])
        if r.get("tombstoned_at") is not None:
            if links:
                deleted.add(r["mid"])  # its links go outright, not as tombstones
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
    if deleted:
        result.unlinked += await store.drop_gone(org, deleted)
    out = store.tombstones(stale)
    result.unlinked += len(stale)
    linked: list[str] = []
    for cns, memories in todo.items():
        files = link_pass.Files(org, cns)
        try:
            rows_out, gone, unparsed = await link_each(org, cns, memories, existing, files)
        except Exception as e:  # noqa: BLE001 — one repository's failure never stops the pass
            result.errors.append(f"link {org}/{cns}: {e}")
            out += [_awaiting(r, cns) for r in memories]
            continue
        if unparsed:
            result.errors.append(f"link {org}/{cns}: a file did not parse")
            out += [_awaiting(r, cns) for r in unparsed]
            waiting = {r["mid"] for r in unparsed}
            memories = [r for r in memories if r["mid"] not in waiting]
        out += rows_out + store.tombstones(gone)
        result.linked += len(rows_out)
        result.unlinked += len(gone)
        linked += [r["mid"] for r in memories]
    await store.insert(out)
    if linked and rereads:
        read = {r["mid"]: r["inputs"] for memories in todo.values() for r in memories}
        await _relink_rewritten(org, linked, read, result, rereads - 1)


async def _relink_rewritten(
    org: str, linked: list[str], read: dict[str, str], result: link_pass.PassResult, rereads: int
) -> None:
    """Relink those of `linked` rewritten since they were read with inputs `read`: a
    writer of the newer value may have linked it before this one replaced its links."""
    heads = await store.memory_heads(org, linked)
    newer = [h for h in heads if inputs_now(org, h) != read.get(h["mid"])]
    if newer:
        await _link_org(org, newer, result, rereads=rereads)


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
    files: link_pass.Files,
) -> tuple[list[LinkRow], list[LinkRow], list[dict[str, Any]]]:
    """`_link`, and the memories it could not link because a file they name does not
    parse; the others are linked all the same."""
    try:
        return *await _link(org, cns, memories, existing, files), []
    except code.ParseFailed:
        pass
    rows: list[LinkRow] = []
    gone: list[LinkRow] = []
    unparsed: list[dict[str, Any]] = []
    for m in memories:
        try:
            made, dropped = await _link(org, cns, [m], existing, files)
        except code.ParseFailed:
            unparsed.append(m)
            continue
        rows += made
        gone += dropped
    return rows, gone, unparsed


async def _link(
    org: str,
    cns: str,
    memories: list[dict[str, Any]],
    existing: dict[str, list[LinkRow]],
    files: link_pass.Files,
) -> tuple[list[LinkRow], list[LinkRow]]:
    """The link rows of `memories` (one repository's) at its indexed head, and their
    existing links that no longer hold. Before the repository's first index each memory
    gets a `*` marker instead, resolved once the index exists."""
    head = await code.indexed_head(org, cns)
    if not head:
        markers = [_awaiting(r, cns) for r in memories]
        old = [a for r in memories for a in existing.get(r["mid"], [])]
        return markers, [a for a in old if a["qualified_name"] != AWAITING_INDEX]
    # one past the cap: a memory with exactly the cap's mentions is not cut
    named = {
        r["mid"]: mentions(link_pass.text_of(r["value"]), cap=_MAX_MENTIONS + 1) for r in memories
    }
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
    refs = sorted({str(f) for r in memories for f in r.get("files") or []})
    paths = await code.live_paths(org, cns, refs) if refs else {}
    rows: list[LinkRow] = []
    gone: list[LinkRow] = []
    for r in memories:
        files_named = [str(f) for f in r.get("files") or []]
        links, waiting, complete = _plan(named[r["mid"]], found, files_named, paths)
        keep = set()
        for d in links:
            here = await at_head(files, head, d)
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


async def at_head(files: link_pass.Files, head: str, d: Definition) -> Definition | None:
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


# a cycle of the sweep through the link table starts at most this often
_SWEEP_EVERY_S = 3600


async def sweep_erased(
    cursor: dict[str, Any], limit: int, result: link_pass.PassResult
) -> dict[str, Any]:
    """Drop the links of gone memories in the next page of the link table, a page a pass,
    a new cycle an hour after the last; the cursor to store."""
    if time.time() - float(cursor.get("done_at", 0)) < _SWEEP_EVERY_S:
        return cursor
    try:
        nxt, dropped = await store.sweep_page(tuple(cursor.get("after") or ()) or None, limit)
    except Exception as e:  # noqa: BLE001 — the same page is swept next pass
        result.errors.append(f"sweep: {e}")
        return cursor
    result.unlinked += dropped
    return {"after": list(nxt)} if nxt else {"done_at": time.time()}


def queue_lease() -> AbstractAsyncContextManager[None]:
    """The lease on reading the memory queue; LeaseBusy while another host holds it."""
    return link_pass.repository_locks.lease("code_links/queue")
