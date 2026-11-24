"""Retained definitions behind a related grep argument."""

from __future__ import annotations

import asyncio
import io
import logging
import tokenize
from collections import Counter
from typing import Any

from server.code.api.models_grep import CodeGrepInput, GrepDependency, RelatedGrepEvidence
from server.code.app.grep_source import GREP_MATCH_TEXT_CHARS, match_revisions
from server.code.app.spans import code_span
from server.code.app.symbol.resolve import _indexed_commit, symbol_body
from server.code.domain.deadline import Budget
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo
from server.core.caller_regex import compile_caller_regex

logger = logging.getLogger(__name__)

_PYTHON_PATHS = (".py", ".pyi", ".pyw")


async def add_dependencies(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    related: RelatedGrepEvidence | None,
    scope: SearchScope,
    commits: dict[tuple[str, int], str],
    budget: Budget | None,
) -> str:
    if (
        related is None
        or related.truncated
        or not input.include_dependencies
        or ("context_lines" in input.model_fields_set and input.context_lines == 0)
        or not any(r["path"].endswith(_PYTHON_PATHS) for r in related.matches)
    ):
        return ""
    if budget is not None and budget.left_ms() <= 0:
        budget.drop("related definitions skipped")
        return budget.reason()
    try:
        with code_span("grep.dependencies"):
            async with asyncio.timeout(None if budget is None else budget.left_ms() / 1000):
                related.dependencies = await _find_dependencies(
                    repo, org, ns, input, related, scope, commits
                )
    except TimeoutError:
        if budget is not None:
            budget.drop("related definition lookup timed out")
            return budget.reason()
        return "related definition lookup timed out"
    except Exception as error:  # noqa: BLE001 — exact and related source survive optional failures
        logger.debug("code.grep dependency lookup ended: %s", type(error).__name__)
        return "related definition lookup failed"
    return ""


def _code_tokens(text: str) -> list[str]:
    names = []
    try:
        for item in tokenize.generate_tokens(io.StringIO(text).readline):
            if item.type == tokenize.COMMENT:
                break
            if item.type in {tokenize.NAME, tokenize.OP}:
                names.append(item.string)
            elif item.type in {tokenize.STRING, tokenize.NUMBER}:
                names.append("")
    except (tokenize.TokenError, IndentationError):
        # A grep line can start or end within a multiline function call.
        pass
    return names


def _member_witness(text: str, receiver: str, member: str) -> bool:
    if not text or len(text) >= GREP_MATCH_TEXT_CHARS:
        return False
    tokens = _code_tokens(text)
    if tokens.count(member) != 1:
        return False
    at = tokens.index(member)
    return at >= 2 and tokens[at - 2 : at] == [receiver, "."] and (at < 3 or tokens[at - 3] != ".")


def _allowed(path: str, input: CodeGrepInput, scope: SearchScope) -> bool:
    return (
        path.endswith(_PYTHON_PATHS)
        and path.startswith(input.path_prefix)
        and path not in input.exclude_unit_ids
        and (not scope.exact_path or path == scope.exact_path)
        and (not scope.extensions or path.endswith(scope.extensions))
        and (
            not scope.path_globs_re
            or any(
                compile_caller_regex(regex, dot_nl=True).search(path)
                for regex in scope.path_globs_re
            )
        )
        and not any(
            compile_caller_regex(regex, dot_nl=True).search(path)
            for regex in scope.exclude_globs_re
        )
    )


async def _find_dependencies(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    related: RelatedGrepEvidence,
    scope: SearchScope,
    commits: dict[tuple[str, int], str],
) -> list[GrepDependency]:
    receiver, _, member = related.pattern.partition(".")
    if not receiver.isidentifier() or not member.isidentifier():
        return []
    revisions = match_revisions(related.matches, commits)
    sites = {
        (r["path"], r["line"]): r["scope"]
        for r in related.matches
        if r["path"] in revisions
        and r["path"].endswith(_PYTHON_PATHS)
        and r["scope"]
        and _member_witness(r["text"], receiver, member)
    }
    if not sites:
        return []
    edges = await repo.member_edges_at_locations(org, ns, sorted(sites), member)
    root = _one_target(
        [
            e
            for e in edges
            if e["from_qualified_name"] == sites.get((e["path"], e["line"]))
            and _indexed_commit(e["indexed_commit"]) == revisions.get(e["path"])
        ]
    )
    if root is None:
        return []
    revision = _indexed_commit(root["indexed_commit"])
    definition = await _load(repo, org, ns, input, scope, root, revision)
    if definition is None:
        return []
    row, source = definition
    related.dependencies.append(source)
    helper = await _one_helper(repo, org, ns, row, source, revision)
    if helper is None:
        return [source]
    extra = await _load(repo, org, ns, input, scope, helper, revision)
    return [source, extra[1]] if extra is not None else [source]


def _one_target(edges: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len({(e["to_qualified_name"], _indexed_commit(e["indexed_commit"])) for e in edges}) != 1:
        return None
    strongest = [e for e in edges if e["tier"] == "scip"] or edges
    ids = {e["to_symbol_id"] for e in strongest}
    return strongest[0] if len(ids) == 1 else None


async def _load(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    scope: SearchScope,
    edge: dict[str, Any],
    revision: str,
) -> tuple[dict[str, Any], GrepDependency] | None:
    rows = await repo.symbols_by_ids(org, ns, [edge["to_symbol_id"]])
    if len(rows) != 1:
        return None
    row = rows[0]
    path, name = row["path"], row["qualified_name"]
    start, end = row["start_line"], row["end_line"]
    if (
        name != edge["to_qualified_name"]
        or row["kind"] not in {"function", "method", "property"}
        or not 0 < start <= end
        or not _allowed(path, input, scope)
    ):
        return None
    extent = {**row, "_extent_commit": row.get("indexed_commit")}
    body = await symbol_body(repo, org, ns, extent, expected_commit=revision)
    if not body:
        return None
    return row, GrepDependency(
        qualified_name=name, path=path, start_line=start, end_line=end, body=body
    )


async def _one_helper(
    repo: CodeRepo, org: str, ns: str, root: dict[str, Any], source: GrepDependency, revision: str
) -> dict[str, Any] | None:
    ids = [root["symbol_id"], *(root.get("_tier_ids") or [])]
    edges = await repo.edges_for_symbols(
        org,
        ns,
        ids,
        direction="callees",
        edge_kinds=["calls"],
        limit=201,
        min_confidence=0.8,
        with_revision=True,
    )
    if len(edges) > 200:
        return None
    return await asyncio.to_thread(_match_helper, edges, source, revision)


def _match_helper(
    edges: list[dict[str, Any]], source: GrepDependency, revision: str
) -> dict[str, Any] | None:
    lines = source.body.split("\n")
    invocations: dict[int, set[str]] = {}
    called = []
    for edge in edges:
        offset = edge["line"] - source.start_line
        if (
            edge["path"] != source.path
            or edge["from_qualified_name"] != source.qualified_name
            or edge["to_qualified_name"] == source.qualified_name
            or _indexed_commit(edge["indexed_commit"]) != revision
            or not 0 <= offset < len(lines)
        ):
            continue
        if offset not in invocations:
            tokens = _code_tokens(lines[offset])
            counts = Counter(tokens)
            invocations[offset] = {
                token
                for token, following in zip(tokens, tokens[1:], strict=False)
                if following == "(" and counts[token] == 1
            }
        name = edge["to_qualified_name"].rsplit("::", 1)[-1]
        if name in invocations[offset]:
            called.append(edge)
    return _one_target(called)
