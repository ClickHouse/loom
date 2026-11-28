"""The edits that bring a namespace's memories up to renames their links followed, for
namespace.validate.

A link in state `moved` is at its code, and its memory's text still names the old
definition or path. This reads each such memory, its links and the index at the head,
and plans its edits (`server.memory.domain.code_rename`): the old and new token with
their positions and the hash to apply them against, or the one reason none is offered.
Nothing is written: a principal applies an edit with memory.update and
`if_match_sha256`.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from server.code.app import memory_links as code
from server.code.domain.anchor import Definition, bare_name, mentions
from server.code.domain.declaration import fingerprint_version
from server.memory.app import code_link_pass as link_pass
from server.memory.app import code_link_reads
from server.memory.app.code_unlinked import SCANNED
from server.memory.domain import code_rename
from server.memory.store import code_links as store
from server.memory.store.code_links import LinkRow

# What `planned` counts a memory under when it offers edits; else under its reason.
OFFERED = "edits"

# A memory to plan: its head and the links made from it as it is now.
_Linked = tuple[dict[str, Any], list[LinkRow]]
# What the index says of one repository: its definitions under their bare names, and
# the old paths its head no longer has.
_Index = tuple[dict[str, list[Definition]], set[str]]


async def planned(
    org: str, namespace: str, keys: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """One row per served memory of the namespace (under `keys` when given) that holds
    a `moved` link into a repository the caller may read, the newest checked first, of
    the `SCANNED` newest: its memory_key and memory_id, and either its `edits` (each a
    `start`, `end`, `old` and `new` over the text) with the `content_sha256` they are
    of, or the `reason` it gets none (`code_rename.REASONS`). A high-sensitivity memory
    is not read: an edit quotes its text."""
    moved = await code_link_reads.stale_links(
        org, namespace, keys, limit=SCANNED, states=("moved",)
    )
    if not moved:
        return []
    ids = [str(a["memory_id"]) for a in moved]
    readable = {str(a["code_namespace"]) for a in moved}
    heads, rows = await asyncio.gather(store.memory_heads(org, ids), store.for_memories(org, ids))
    by_memory: dict[str, list[LinkRow]] = defaultdict(list)
    for a in rows:
        if a["code_namespace"] in readable:
            by_memory[a["memory_id"]].append(a)
    read = {str(h["mid"]): h for h in heads if _quotable(h)}
    memories: list[_Linked] = []
    for mid in ids:
        links = code_link_reads.current(org, read[mid], by_memory[mid]) if mid in read else []
        if any(a["state"] == "moved" for a in links):
            memories.append((read[mid], links))
    # reading names and planning are CPU work: off the loop
    wanted = await asyncio.to_thread(_wanted, memories)
    index: dict[str, _Index] = {}
    for cns, (names, paths) in wanted.items():
        by_name: dict[str, list[Definition]] = defaultdict(list)
        for d in await code.candidates(org, cns, names=sorted(names)):
            by_name[d.name].append(d)
        live = await code.live_paths(org, cns, sorted(paths))
        index[cns] = by_name, {p for p in paths if live.get(p) != p}
    return await asyncio.to_thread(_rows, memories, index)


def _quotable(head: dict[str, Any]) -> bool:
    """A served memory whose text a report may quote."""
    return (
        head["status"] in code_link_reads.SERVED
        and head.get("tombstoned_at") is None
        and head.get("sensitivity") != "high"
    )


def _wanted(memories: Sequence[_Linked]) -> dict[str, tuple[set[str], set[str]]]:
    """Code namespace -> what a plan reads of its index: the bare names its memories
    mention or are linked to, and the paths their links were written against."""
    out: dict[str, tuple[set[str], set[str]]] = defaultdict(lambda: (set(), set()))
    for head, links in memories:
        names, paths = out[links[0]["code_namespace"]]
        text = link_pass.claim_of(head["value"]) or ""
        names.update(bare_name(m.name) for m in mentions(text, cap=len(text)))
        for a in links:
            was_path, was_name = code_rename.written_as(a)
            names.update(bare_name(n) for n in (was_name, a["qualified_name"]) if n)
            paths.add(was_path)
    return out


def _rows(memories: Sequence[_Linked], index: dict[str, _Index]) -> list[dict[str, Any]]:
    rules = fingerprint_version()
    out: list[dict[str, Any]] = []
    for head, links in memories:
        by_name, gone = index[links[0]["code_namespace"]]
        plan = code_rename.plan(
            link_pass.claim_of(head["value"]), head, links, by_name, gone=gone, rules=rules
        )
        row: dict[str, Any] = {"memory_key": str(head["memory_key"]), "memory_id": str(head["mid"])}
        if plan.reason:
            row["reason"] = plan.reason
        else:
            row["edits"] = [e._asdict() for e in plan.edits]
            row["content_sha256"] = plan.sha256
        out.append(row)
    return out
