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
import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, NamedTuple

from server.code.domain.anchor import local_name
from server.memory.api.models_for_file import MemoryForFileInput
from server.memory.app.code_plane_reads import PER_PATH_ROWS
from server.memory.app.for_file import CHARS_PER_TOKEN, listing_rows, value_text
from server.memory.domain.decision_schema import CONSTRAINT_TAG, is_whole_row
from server.memory.domain.lemmatize import snowball_stem_tokens
from server.memory.domain.recall import query_tokens
from server.memory.store import code_links

logger = logging.getLogger(__name__)


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
    """The rows served and how many file lessons were left out (memory.for_file lists them)."""

    rows: list[dict[str, Any]]
    left_out: int = 0


class GrepMatchLines(NamedTuple):
    """Retained revisions of all grep matches and line numbers of unscoped matches."""

    unscoped: dict[str, set[int]]
    revisions: dict[str, set[str]]

    def excludes(self, link: code_links.LinkRow) -> bool:
        path = link["path"]
        revisions = self.revisions.get(path)
        if revisions is None:
            return True
        checked_commit = link["checked_commit"]
        if not checked_commit or revisions != {checked_commit}:
            return False
        lines = self.unscoped.get(path)
        if not lines:
            return True
        start, end = link["start_line"], link["end_line"]
        return 0 < start <= end and all(line > 0 and not start <= line <= end for line in lines)


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


def _excludes_other_symbol(hit: dict[str, Any], path: str, symbols: set[str]) -> bool:
    if hit.get("pinned") or is_whole_row(hit.get("memory_type", ""), hit.get("tags") or []):
        return False
    return _outside_scope(hit.get("code_symbols", []), path, symbols)


def _outside_scope(names: Sequence[str], path: str, symbols: set[str]) -> bool:
    anchors = [local_name(name, path) for name in names]
    # A chunk can hold a nested declaration without naming it as its own scope.
    return bool(anchors) and all(
        name
        and not any(
            name == scope or name.startswith(scope + ".") or scope.startswith(name + ".")
            for scope in symbols
        )
        for name in anchors
    )


def _focused_paths(
    candidates: list[tuple[str, dict[str, Any]]],
    matched_symbols: dict[str, set[str]],
    superseded: set[str],
) -> set[str]:
    """Focus a file only where the read found a whole lesson for a matched symbol."""
    return {
        path
        for path, hit in candidates
        if (symbols := matched_symbols.get(path))
        and str(hit.get("memory_id")) not in superseded
        and not hit.get("value_truncated")
        and any(name and local_name(name, path) in symbols for name in hit.get("code_symbols", []))
    }


async def _confirmed_omissions(
    org: str,
    trunk_ns: str,
    candidates: list[tuple[str, dict[str, Any]]],
    symbols: dict[str, set[str]],
    focused: set[str],
    timeout_s: float | None,
    grep_lines: GrepMatchLines | None,
) -> set[tuple[str, str]]:
    """Path reads miss pending links. Check only the rows being considered for omission."""
    omitted = {
        (path, mid)
        for path, hit in candidates
        if path in focused
        and (mid := str(hit.get("memory_id") or ""))
        and _excludes_other_symbol(hit, path, symbols[path])
    }
    if not omitted or not trunk_ns or (timeout_s is not None and timeout_s <= 0):
        return set()
    try:
        async with asyncio.timeout(timeout_s):
            links = await code_links.for_memories(org, sorted({mid for _, mid in omitted}))
    except Exception as error:  # noqa: BLE001 — without confirmation, keep the lessons
        logger.debug("Scope check unavailable for code lessons: %s", type(error).__name__)
        return set()
    resolved: dict[tuple[str, str], list[str]] = defaultdict(list)
    uncertain: set[str] = set()
    for link in links:
        if link["code_namespace"] != trunk_ns:
            continue
        if not link["complete"] or link["state"] not in ("fresh", "moved", "changed"):
            uncertain.add(link["memory_id"])
        elif grep_lines is not None and not grep_lines.excludes(link):
            uncertain.add(link["memory_id"])
        else:
            resolved[(link["path"], link["memory_id"])].append(link["qualified_name"])
    return {
        (path, mid)
        for path, mid in omitted
        if mid not in uncertain
        and _outside_scope(resolved.get((path, mid), []), path, symbols[path])
    }


async def lessons_for_paths(
    org: str,
    namespaces: Sequence[str],
    trunk_ns: str,
    paths: Sequence[str],
    *,
    token_budget: int,
    timeout_s: float | None = None,
    query: str = "",
    matched_symbols: dict[str, set[str]] | None = None,
    grep_lines: GrepMatchLines | None = None,
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
    preserving path and listing order on ties. Uncapped grep matches focus lessons on
    the returned definitions. Search focuses a file when a matched definition has a
    complete lesson. File-wide or uncertain links, decisions, constraints and pins
    remain eligible."""
    if not namespaces or not paths:
        return Lessons([])
    started = asyncio.get_running_loop().time()
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
    symbols = (matched_symbols or {}) if not token_budget else {}
    focused = (
        set(symbols) if grep_lines is not None else _focused_paths(candidates, symbols, superseded)
    )
    remaining = (
        None if timeout_s is None else timeout_s - (asyncio.get_running_loop().time() - started)
    )
    omitted = await _confirmed_omissions(
        org, trunk_ns, candidates, symbols, focused, remaining, grep_lines
    )
    for path, hit in _ordered_candidates(candidates, query):
        key = (str(hit["namespace"]), str(hit["memory_key"]))
        if key in seen:
            continue
        if (path, str(hit.get("memory_id") or "")) in omitted:
            left.add(key)
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
