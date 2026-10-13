"""Memory reads the code plane serves from: lesson excerpts on a symbol card and
the directories a user's memories name. The statements are
`server.memory.store.code_plane_reads`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.identity.domain.read_scope import ReadFilters
from server.memory.store import code_plane_reads as reads


async def lesson_corpus(org: str, namespaces: Sequence[str], *, cap: int) -> list[dict[str, Any]]:
    """Up to `cap` live lesson excerpts of `namespaces`, shortest first, each with
    `memory_key`, `namespace`, `content_excerpt`, `content_excerpt_lc` and `files`."""
    return await reads.lesson_corpus(org, namespaces, cap=cap)


async def lessons_matching(
    org: str, namespaces: Sequence[str], *, token: str, paths: Sequence[str], limit: int
) -> list[dict[str, Any]]:
    """Live lesson excerpts of `namespaces` that hold `token` or name one of
    `paths` in their files facet, shortest first."""
    return await reads.lessons_matching(org, namespaces, token=token, paths=paths, limit=limit)


async def user_file_dirs(
    org: str, user_id: str, namespaces: Sequence[str] | None, *, scope: ReadFilters
) -> list[str]:
    """The parent directories of the files the user's live memories in
    `namespaces` (None: every namespace) and inside `scope` name, in path order,
    each once."""
    dirs: dict[str, None] = {}
    for f in await reads.user_file_paths(org, user_id, namespaces, scope=scope):
        dirs.setdefault(f.rsplit("/", 1)[0] if "/" in f else f, None)
    return list(dirs)
