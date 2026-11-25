"""Retained definitions behind a grep argument or forwarding declaration."""

from __future__ import annotations

import ast
import asyncio
import io
import logging
import re
import textwrap
import tokenize
from collections import Counter, defaultdict
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
_DECLARATION = re.compile(r"(?:async[ \t]+)?def[ \t]+([^\W\d]\w*)(?=[ \t]*(?:\(|$))")


_Forwarder = tuple[dict[str, Any], GrepDependency, str, tuple[int, str]]


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


async def forwarded_dependency(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    scope: SearchScope,
    definitions: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    commits: dict[tuple[str, int], str],
    budget: Budget | None,
) -> tuple[RelatedGrepEvidence | None, str]:
    if (
        input.regex
        or not definitions
        or ("context_lines" in input.model_fields_set and input.context_lines == 0)
    ):
        return None, ""
    patterns = input.pattern if isinstance(input.pattern, list) else [input.pattern]
    names = {m[1] for pattern in patterns if (m := _DECLARATION.match(pattern.strip()))}
    if not names:
        return None, ""
    try:
        with code_span("grep.forwarded_definition"):
            async with asyncio.timeout(None if budget is None else budget.left_ms() / 1000):
                found = await _forwarded_definition(
                    repo,
                    org,
                    ns,
                    input,
                    scope,
                    definitions,
                    names,
                    match_revisions(matches, commits),
                )
    except TimeoutError:
        if budget is not None:
            budget.drop("forwarded definition lookup timed out")
        return None, "forwarded definition lookup timed out"
    except Exception as error:  # noqa: BLE001 — a failed optional read leaves the exact result intact
        logger.debug("code.grep forwarded definition lookup ended: %s", type(error).__name__)
        return None, "forwarded definition lookup failed"
    return found, ""


async def _forwarded_definition(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    scope: SearchScope,
    definitions: list[dict[str, Any]],
    names: set[str],
    revisions: dict[str, str],
) -> RelatedGrepEvidence | None:
    wrappers = await asyncio.to_thread(_forwarders, definitions, names, revisions)
    if not wrappers:
        return None
    ids = [str(d["symbol_id"]) for d, _, _, _ in wrappers]
    edges = await _helper_edges(repo, org, ns, ids)
    delivered = {str(d.get("qualified_name", "")) for d in definitions if d.get("body")}
    candidates = await asyncio.to_thread(_forwarding_edges, wrappers, edges, delivered)
    rows = await repo.symbols_by_ids(org, ns, sorted({e["to_symbol_id"] for _, e in candidates}))
    counts = Counter(r["symbol_id"] for r in rows)
    by_id = {r["symbol_id"]: r for r in rows if counts[r["symbol_id"]] == 1}
    available: dict[tuple[str, str], tuple[GrepDependency, dict[str, Any]]] = {}
    for wrapper, edge in candidates:
        sid = edge["to_symbol_id"]
        if (row := by_id.get(sid)) is not None and _dependency_span(row, edge, input, scope):
            available.setdefault((sid, _indexed_commit(edge["indexed_commit"])), (wrapper, row))
    if len(available) != 1:
        return None
    (_, revision), (wrapper, row) = next(iter(available.items()))
    source = await _dependency_source(repo, org, ns, row, revision)
    if source is None or await asyncio.to_thread(_covers_dependency, definitions, source):
        return None
    return RelatedGrepEvidence(pattern=wrapper.qualified_name, matches=[], dependencies=[source])


def _covers_dependency(definitions: list[dict[str, Any]], source: GrepDependency) -> bool:
    for row in definitions:
        start, end = row["start_line"], row["end_line"]
        body = row.get("body")
        if (
            row["path"] == source.path
            and body
            and not row.get("body_elided")
            and 0 < start <= source.start_line <= source.end_line <= end
        ):
            lines = list(io.StringIO(body, newline="\n"))
            if (
                len(lines) == end - start + 1
                and "".join(lines[source.start_line - start : source.end_line - start + 1])
                == source.body
            ):
                return True
    return False


