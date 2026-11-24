"""Resolve a file-scoped edit target from exact text at one index revision."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.code.api.models_grep import CodeGrepInput
from server.code.api.models_symbol import CodeEditContextInput
from server.code.app.common import repo_relative, resolve_code_identity
from server.code.app.grep import handle_code_grep
from server.code.app.grep_source import match_revisions
from server.code.app.symbol.outline import _is_large_container
from server.code.app.symbol.resolve import _indexed_commit
from server.code.store.index import CodeRepo


@dataclass
class EditTarget:
    name: str = ""
    revision: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)
    grouped: bool = False


async def locate_edit_target(input: CodeEditContextInput) -> EditTarget:
    """Resolve a declaration or return verified source pointers."""
    path = repo_relative(input.name)
    if not path:
        return EditTarget()
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
        ),
        exact_path=path,
    )
    matches = found.matches
    candidates = _scope_candidates(matches)
    revision = match_revisions(matches, found._match_commits).get(path)
    if not revision or found.has_more or found.truncated or found.partial:
        return EditTarget(candidates=candidates)
    from server.code.app.symbol.enclosing import ENCLOSING_KINDS

    repo = CodeRepo()
    rows = await repo.functions_for_paths(
        org, ns, [path], limit=2000, kinds=ENCLOSING_KINDS, with_revision=True
    )
    if len(candidates) == 1:
        targets = [row for row in rows if _row_key(row) == _match_key(matches[0])]
        if len(targets) == 1:
            return EditTarget(
                f"{path}:{matches[0]['line']}", revision, candidates, targets, matches
            )
    lines = {m["line"] for m in matches}
    types = await repo.types_for_paths(org, ns, [path], with_revision=True)
    covering = sorted(
        (
            row
            for row in types
            if 2 * sum(row["start_line"] <= line <= row["end_line"] for line in lines) > len(lines)
        ),
        key=_span,
    )
    if not covering or (len(covering) > 1 and _span(covering[0]) == _span(covering[1])):
        return EditTarget(candidates=candidates)
    owner = covering[0]
    name = f"{path}:{owner['qualified_name']}"
    if not _is_large_container(owner) and _indexed_commit(owner.get("extent_commit")) == revision:
        return EditTarget(name, revision, candidates, [owner], matches)
    scoped = [m for m in matches if owner["start_line"] <= m["line"] <= owner["end_line"]]
    wanted = {_match_key(m) for m in scoped}
    members = [
        row
        for row in rows
        if _row_key(row) in wanted
        and row["qualified_name"].startswith(owner["qualified_name"] + "::")
    ]
    if not members or {_row_key(row) for row in members} != wanted or len(members) != len(wanted):
        return EditTarget(candidates=candidates)
    return EditTarget(name, revision, candidates, members, scoped, grouped=True)


def _row_key(row: dict[str, Any]) -> tuple[str, int, int]:
    return row["qualified_name"], row["start_line"], row["end_line"]


def _match_key(match: dict[str, Any]) -> tuple[str, int, int]:
    return match["scope"], match["scope_start_line"], match["scope_end_line"]


def _span(row: dict[str, Any]) -> int:
    return row["end_line"] - row["start_line"]


def _scope_candidates(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: dict[tuple[str, int], dict[str, Any]] = {}
    for match in matches:
        start = match["scope_start_line"] or match["line"]
        candidates.setdefault(
            (match["scope"], start),
            {"path": match["path"], "qualified_name": match["scope"], "start_line": start},
        )
    return list(candidates.values())
