"""Resolve a file-scoped edit target from exact text at one index revision."""

from __future__ import annotations

from typing import Any

from server.code.api.models_grep import CodeGrepInput
from server.code.api.models_symbol import CodeEditContextInput
from server.code.app.common import repo_relative, resolve_code_identity
from server.code.app.grep import handle_code_grep
from server.code.app.grep_source import match_revisions
from server.code.app.symbol.resolve import _indexed_commit
from server.code.store.index import CodeRepo


async def locate_edit_target(
    input: CodeEditContextInput,
) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Return an edit target or source pointers when matches do not resolve one."""
    path = repo_relative(input.name)
    if not path:
        return "", None, []
    org, ns = resolve_code_identity(input.org, input.namespace)
    found = await handle_code_grep(
        CodeGrepInput(
            org=org,
            namespace=ns,
            agent=input.agent,
            user_id=input.user_id,
            consumer=input.consumer,
            pattern=input.clues,
            path_prefix=path,
            include_body=False,
            include_lessons=False,
        )
    )
    matches = [m for m in found.matches if m["path"] == path]
    candidates = _scope_candidates(matches)
    revision = match_revisions(matches, found._match_commits).get(path)
    if not revision or found.has_more or found.truncated or found.partial:
        return "", None, candidates
    if len(candidates) == 1 and all(m["scope_start_line"] for m in matches):
        return f"{path}:{matches[0]['line']}", revision, candidates

    lines = {m["line"] for m in matches}
    types = await CodeRepo().types_for_paths(org, ns, [path], with_revision=True)
    covering = [
        (row["end_line"] - row["start_line"], row["qualified_name"])
        for row in types
        if _indexed_commit(row.get("extent_commit")) == revision
        and 2 * sum(row["start_line"] <= line <= row["end_line"] for line in lines) > len(lines)
    ]
    covering.sort()
    if covering and (len(covering) == 1 or covering[0][0] < covering[1][0]):
        return f"{path}:{covering[0][1]}", revision, candidates
    return "", None, candidates


def _scope_candidates(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: dict[tuple[str, int], dict[str, Any]] = {}
    for match in matches:
        start = match["scope_start_line"] or match["line"]
        candidates.setdefault(
            (match["scope"], start),
            {"path": match["path"], "qualified_name": match["scope"], "start_line": start},
        )
    return list(candidates.values())
