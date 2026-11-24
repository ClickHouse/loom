"""Indexed test examples for references that do not create call edges."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.code.app.symbol.resolve import _indexed_commit
from server.code.domain.symbol_body import retained_source_lines, retained_source_window
from server.code.store.index import CodeRepo
from server.embedding.embedder import served_model_label
from server.settings import settings


async def named_test_examples(
    repo: CodeRepo, org: str, ns: str, definition: dict[str, Any], radius: int
) -> list[dict[str, Any]]:
    """Read named tests that mention the identifier in verified indexed source."""
    name = str(definition.get("name") or "")
    if len(name) < 3 or not name.isidentifier() or not settings.embedding_model:
        return []
    reference = re.compile(rf"\b{re.escape(name)}\b")
    model = served_model_label(settings.embedding_model)
    candidates = await repo.symbols_fuzzy(org, ns, name, tests_only=True, limit=6)

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
            start, end, text = window
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
