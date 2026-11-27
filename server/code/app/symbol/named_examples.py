"""Indexed test examples for references that do not create call edges."""

from __future__ import annotations

import ast
import asyncio
import re
import textwrap
from typing import Any

from server.code.app.symbol.resolve import _indexed_commit
from server.code.app.symbol_uses import TYPE_KINDS, single_declaration
from server.code.domain.anchor import local_name
from server.code.domain.extract.treesitter import lang_for_path
from server.code.domain.symbol_body import retained_source_lines, retained_source_window
from server.code.store.index import CodeRepo
from server.embedding.embedder import served_model_label
from server.settings import settings


def _with_decorators(
    chunks: list[dict[str, Any]],
    window: tuple[int, int, str],
    row: dict[str, Any],
    revision: str,
    minimum: int,
) -> tuple[int, int, str]:
    first, end, text = window
    if first != row["start_line"] or minimum >= first or lang_for_path(row["path"]) != "python":
        return window
    last = row["end_line"]
    # Python function chunks start at their first decorator. Symbol extents start at def.
    starts = {
        chunk["start_line"]
        for chunk in chunks
        if chunk["kind"] == "function"
        and chunk.get("scope") == row["qualified_name"]
        and _indexed_commit(chunk.get("indexed_commit")) == revision
        and minimum <= chunk["start_line"] < first
        and chunk["end_line"] == last
    }
    if len(starts) != 1:
        return window
    (start,) = starts
    complete = retained_source_window(chunks, start, last, anchor=first, revision=revision)
    if complete is None or complete[:2] != (start, last):
        return window
    source = complete[2]
    if len(source) > settings.code_chunk_max_chars or not source.lstrip().startswith("@"):
        return window
    try:
        nodes = ast.parse(textwrap.dedent(source)).body
    except (RecursionError, SyntaxError, ValueError):
        return window
    if len(nodes) != 1 or not isinstance(node := nodes[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        return window
    if (
        not node.decorator_list
        or node.name != row["name"]
        or node.lineno != first - start + 1
        or node.end_lineno != last - start + 1
    ):
        return window
    prefix = "\n".join(source.split("\n", first - start)[:-1])
    return start, end, f"{prefix}\n{text}"


async def owning_type(
    repo: CodeRepo, org: str, ns: str, method: dict[str, Any]
) -> dict[str, Any] | None:
    """The method's named container with a body span at the same indexed revision."""
    qualified = str(method.get("qualified_name") or "")
    owner, separator, _ = qualified.rpartition("::")
    path = str(method.get("path") or "")
    revision = _indexed_commit(method.get("indexed_commit") or method.get("extent_commit"))
    line = int(method.get("start_line") or 0)
    if (
        method.get("kind") not in {"method", "function", "symbol"}
        or not (separator and owner and path and revision and line > 0)
        or "." not in local_name(qualified, path)
    ):
        return None
    types = await repo.types_for_paths(org, ns, [path], with_revision=True)
    matches = [
        row
        for row in types
        if row["qualified_name"] == owner
        and row["kind"] in TYPE_KINDS
        and _indexed_commit(row.get("extent_commit")) == revision
        and row["start_line"] <= line <= row["end_line"]
    ]
    return matches[0] if len(matches) == 1 else None


async def named_test_examples(
    repo: CodeRepo, org: str, ns: str, definition: dict[str, Any], radius: int
) -> list[dict[str, Any]]:
    """Read tests that mention a type in verified source, such as decorator arguments."""
    if definition.get("kind") not in TYPE_KINDS:
        return []  # routine names alone do not identify which declaration the test called
    name = str(definition.get("name") or "")
    if len(name) < 3 or not name.isidentifier() or not settings.embedding_model:
        return []
    reference = re.compile(rf"\b{re.escape(name)}\b")
    model = served_model_label(settings.embedding_model)
    declarations, candidates = await asyncio.gather(
        repo.symbols_by_name(org, ns, name),
        repo.symbols_fuzzy(org, ns, name, tests_only=True, limit=6),
    )
    target = (definition.get("qualified_name"), definition.get("path"))
    if not single_declaration(declarations) or {
        (row.get("qualified_name"), row.get("path")) for row in declarations
    } != {target}:
        return []

    async def example(row: dict[str, Any]) -> dict[str, Any] | None:
        revision = _indexed_commit(row.get("indexed_commit"))
        if not revision or row.get("kind") not in {"function", "method"}:
            return None
        if row["qualified_name"] == definition.get("qualified_name"):
            return None
        path = str(row["path"])
        first, last = int(row["start_line"]), int(row["end_line"])
        if first <= 0 or last < first:
            return None
        chunks = await repo.source_chunks_for_span(
            org, ns, path, first, last, model, with_revision=True
        )
        if len(chunks) > 400:
            return None
        hits = {
            line
            for chunk in chunks
            if _indexed_commit(chunk.get("indexed_commit")) == revision
            for line, text in enumerate(
                retained_source_lines(chunk) or [], int(chunk["start_line"])
            )
            if first <= line <= last and reference.search(text)
        }
        for hit in sorted(hits):
            window = retained_source_window(
                chunks,
                max(first, hit - radius),
                min(last, hit + radius),
                anchor=hit,
                revision=revision,
            )
            if window is None:
                continue
            start, end, text = _with_decorators(chunks, window, row, revision, max(1, hit - radius))
            result = {
                "caller": str(row["qualified_name"]),
                "path": path,
                "line": hit,
                "tier": str(row["tier"]),
                "evidence": "name_reference",
            }
            if radius:
                result["context"] = {
                    "commit": revision,
                    "start_line": start,
                    "end_line": end,
                    "text": text,
                }
            return result
        return None

    rows = await asyncio.gather(*(example(row) for row in candidates))
    return [row for row in rows if row is not None][:3]