def _forwarding_edges(
    wrappers: list[_Forwarder],
    edges: list[dict[str, Any]],
    delivered: set[str],
) -> list[tuple[GrepDependency, dict[str, Any]]]:
    by_caller: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        by_caller[edge["from_symbol_id"]].append(edge)
    candidates = []
    for row, wrapper, revision, (line, member) in wrappers:
        exact = [
            e
            for e in by_caller[row["symbol_id"]]
            if e["line"] == line and e["to_qualified_name"].endswith(f"::{member}")
        ]
        if (bound := _match_helper(exact, wrapper, revision)) and bound[
            "to_qualified_name"
        ] not in delivered:
            candidates.append((wrapper, bound))
    return candidates


def _forwarders(
    definitions: list[dict[str, Any]], names: set[str], revisions: dict[str, str]
) -> list[_Forwarder]:
    out = []
    for row in definitions:
        path, start = row["path"], row["start_line"]
        qualified = str(row.get("qualified_name") or "")
        name = qualified.rsplit("::", 1)[-1]
        if (
            name not in names
            or not row.get("symbol_id")
            or not path.endswith(_PYTHON_PATHS)
            or start not in row["lines"]
            or not (revision := revisions.get(path))
            or not (body := row.get("body"))
            or (call := _forward_call(body, name)) is None
        ):
            continue
        out.append(
            (
                row,
                GrepDependency(
                    qualified_name=qualified,
                    path=path,
                    start_line=start,
                    end_line=row["end_line"],
                    body=body,
                ),
                revision,
                (start + call[0] - 1, call[1]),
            )
        )
    return out


def _forward_call(body: str, name: str) -> tuple[int, str] | None:
    try:
        [function] = ast.parse(textwrap.dedent(body)).body
    except (SyntaxError, ValueError):
        return None
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name != name:
        return None
    statements = (
        function.body[1:] if ast.get_docstring(function, clean=False) is not None else function.body
    )
    if len(statements) != 1 or not isinstance(statements[0], (ast.Expr, ast.Return)):
        return None
    value = statements[0].value
    if isinstance(value, ast.Await):
        value = value.value
    if (
        not isinstance(value, ast.Call)
        or sum(isinstance(node, ast.Call) for node in ast.walk(value)) != 1
    ):
        return None
    if isinstance(value.func, ast.Name):
        return value.lineno, value.func.id
    if isinstance(value.func, ast.Attribute):
        return value.lineno, value.func.attr
    return None


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
    if not _dependency_span(row, edge, input, scope):
        return None
    source = await _dependency_source(repo, org, ns, row, revision)
    return (row, source) if source is not None else None


def _dependency_span(
    row: dict[str, Any], edge: dict[str, Any], input: CodeGrepInput, scope: SearchScope
) -> bool:
    return (
        row["qualified_name"] == edge["to_qualified_name"]
        and row["kind"] in {"function", "method", "property"}
        and 0 < row["start_line"] <= row["end_line"]
        and _allowed(row["path"], input, scope)
    )


async def _dependency_source(
    repo: CodeRepo, org: str, ns: str, row: dict[str, Any], revision: str
) -> GrepDependency | None:
    extent = {**row, "_extent_commit": row.get("indexed_commit")}
    body = await symbol_body(repo, org, ns, extent, expected_commit=revision)
    if not body:
        return None
    source = GrepDependency(
        qualified_name=row["qualified_name"],
        path=row["path"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        body=body,
    )
    source._indexed_commit = revision
    return source


async def _one_helper(
    repo: CodeRepo,
    org: str,
    ns: str,
    root: dict[str, Any],
    source: GrepDependency,
    revision: str,
) -> dict[str, Any] | None:
    ids = [root["symbol_id"], *(root.get("_tier_ids") or [])]
    edges = await _helper_edges(repo, org, ns, ids)
    return await asyncio.to_thread(_match_helper, edges, source, revision)


async def _helper_edges(repo: CodeRepo, org: str, ns: str, ids: list[str]) -> list[dict[str, Any]]:
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
    return edges if len(edges) <= 200 else []


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
