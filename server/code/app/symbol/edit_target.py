"""Resolve an edit target from exact text at one index revision."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.code.api.models_grep import CodeGrepInput
from server.code.api.models_symbol import CodeEditContextInput
from server.code.app.common import repo_relative, resolve_code_identity
from server.code.app.grep import handle_code_grep, qualified_call_argument
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
    recovered_argument: bool = False


async def locate_edit_target(input: CodeEditContextInput) -> EditTarget:
    """Resolve a declaration or return verified source pointers."""
    path = repo_relative(input.name)
    if input.name and not path:
        return EditTarget()
    pattern: str | list[str] = input.clues
    if not path and len(input.clues) == 1 and qualified_call_argument(input.clues[0]):
        pattern = input.clues[0]
    org, ns = resolve_code_identity(input.org, input.namespace)
    found = await handle_code_grep(
        CodeGrepInput(
            org=org,
            namespace=ns,
            agent=input.agent,
            user_id=input.user_id,
            consumer=input.consumer,
            pattern=pattern,
            path_prefix=path,
            include_body=False,
            context_lines=0,
            related_on_miss=isinstance(pattern, str),
            include_lessons=False,
        ),
        exact_path=path,
    )
    related = found.related
    matches = related.matches if related is not None else found.matches
    candidates = _scope_candidates(matches)
    if not path:
        paths = {match["path"] for match in matches}
        if len(paths) != 1:
            return EditTarget(candidates=candidates)
        (path,) = paths
    revision = match_revisions(matches, found._match_commits).get(path)
    truncated = related.truncated if related is not None else found.truncated
    if not revision or found.has_more or truncated or found.partial:
        return EditTarget(candidates=candidates)
    from server.code.app.symbol.enclosing import ENCLOSING_KINDS

    repo = CodeRepo()
    rows = await repo.functions_for_paths(
        org, ns, [path], limit=2000, kinds=ENCLOSING_KINDS, with_revision=True
    )
    targets = _matched_rows(rows, matches)
    if len(targets) == 1:
        return EditTarget(
            f"{path}:{matches[0]['line']}",
            revision,
            candidates,
            targets,
            matches,
            recovered_argument=related is not None,
        )
    lines = {m["line"] for m in matches}
    types = await repo.types_for_paths(org, ns, [path], with_revision=True)
    covering = sorted(
        (row for row in types if 2 * sum(_contains(row, line) for line in lines) > len(lines)),
        key=_span,
    )
    if not covering or (len(covering) > 1 and _span(covering[0]) == _span(covering[1])):
        return EditTarget(candidates=candidates)
    owner = covering[0]
    if any(
        (row["end_line"] < owner["start_line"] or owner["end_line"] < row["start_line"])
        and any(_contains(row, line) for line in lines)
        for row in types
    ):
        return EditTarget(candidates=candidates)
    name = f"{path}:{owner['qualified_name']}"
    if not _is_large_container(owner) and _indexed_commit(owner.get("extent_commit")) == revision:
        return EditTarget(
            name, revision, candidates, [owner], matches, recovered_argument=related is not None
        )
    scoped = [m for m in matches if _contains(owner, m["line"])]
    members = _matched_rows(rows, scoped)
    if not members or any(
        not row["qualified_name"].startswith(owner["qualified_name"] + "::") for row in members
    ):
        return EditTarget(candidates=candidates)
    return EditTarget(
        name,
        revision,
        candidates,
        members,
        scoped,
        grouped=True,
        recovered_argument=related is not None,
    )


def _matched_rows(
    rows: list[dict[str, Any]], matches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Bind every hit by its own line. Overloads can share a grep scope name."""
    targets: dict[str, dict[str, Any]] = {}
    for match in matches:
        choices = [
            row
            for row in rows
            if row["qualified_name"] == match["scope"] and _contains(row, match["line"])
        ]
        if len(choices) != 1:
            return []
        row = choices[0]
        targets[row["symbol_id"]] = row
    return list(targets.values())


def _contains(row: dict[str, Any], line: int) -> bool:
    start_line: int = row["start_line"]
    end_line: int = row["end_line"]
    return start_line <= line <= end_line


def _span(row: dict[str, Any]) -> int:
    start_line: int = row["start_line"]
    end_line: int = row["end_line"]
    return end_line - start_line


def _scope_candidates(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: dict[tuple[str, str, int], dict[str, Any]] = {}
    for match in matches:
        start = match["line"]
        candidates.setdefault(
            (match["path"], match["scope"], start),
            {"path": match["path"], "qualified_name": match["scope"], "start_line": start},
        )
    return list(candidates.values())
