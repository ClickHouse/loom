"""The edits that bring memories up to renames their links followed: listed for
namespace.validate (`planned`), and applied by the link pass (`revise`).

A link in state `moved` is at its code, and its memory's text may still name the old
definition or path. This reads such a memory, its links and the index at the head, and
plans its edits (`server.memory.domain.code_rename`): the old and new token with their
positions and the hash to apply them against, or the one reason none is offered. A
memory whose text names nothing old is not listed.

The link pass applies a plan's edits as one revision (memory.update_by_id's write, on
the planned text's hash) where the memory is held in the repository's own code
namespace, nobody stands behind the text as it is and no other branch may still have
the old name (`_revisable`), and the write lets it change that memory alone
(`update.revise_as_pipeline`). Every other memory keeps its text, and a principal
applies its listed edits with memory.update and `if_match_sha256`.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException

from server.code.app import memory_links as code
from server.code.domain.anchor import Definition, bare_name, mentions
from server.code.domain.declaration import fingerprint_version
from server.code.domain.index.repos import configured_repos, repo_for_namespace
from server.core.errors import LoomServerError
from server.llm import customer
from server.memory.app import code_link_pass as link_pass
from server.memory.app import code_link_reads
from server.memory.app.code_unlinked import SCANNED
from server.memory.domain import code_change, code_rename
from server.memory.domain.provenance import pipeline_actor
from server.memory.domain.trust import attests, subject_of
from server.memory.store import code_links as store
from server.memory.store.code_links import LinkRow

# What `planned` counts a memory under when it offers edits; else under its reason.
OFFERED = "edits"
# Who the audit row of a revision `revise` makes names. The version stays its writer's.
ACTOR = "process:code_rename"
# The statuses memory.update_by_id's write refuses a revision with: the memory moved
# since it was planned, expired, is locked, was pinned, attested or retired since it was
# read, or the new value is refused or is close to another memory's. Its text stays.
_REFUSED = (404, 409, 422, 423)

# A memory to plan: its head and the links made from it as it is now.
_Linked = tuple[dict[str, Any], list[LinkRow]]
# What the index says of one repository: its definitions under their bare names, and
# the old paths its head no longer has.
_Index = tuple[dict[str, list[Definition]], set[str]]


async def planned(
    org: str, namespace: str, keys: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """One row per served memory of the namespace (under `keys` when given) that holds
    a `moved` link into a repository the caller may read and whose text names an old
    name or path, the newest checked first, of the `SCANNED` newest: its memory_key and
    memory_id, and either its `edits` (each a `start`, `end`, `old` and `new` over the
    text) with the `content_sha256` they are of, or the `reason` it gets none
    (`code_rename.REASONS` but `UNNAMED`). A high-sensitivity memory is not read: an
    edit quotes its text."""
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
    # planning is CPU work: off the loop
    return await asyncio.to_thread(_rows, memories, await _index(org, memories))


async def revise(org: str, head: dict[str, Any], links: Sequence[LinkRow]) -> bool:
    """Bring the text of memory `head` up to the renames its `links` followed, when its
    plan offers edits and it is `_revisable`: one revision through memory.update_by_id's
    write, on the hash of the text the edits were planned for. True when it was
    written. The write is the reviser's own in the audit log (`ACTOR`), and the model
    calls it makes (the embedding) are for the memory's writer. A revision the write
    refuses (`_REFUSED`) leaves the text; any other failure raises, a model call held
    for the writer (`customer.HOLDS`) among them."""
    from server.memory.app import update

    current = code_link_reads.current(org, head, list(links))
    if not any(a["state"] == "moved" for a in current) or not _revisable(org, head, current):
        return False
    text = link_pass.claim_of(head["value"])
    if text is None:
        return False
    memory: _Linked = (head, current)
    # planning is CPU work: off the loop
    plan = await asyncio.to_thread(_plan, memory, await _index(org, [memory]))
    if not plan.edits:
        return False
    namespace, writer = str(head["namespace"]), subject_of(str(head.get("actor") or ""))
    attribution = customer.for_org(
        org, namespace=namespace, subject=writer, user_id=str(head.get("user_id") or "")
    )
    try:
        with attribution, pipeline_actor(ACTOR):
            written = await update.revise_as_pipeline(
                org,
                namespace,
                str(head["mid"]),
                code_rename.revised(text, plan.edits),
                if_match_sha256=plan.sha256,
            )
    except (HTTPException, LoomServerError) as e:
        if e.status_code not in _REFUSED:
            raise
        return False
    return written.updated


def _revisable(org: str, head: dict[str, Any], links: Sequence[LinkRow]) -> bool:
    """Loom may revise the memory's text itself: a report may quote it, it is `active`
    (a `conflicted` one is already close to another memory), no person holds it (pinned,
    locked, verified), no principal attested the text as it is (a revision would leave
    the attestation stale), it is held in its repository's own code namespace, and that
    repository is indexed on one branch. A memory of any other namespace is linked by
    its `repo:` tag, which its writer set and which grants nothing: the new name would
    be served to readers who may hold no role on the repository. Another branch may
    still have the old name, and a memory linked again from its revised text keeps no
    link to it. The write checks the hold and the status again on the head it reads
    under the key's lock (`update.revise_as_pipeline`)."""
    attested = attests(head.get("verified_by"), head.get("verified_sha256"), head.get("sha"))
    active = _quotable(head) and head["status"] == "active"
    if not active or code_change.held(head) or attested:
        return False
    cns = links[0]["code_namespace"]
    if code.trunk_namespace(org, str(head["namespace"])) != cns:
        return False
    cfg = repo_for_namespace(org, cns)
    return cfg is not None and not any(
        c.org == org and c.repo == cfg.repo and c.namespace != cns for c in configured_repos()
    )


async def _index(org: str, memories: Sequence[_Linked]) -> dict[str, _Index]:
    """What the plans of `memories` read of each repository's index."""
    # reading names is CPU work: off the loop
    wanted = await asyncio.to_thread(_wanted, memories)
    index: dict[str, _Index] = {}
    for cns, (names, paths) in wanted.items():
        by_name: dict[str, list[Definition]] = defaultdict(list)
        for d in await code.candidates(org, cns, names=sorted(names)):
            by_name[d.name].append(d)
        live = await code.live_paths(org, cns, sorted(paths))
        index[cns] = by_name, {p for p in paths if live.get(p) != p}
    return index


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


def _plan(memory: _Linked, index: dict[str, _Index]) -> code_rename.Plan:
    head, links = memory
    by_name, gone = index[links[0]["code_namespace"]]
    text = link_pass.claim_of(head["value"])
    return code_rename.plan(text, head, links, by_name, gone=gone, rules=fingerprint_version())


def _rows(memories: Sequence[_Linked], index: dict[str, _Index]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for head, links in memories:
        plan = _plan((head, links), index)
        if plan.reason == code_rename.UNNAMED:
            continue  # its text holds as it is
        row: dict[str, Any] = {"memory_key": str(head["memory_key"]), "memory_id": str(head["mid"])}
        if plan.reason:
            row["reason"] = plan.reason
        else:
            row["edits"] = [e._asdict() for e in plan.edits]
            row["content_sha256"] = plan.sha256
        out.append(row)
    return out
