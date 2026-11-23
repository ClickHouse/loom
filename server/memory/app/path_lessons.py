"""The memories a code answer carries for the files it returns: each file's pre-edit rows
(`memory.for_file`'s listing: the rows that name the file and the rows linked to code in
it), merged across the files and cut to a shared token cap. Query matches follow
constraints and pins across all selected files; ties retain path and listing order.
Read only; it
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
from server.memory.domain.decision_schema import CONSTRAINT_TAG
from server.memory.domain.lemmatize import snowball_stem_tokens
from server.memory.domain.recall import query_tokens


def _excerpt(hit: dict[str, Any]) -> str | None:
    """Keep the whole fact; a cut condition or source reference cannot be trusted."""
    return None if hit.get("value_truncated") else " ".join(value_text(hit).split())


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
    """The row as a lesson, or None when its source value is incomplete."""
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
    if hit.get("code_changed_since"):
        lesson["changed_since"] = str(hit["code_changed_since"])
    if hit.get("supersedes_key"):
        lesson["supersedes_key"] = str(hit["supersedes_key"])
    return lesson


class Lessons(NamedTuple):
    """The rows served, and how many rows about these files the cap, a cut or a newer row
    read here that supersedes them left out (memory.for_file lists them)."""

    rows: list[dict[str, Any]]
    left_out: int = 0


def _ordered_candidates(
    candidates: list[tuple[str, dict[str, Any]]], query: str
) -> list[tuple[str, dict[str, Any]]]:
    """Rank the shared budget's candidates; ties retain path and listing order."""
    terms = set(snowball_stem_tokens(query_tokens(query)))
    if not terms:
        return candidates

    def rank(candidate: tuple[str, dict[str, Any]]) -> tuple[bool, bool, int]:
        hit = candidate[1]
        words = snowball_stem_tokens(query_tokens(value_text(hit)))
        return (
            CONSTRAINT_TAG not in (hit.get("tags") or []),
            not hit.get("pinned"),
            -len(terms.intersection(words)),
        )

    return sorted(candidates, key=rank)


async def lessons_for_paths(
    org: str,
    namespaces: Sequence[str],
    trunk_ns: str,
    paths: Sequence[str],
    *,
    token_budget: int,
    timeout_s: float | None = None,
    query: str = "",
) -> Lessons:
    """The live memories of `namespaces` (each already checked readable) that name or are
    linked to code in `paths`, in path order, then each file's memory.for_file order (past
    two caller namespaces, each pair's rows after the pair before), each once, within
    `token_budget` estimated tokens of serialized rows. A row that another row read here
    supersedes is never served: the newer row names the first one it replaces in
    `supersedes_key`, and `memory.for_file` shows them. Each row: `path`, `memory_key`,
    `namespace`, `excerpt`, `code_state`: the worst state of its links to code in the file
    (`fresh`, `moved`, or `changed`, `unresolved` or `missing` when that code differs from
    what the memory was written against), '' for a row that names the file only;
    `changed_since`, the commit that put its code in a `changed`, `unresolved` or `missing`
    state, when one is known; and `supersedes_key`, the key of the older row it replaces,
    when the conflict check linked one. `timeout_s` bounds the file reads together;
    failed or unfinished reads leave the completed files' rows intact. A nonempty `query`
    ranks matching excerpts after constraints and pins across the selected paths,
    preserving path and listing order on ties."""
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
    per_read = await _read_paths(reads, timeout_s)
    # every row a row read here replaces, whichever file or read it came from
    superseded = {i for hits, _ in per_read for h in hits for i in h.get("supersedes_ids") or []}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    left: set[tuple[str, str]] = set()
    spent = 0
    candidates = [
        (inp.path, hit)
        for inp, (hits, _skipped) in zip(reads, per_read, strict=True)
        for hit in hits
        if _names_this_file(hit, inp.path)
    ]
    for path, hit in _ordered_candidates(candidates, query):
        key = (str(hit["namespace"]), str(hit["memory_key"]))
        if key in seen:
            continue
        stale = str(hit.get("memory_id")) in superseded
        lesson = None if stale else _lesson(hit, path)
        cost = _tokens(lesson) if lesson else 0
        if lesson is None or (
            token_budget and spent + cost > token_budget
        ):  # a shorter row may still fit
            left.add(key)
            continue
        seen.add(key)
        out.append(lesson)
        spent += cost
    return Lessons(out, len(left - seen))


async def _read_paths(
    reads: Sequence[MemoryForFileInput], timeout_s: float | None
) -> list[tuple[list[dict[str, Any]], bool]]:
    """Keep completed file reads when another fails or runs past the shared bound."""
    tasks = [asyncio.create_task(listing_rows(inp)) for inp in reads]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout_s)
        return [
            task.result()
            if task in done and not task.cancelled() and task.exception() is None
            else ([], True)
            for task in tasks
        ]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
