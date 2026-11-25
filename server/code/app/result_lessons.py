"""`lessons` on a code answer: the memories that name or are linked to the files it
returns, read after the op's own answer (outside code.search's shared memo, so a stored
or replayed answer never carries another caller's lessons), for this caller only.

An agent that looks at code through these tools gets the team's notes on that code with
no memory call of its own. The read is bounded (`code_enrich_timeout_s`, as code.symbol's
lessons are); a slow or failed file leaves completed files' lessons intact. A token
narrowed below its namespaces reads none (the memory rows are not filtered by user),
as code.symbol's lessons do.
`include_lessons: false` or `code_result_lessons` off skips the read. Search and
grep can focus a file's lessons on returned definitions. `code.symbol` reads the
lessons linked to the requested symbol itself."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from server.code.api.models_grep import CodeGrepInput, CodeGrepOutput, GrepDependency
from server.code.api.models_navigation import CodeEnclosingInput
from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
from server.code.app import common, enrich, memory_links
from server.code.app.search_legs import resolve_deadline_ms
from server.code.app.spans import code_span, span_attrs
from server.code.domain import deadline as code_deadline
from server.code.domain.anchor import local_name
from server.core.ch import caller_shaped
from server.identity.app import request_scope
from server.memory.app import code_plane_reads, path_lessons
from server.settings import settings

Handler = Callable[[Any], Awaitable[Any]]
PathsOf = Callable[[Any, Any], Iterable[str]]
logger = logging.getLogger(__name__)


def _result_paths(paths: Iterable[str]) -> list[str]:
    """Distinct result paths in order, spelled as the index stores them."""
    return list(dict.fromkeys(filter(None, map(common.repo_relative, paths))))


async def _grep_lesson_paths(
    org: str,
    namespaces: list[str],
    trunk: str,
    paths: list[str],
    symbols: dict[str, set[str]],
    grep_lines: path_lessons.GrepMatchLines,
    remaining: float,
) -> list[str]:
    limit = settings.code_result_lessons_paths
    if not trunk or not namespaces or not any(path in symbols for path in paths[limit:]):
        return paths[:limit]
    # Keep most of the shared deadline for reading the actual lessons.
    timeout_s = remaining / 3
    with code_span("enrich.lesson_paths", seeds=len(paths)) as sp, caller_shaped():
        linked = await enrich.bounded(
            path_lessons.paths_with_matched_links(
                org, namespaces, trunk, paths, symbols, grep_lines, timeout_s=timeout_s
            ),
            set(),
            seconds=timeout_s,
        )
        span_attrs(sp, results=len(linked))
    return sorted(paths, key=lambda path: path not in linked)[:limit]


def _searched_namespace(input: Any) -> str:
    """The code namespace the answer was read from: a one-entry `namespaces` list is
    searched in place of `namespace` (code.search's fan-out normalisation)."""
    names = list(getattr(input, "namespaces", None) or [])
    return str(names[0] if len(names) == 1 else input.namespace)


def _bound_s(input: Any, started: float) -> float:
    """Seconds the read may take: `code_enrich_timeout_s`, and no more than what is left
    of the operation's own `deadline_ms`."""
    bound = settings.code_enrich_timeout_s
    if isinstance(input, CodeSearchInput):
        budget = code_deadline.start(resolve_deadline_ms(input.deadline_ms), started=started)
        if budget is not None:
            bound = min(bound, budget.left_ms() / 1000)
    elif isinstance(input, CodeGrepInput):
        budget = code_deadline.start(input.deadline_ms, started=started)
        if budget is not None:
            bound = min(bound, budget.left_ms() / 1000)
    return bound


async def lessons_for(
    input: Any,
    paths: Iterable[str],
    started: float,
    *,
    query: str = "",
    matched_symbols: dict[str, set[str]] | None = None,
    grep_lines: path_lessons.GrepMatchLines | None = None,
) -> path_lessons.Lessons:
    """The completed lessons for the answer's top `paths`; none when off, narrowed,
    past the call's deadline, or unable to read the scope. `started` is the call's
    monotonic start."""
    offered_paths = _result_paths(paths)
    if not (input.include_lessons and settings.code_result_lessons and offered_paths):
        return path_lessons.Lessons([])
    if request_scope.request_narrowed():
        return path_lessons.Lessons([])  # a token that may not read every user's memories

    async def scope() -> tuple[str, list[str], str, bool]:
        org, ns = common.resolve_code_identity(input.org, _searched_namespace(input))
        trunk = memory_links.trunk_namespace(org, ns) or ns
        nss = await code_plane_reads.lesson_namespaces(org, ns, input.lesson_namespaces)
        # another repository's code namespace would bring its links to a same-named file
        nss = [n for n in nss if memory_links.trunk_namespace(org, n) in ("", trunk)]
        return org, nss, trunk if trunk in nss else "", ns == trunk

    bound = _bound_s(input, started)
    if bound <= 0:
        return path_lessons.Lessons([])
    with code_span("enrich.lessons"):
        read_started = time.monotonic()
        resolved = await enrich.bounded(scope(), None, seconds=bound)
        remaining = bound - (time.monotonic() - read_started)
        if resolved is None or remaining <= 0:
            return path_lessons.Lessons([])
        org, nss, trunk, on_trunk = resolved
        top = offered_paths[: settings.code_result_lessons_paths]
        if on_trunk and grep_lines is not None and matched_symbols:
            top = await _grep_lesson_paths(
                org, nss, trunk, offered_paths, matched_symbols, grep_lines, remaining
            )
            remaining = bound - (time.monotonic() - read_started)
            if remaining <= 0:
                return path_lessons.Lessons([])
        if isinstance(input, CodeSearchInput):
            query = input.query
        elif isinstance(input, CodeGrepInput):
            query = " ".join(input.pattern) if isinstance(input.pattern, list) else input.pattern
        try:
            return await path_lessons.lessons_for_paths(
                org,
                nss,
                trunk,
                top,
                token_budget=settings.code_result_lessons_tokens,
                timeout_s=remaining,
                matched_symbols=matched_symbols if on_trunk else None,
                grep_lines=grep_lines,
                query=query,
            )
        except Exception as e:  # noqa: BLE001 — lesson enrichment must not fail the code answer
            logger.debug("code lessons skipped: %s", e)
            return path_lessons.Lessons([])


class WithLessons:
    """`handler`, its answer carrying `lessons` for the files `paths_of(input, output)`
    names, best first."""

    def __init__(self, handler: Handler, paths_of: PathsOf) -> None:
        functools.update_wrapper(self, handler)
        self.handler = handler
        self.paths_of = paths_of

    async def __call__(self, input: Any) -> Any:
        started = time.monotonic()
        out = await self.handler(input)
        query = _enclosing_query(out) if isinstance(input, CodeEnclosingInput) else ""
        symbols = None
        grep_lines = None
        if isinstance(input, CodeGrepInput) and input.mode == "matches":
            dependencies = out.related.dependencies if out.related is not None else ()
            symbols, grep_lines = _grep_match_scopes(
                _grep_matches(out), out._match_commits, dependencies
            )
        elif isinstance(input, CodeSearchInput):
            symbols = _search_match_symbols(out)
        found = await lessons_for(
            input,
            self.paths_of(input, out),
            started,
            query=query,
            matched_symbols=symbols,
            grep_lines=grep_lines,
        )
        out.lessons, out.lessons_left_out = found.rows, found.left_out
        return out


def _grep_match_scopes(
    matches: Iterable[dict[str, Any]],
    commits: dict[tuple[str, int], str],
    dependencies: Iterable[GrepDependency],
) -> tuple[dict[str, set[str]], path_lessons.GrepMatchLines]:
    """Retain the scopes and revisions of hits and their verified dependencies."""
    symbols: dict[str, set[str]] = {}
    lines: dict[str, set[int]] = {}
    revisions: dict[str, set[str]] = {}
    for row in matches:
        path = str(row.get("path") or "")
        if not path:
            continue
        line = int(row.get("line") or 0)
        revisions.setdefault(path, set()).add(commits.get((path, line), ""))
        scope = local_name(str(row.get("scope") or ""), path) if row.get("scope_start_line") else ""
        if scope:
            symbols.setdefault(path, set()).add(scope)
        else:
            lines.setdefault(path, set()).add(line)
    for dependency in dependencies:
        if dependency._indexed_commit:
            scope = local_name(dependency.qualified_name, dependency.path)
            symbols.setdefault(dependency.path, set()).add(scope)
            revisions.setdefault(dependency.path, set()).add(dependency._indexed_commit)
    return symbols, path_lessons.GrepMatchLines(lines, revisions)


def _search_match_symbols(out: CodeSearchOutput) -> dict[str, set[str]]:
    """File-level hits grant wider scope than the same answer's named units."""
    return _symbols_by_path(
        (
            unit.path,
            unit.symbol
            if unit.start_line > 0
            and unit.symbol != unit.path
            and unit.kind in ("function", "symbol", "class_skeleton")
            and unit.granularity != "file"
            else "",
        )
        for unit in out.results
    )


def _symbols_by_path(rows: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    symbols: dict[str, set[str]] = {}
    for path, scope in rows:
        symbols.setdefault(path, set()).add(local_name(scope, path) if scope else "")
    return {path: names for path, names in symbols.items() if path and "" not in names}


# The files each code answer returns, best first.


def _grep_matches(out: CodeGrepOutput) -> list[dict[str, Any]]:
    return [*out.matches, *out.related.matches] if out.related is not None else out.matches


def grep_paths(input: CodeGrepInput, out: CodeGrepOutput) -> list[str]:
    if input.mode == "presence" or out.not_covered:
        return []
    rows = out.files if input.mode == "files" else _grep_matches(out)
    paths = [str(row.get("path") or "") for row in rows]
    if input.mode == "matches" and out.related is not None:
        paths.extend(d.path for d in out.related.dependencies if d._indexed_commit)
    return paths


def search_paths(input: Any, out: Any) -> list[str]:
    # An abstained search's results are not stood behind, and a search over several
    # repositories has no one team namespace: no lessons on either.
    if out.abstain or len(input.namespaces) > 1:
        return []
    return [u.path for u in out.results]


def edge_paths(_input: Any, out: Any) -> list[str]:
    # The call sites' files: for callers, where the answer points the agent next; for
    # callees, the seed's own file, where the calls are made.
    return [e.path for e in out.edges]


def impact_paths(_input: Any, out: Any) -> list[str]:
    # the seed's own files first: the code the agent is about to change
    impacted = [str(r.get("path") or "") for r in out.impacted_symbols]
    return [*out._paths, *impacted]


def outline_paths(_input: Any, out: Any) -> list[str]:
    return [out.path]


def enclosing_paths(_input: Any, out: Any) -> list[str]:
    # Unresolved locations are echoed by the handler; they are not indexed evidence.
    return [str(row.get("path") or "") for row in out.results if row.get("start_line")]


def _enclosing_query(out: Any) -> str:
    # Resolved names distinguish the code being read from other code in the same file.
    return " ".join(
        dict.fromkeys(
            str(row.get("symbol") or row.get("qualified_name") or "")
            for row in out.results
            if row.get("start_line")
        )
    )


def pack_paths(_input: Any, out: Any) -> list[str]:
    # a task briefing's files; the orientation map and a PR's stored pack name none
    return list(out._paths)
