"""Why a served memory that names code holds no link to it, for namespace.validate.

A read shows a memory with no link exactly as one whose code is current. The link pass
(`code_links`) links a memory when it names a repository Loom indexes and one of its
names or files resolves on that repository's trunk; this reads, for each memory it did
not link, which of those did not hold: from the memory, the rows the pass stored for it
and the pass's own cursors. Nothing is resolved again.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from server.code.domain.anchor import mentions
from server.code.domain.index.repos import configured_repos
from server.memory.app import code_link_pass as link_pass
from server.memory.app.code_link_reads import (
    code_namespace_for,
    memory_inputs,
    readable_namespaces,
)
from server.memory.app.code_links import AWAITING_INDEX
from server.memory.domain.code_link_keys import IDENTITY
from server.memory.store import code_links as store

# The causes, in the order namespace.validate lists them.
NO_REPOSITORY = "no_repository"  # it names no indexed repository the caller may read
FILE_NOT_INDEXED = "file_not_indexed"  # it lists files, and nothing of it is on trunk
NAME_UNRESOLVED = "name_unresolved"  # it lists no file, and no name is one definition
PENDING = "pending"  # the link pass has yet to read it, or its code is not there yet
CAUSES = (NO_REPOSITORY, FILE_NOT_INDEXED, NAME_UNRESOLVED, PENDING)

# served memories read of a namespace, the last written first
SCANNED = 2000


def _names_code(memory: dict[str, Any]) -> bool:
    """`memory` lists files or carries a name the link pass would look up: a word of the
    prose counts only beside files, as it links only inside them."""
    if memory.get("files"):
        return True
    return any(not m.in_prose for m in mentions(link_pass.text_of(memory["value"])))


class _Pass:
    """How far the link pass has read: its queue position and its backfill cursor."""

    def __init__(self, queue: list[Any] | None, backfill: dict[str, Any]) -> None:
        # the queue time (µs) of the last write read; 0 before the first pass
        self.read_to = int(queue[0]) if queue else 0
        self.backfilled = bool(backfill.get("done"))
        # the last identity the backfill of earlier writes read
        self.backfill_at = tuple(str(v) for v in backfill.get("after") or ())

    @classmethod
    async def read(cls) -> _Pass:
        return cls(
            await link_pass.Cursor[list[Any] | None]("queue", None).read(),
            await link_pass.Cursor[dict[str, Any]]("backfill", {}).read(),
        )

    def has_read(self, org: str, memory: dict[str, Any]) -> bool:
        """The pass read `memory` as it is now: written before the queue position and,
        while the backfill runs, at or before the identity it has reached. A version is
        the write's wall-clock time in ns."""
        if not self.read_to or int(memory["version"]) // 1000 > self.read_to:
            return False
        ident = (org, *(str(memory[c]) for c in IDENTITY[1:]))
        return self.backfilled or ident <= self.backfill_at


async def unlinked(
    org: str, namespace: str, keys: Sequence[str] | None = None
) -> dict[str, list[dict[str, str]]]:
    """Cause -> the namespace's served memories that name code and hold no link made
    from them as they are now (a raw tool observation is never linked, and is not
    read), the last written first: each with its memory_key and
    memory_id and, under `pending`, what it `waits_for` (`index`: its repository's first
    index or a file that does not parse; `trunk`: a qualified name trunk does not define
    yet; `link_pass`: the pass to read it). Read of the memories under `keys` when given,
    of the `SCANNED` last written otherwise. A memory whose repository the caller may not
    read counts as naming none: its cause must not tell what that repository holds.
    Nothing is read, and nothing counted, in an org that indexes no repository."""
    out: dict[str, list[dict[str, str]]] = {cause: [] for cause in CAUSES}
    if not any(cfg.org == org for cfg in configured_repos()):
        return out  # the org indexes no repository: no memory of it could be linked
    linkable = await store.linkable_of_namespace(org, namespace, keys, limit=SCANNED)
    # reading names is CPU work: off the loop
    heads = await asyncio.to_thread(lambda: [h for h in linkable if _names_code(h)])
    if not heads:
        return out
    named = {h["mid"]: code_namespace_for(org, namespace, list(h.get("tags") or [])) for h in heads}
    readable = await readable_namespaces(org, set(named.values()) - {""})
    made: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in await store.link_states(
        org, namespace, [m for m, c in named.items() if c in readable]
    ):
        made[a["mid"]].append(a)
    read: _Pass | None = None
    for h in heads:
        cns = named[h["mid"]]
        row = {"memory_key": str(h["memory_key"]), "memory_id": str(h["mid"])}
        if cns not in readable:
            out[NO_REPOSITORY].append(row)
            continue
        want = memory_inputs(h, cns)
        mine = [a for a in made[h["mid"]] if a["inputs_hash"] == want]
        if any(a["state"] != "pending" for a in mine):
            continue  # linked
        if mine:
            waits = "index" if any(a["qualified_name"] == AWAITING_INDEX for a in mine) else "trunk"
            out[PENDING].append(row | {"waits_for": waits})
            continue
        read = read or await _Pass.read()
        if not read.has_read(org, h):
            out[PENDING].append(row | {"waits_for": "link_pass"})
        else:
            out[FILE_NOT_INDEXED if h.get("files") else NAME_UNRESOLVED].append(row)
    return out
