"""Resolve an edit target from exact text at one index revision."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from server.code.api.models_grep import CodeGrepInput
from server.code.api.models_symbol import EDIT_CONTEXT_MAX_NAMES, CodeEditContextInput
from server.code.app.common import repo_relative, resolve_code_identity
from server.code.app.grep import handle_code_grep, qualified_call_argument
from server.code.app.grep_source import GREP_MATCH_TEXT_CHARS, match_revisions
from server.code.app.spans import code_span
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
    declarations: list[EditTarget] = field(default_factory=list)


async def locate_edit_target(input: CodeEditContextInput) -> EditTarget:
    """Resolve a declaration or return verified source pointers."""
    path = repo_relative(input.name)
    if input.name and not path:
        return EditTarget()
    pattern: str | list[str] = input.clues
    if len(input.clues) == 1 and qualified_call_argument(input.clues[0]):
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
    revisions = match_revisions(matches, found._match_commits)
    partial = (
        found.has_more
        or (related.truncated if related is not None else found.truncated)
        or found.partial
    )
    if not path:
        paths = {match["path"] for match in matches}
        if len(paths) != 1:
            declarations = []
            if len(paths) > 1 and related is None and not partial:
                declarations = await _declaration_targets(org, ns, matches, revisions, input.clues)
            return EditTarget(candidates=candidates, declarations=declarations)
        (path,) = paths
    revision = revisions.get(path)
    if not revision or partial:
        return EditTarget(candidates=candidates)
    from server.code.app.symbol.enclosing import ENCLOSING_KINDS

    repo = CodeRepo()
    rows = await repo.functions_for_paths(
        org, ns, [path], limit=2000, kinds=ENCLOSING_KINDS, with_revision=True
    )
    targets = _matched_rows(rows, matches)
    if len(targets) != 1 and related is None:
        supported = _definition_clue_matches(rows, matches, input.clues)
        bound = _matched_rows(rows, supported)
        if len(bound) == 1:
            targets, matches = bound, supported
            candidates = _scope_candidates(matches)
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


async def _declaration_targets(
    org: str,
    ns: str,
    matches: list[dict[str, Any]],
    revisions: dict[str, str],
    clues: list[str],
) -> list[EditTarget]:
    """Find each exact declaration only when the whole clue match set is verifiable."""
    required = set(clues)
    if not 1 < len(matches) <= EDIT_CONTEXT_MAX_NAMES or len(required) < 2:
        return []
    if any(
        match["line"] != match["scope_start_line"] or len(match["text"]) >= GREP_MATCH_TEXT_CHARS
        for match in matches
    ) or not all(any(clue in match["text"] for match in matches) for clue in required):
        return []
    paths = sorted({match["path"] for match in matches})
    if any(not revisions.get(path) for path in paths):
        return []
    from server.code.app.symbol.enclosing import ENCLOSING_KINDS

    with code_span("edit_context.declarations", seeds=len(matches)):
        rows = await CodeRepo().functions_for_paths(
            org, ns, paths, limit=2000, kinds=ENCLOSING_KINDS, with_revision=True
        )
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_path[row["path"]].append(row)
    targets = []
    for match in matches:
        path, line = match["path"], match["line"]
        definition = _row_at_match(by_path[path], match)
        revision = revisions[path]
        if (
            definition is None
            or definition["start_line"] != line
            or _indexed_commit(definition.get("extent_commit")) != revision
        ):
            return []
        targets.append(EditTarget(f"{path}:{line}", revision, rows=[definition], matches=[match]))
    return targets


def _definition_clue_matches(
    rows: list[dict[str, Any]], matches: list[dict[str, Any]], clues: list[str]
) -> list[dict[str, Any]]:
    """A complete set of literal witnesses in one indexed source span."""
    required = set(clues)
    # Grep strips and clips line previews. An incomplete preview cannot rule out a tie.
    if len(required) < 2 or any(clue != clue.strip() for clue in required):
        return []
    if any(len(match["text"]) >= GREP_MATCH_TEXT_CHARS for match in matches):
        return []
    spans: dict[tuple[str] | tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        row = _row_at_match(rows, match)
        # Overloads share a grep scope. Keep their witnesses separate by declaration.
        key: tuple[str] | tuple[str, int, int] = (
            (str(row["symbol_id"]),)
            if row is not None
            else (match["scope"], match["scope_start_line"], match["scope_end_line"])
        )
        spans[key].append(match)
    covers = [
        hits
        for hits in spans.values()
        if all(any(clue in hit["text"] for hit in hits) for clue in required)
    ]
    return covers[0] if len(covers) == 1 else []


def _matched_rows(
    rows: list[dict[str, Any]], matches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Bind every hit by its own line. Overloads can share a grep scope name."""
    targets: dict[str, dict[str, Any]] = {}
    for match in matches:
        row = _row_at_match(rows, match)
        if row is None:
            return []
        targets[row["symbol_id"]] = row
    return list(targets.values())


def _row_at_match(rows: list[dict[str, Any]], match: dict[str, Any]) -> dict[str, Any] | None:
    choices = [
        row
        for row in rows
        if row["qualified_name"] == match["scope"] and _contains(row, match["line"])
    ]
    return choices[0] if len(choices) == 1 else None


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
