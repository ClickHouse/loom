"""`lessons` on a code answer: the memories that name or are linked to the files it
returns, read after the op's own answer (outside code.search's shared memo, so a stored
or replayed answer never carries another caller's lessons), for this caller only.

An agent that looks at code through these tools gets the team's notes on that code with
no memory call of its own. The read is bounded (`code_enrich_timeout_s`, as code.symbol's
lessons are) and degrades to no `lessons`; a token narrowed below its namespaces reads
none (the memory rows are not filtered by user), as code.symbol's lessons do.
`include_lessons: false` or `code_result_lessons` off skips the read. These are
file-level; code.symbol's own `lessons` are the memories linked to the one symbol."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from server.code.api.models_search import CodeSearchInput
from server.code.app import common, enrich, memory_links
from server.code.app.search_legs import resolve_deadline_ms
from server.code.app.spans import code_span
from server.code.domain import deadline as code_deadline
from server.identity.app import request_scope
from server.memory.app import code_plane_reads, path_lessons
from server.settings import settings

Handler = Callable[[Any], Awaitable[Any]]
PathsOf = Callable[[Any, Any], Iterable[str]]


def _top_paths(paths: Iterable[str]) -> list[str]:
    """The answer's first distinct files, in its order, as many as the setting reads,
    spelled as the index stores them (code.impact passes the caller's `files` as given)."""
    distinct = dict.fromkeys(filter(None, map(common.repo_relative, paths)))
    return list(distinct)[: settings.code_result_lessons_paths]


def _searched_namespace(input: Any) -> str:
    """The code namespace the answer was read from: a one-entry `namespaces` list is
    searched in place of `namespace` (code.search's fan-out normalisation)."""
    names = list(getattr(input, "namespaces", None) or [])
    return str(names[0] if len(names) == 1 else input.namespace)


def _bound_s(input: Any, started: float) -> float:
    """Seconds the read may take: `code_enrich_timeout_s`, and no more than what is left
    of code.search's own `deadline_ms`, read as the search reads it."""
    bound = settings.code_enrich_timeout_s
    if isinstance(input, CodeSearchInput):  # the one wrapped op with a deadline
        budget = code_deadline.start(resolve_deadline_ms(input.deadline_ms), started=started)
        if budget is not None:
            bound = min(bound, budget.left_ms() / 1000)
    return bound


async def lessons_for(input: Any, paths: Iterable[str], started: float) -> path_lessons.Lessons:
    """The lessons for the answer's top `paths`; none when off, narrowed, past the call's
    deadline (from `started`, the call's monotonic start), or on any failure."""
    top = _top_paths(paths)
    if not (input.include_lessons and settings.code_result_lessons and top):
        return path_lessons.Lessons([])
    if request_scope.request_narrowed():
        return path_lessons.Lessons([])  # a token that may not read every user's memories

    async def read() -> path_lessons.Lessons:
        org, ns = common.resolve_code_identity(input.org, _searched_namespace(input))
        trunk = memory_links.trunk_namespace(org, ns) or ns
        nss = await code_plane_reads.lesson_namespaces(org, ns, input.lesson_namespaces)
        # another repository's code namespace would bring its links to a same-named file
        nss = [n for n in nss if memory_links.trunk_namespace(org, n) in ("", trunk)]
        return await path_lessons.lessons_for_paths(
            org,
            nss,
            trunk if trunk in nss else "",
            top,
            token_budget=settings.code_result_lessons_tokens,
        )

    bound = _bound_s(input, started)
    if bound <= 0:
        return path_lessons.Lessons([])
    with code_span("enrich.lessons"):
        return await enrich.bounded(read(), path_lessons.Lessons([]), seconds=bound)


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
        found = await lessons_for(input, self.paths_of(input, out), started)
        out.lessons, out.lessons_left_out = found.rows, found.left_out
        return out


# The files each code answer returns, best first.


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


def pack_paths(_input: Any, out: Any) -> list[str]:
    # a task briefing's files; the orientation map and a PR's stored pack name none
    return list(out._paths)
