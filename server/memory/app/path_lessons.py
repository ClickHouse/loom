"""The memories a code answer carries for the files it returns: each file's pre-edit rows
(`memory.for_file`'s listing: the rows that name the file and the rows linked to code in
it), merged across the files in their rank order and cut to a token cap. Read only; it
records no served row (a code-plane annotation is not a memory recall, `served_reads`).

Unlike the pre-edit block, a row that names the file only by another directory's path
ending in its filename (`loom_search/handler.py` for `search/handler.py`) is left out (on
every code answer, each `handler.py` would bring every other `handler.py`'s rows). Each
file reads memory.for_file's 50-row ceiling before that filter, over the namespaces of one
read ranked as memory.for_file ranks them (constraint, pinned, confidence, newest): 50
rows about other files of the same name ranked ahead of the file's own rows cut those."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, NamedTuple

from server.memory.api.models_for_file import MemoryForFileInput
from server.memory.app.code_plane_reads import PER_PATH_ROWS
from server.memory.app.for_file import CHARS_PER_TOKEN, listing_rows, value_text
from server.memory.domain.decision_schema import is_whole_row

# A row's excerpt is cut here, unless it is a decision or a constraint, which is served
# whole or not at all (a cut "never X unless Y" can say the opposite).
EXCERPT_CHARS = 200


def _excerpt(hit: dict[str, Any]) -> str | None:
    """The row's text on one line: whole for a decision or constraint (None when its
    stored value was cut, so it is not served), else cut at EXCERPT_CHARS."""
    one = " ".join(value_text(hit).split())
    if is_whole_row(str(hit.get("memory_type") or ""), hit.get("tags") or []):
        return None if hit.get("value_truncated") else one
    return one if len(one) <= EXCERPT_CHARS else one[: EXCERPT_CHARS - 1] + "…"


def _names_this_file(hit: dict[str, Any], path: str) -> bool:
    """Linked to code in the file, or naming it by its path, by a tail of it (a bare
    filename or `search/handler.py`), or, for a file below the root, by an absolute path
    ending in it. A longer relative path is another directory's file:
    `packages/web/src/index.ts` is not `src/index.ts`, and `frontend/Makefile` is not
    the root `Makefile`. An absolute path is trusted to name the file it ends in: the
    repository's root on the writer's machine is not known."""
    if hit.get("code_state"):
        return True
    return any(_names_path(f, path) for f in hit.get("matched_files") or [])


def _names_path(f: str, path: str) -> bool:
    if f == path or path.endswith("/" + f):
        return True
    return f.startswith("/") and "/" in path and f.endswith("/" + path)


def _tokens(lesson: dict[str, Any]) -> int:
    """The row's wire cost: its serialized JSON at 4 characters a token."""
    return len(json.dumps(lesson, ensure_ascii=False)) // CHARS_PER_TOKEN + 1


def _lesson(hit: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The row as a lesson, or None for a decision cut at the read (served whole or not
    at all)."""
    excerpt = _excerpt(hit)
    if excerpt is None:
        return None
    lesson = {
        "path": path,
        "memory_key": str(hit["memory_key"]),
        "namespace": str(hit["namespace"]),
        "excerpt": excerpt,
        "code_state": str(hit.get("code_state") or ""),
    }
    if hit.get("supersedes_key"):
        lesson["supersedes_key"] = str(hit["supersedes_key"])
    return lesson


class Lessons(NamedTuple):
    """The rows served, and how many rows about these files the cap, a cut or a newer row
    read here that supersedes them left out (memory.for_file lists them)."""

    rows: list[dict[str, Any]]
    left_out: int = 0


async def lessons_for_paths(
    org: str,
    namespaces: Sequence[str],
    trunk_ns: str,
    paths: Sequence[str],
    *,
    token_budget: int,
) -> Lessons:
    """The live memories of `namespaces` (each already checked readable) that name or are
    linked to code in `paths`, in path order, then each file's memory.for_file order (past
    two caller namespaces, each pair's rows after the pair before), each once, within
    `token_budget` estimated tokens of serialized rows. A row that another row read here
    supersedes is never served: the newer row names the first one it replaces in
    `supersedes_key`, and `memory.for_file` shows them. Each row: `path`, `memory_key`,
    `namespace`, `excerpt`, `code_state`: the worst state of its links to code in the file
    (`fresh`, `moved`, or `changed`, `unresolved` or `missing` when that code differs from
    what the memory was written against), '' for a row that names the file only; and
    `supersedes_key`, the key of the older row it replaces, when the conflict check linked
    one."""
    if not namespaces or not paths:
        return Lessons([])
    # memory.for_file takes two extra namespaces and follows code links only into the
    # trunk an extra one names: each read names the trunk and two caller namespaces.
    own = [ns for ns in namespaces if ns != trunk_ns] or [trunk_ns]
    groups = [own[i : i + 2] for i in range(0, len(own), 2)]
    reads = [
        MemoryForFileInput(
            org=org,
            namespace=group[0],
            path=p,
            top_k=PER_PATH_ROWS,
            extra_namespaces=([trunk_ns] if trunk_ns else []) + group[1:],
        )
        for p in paths
        for group in groups
    ]
    per_read = await asyncio.gather(*(listing_rows(inp) for inp in reads))
    # every row a row read here replaces, whichever file or read it came from
    superseded = {i for hits, _ in per_read for h in hits for i in h.get("supersedes_ids") or []}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    left: set[tuple[str, str]] = set()
    spent = 0
    for inp, (hits, _skipped) in zip(reads, per_read, strict=True):
        path = inp.path
        for hit in hits:
            key = (str(hit["namespace"]), str(hit["memory_key"]))
            if key in seen or not _names_this_file(hit, path):
                continue
            stale = str(hit.get("memory_id")) in superseded
            lesson = None if stale else _lesson(hit, path)
            cost = _tokens(lesson) if lesson else 0
            if lesson is None or spent + cost > token_budget:  # a shorter row may still fit
                left.add(key)
                continue
            seen.add(key)
            out.append(lesson)
            spent += cost
    return Lessons(out, len(left - seen))
