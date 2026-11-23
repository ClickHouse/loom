"""Enclosing bodies for hits in retained Python decorators."""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from server.code.app.spans import code_span, span_attrs
from server.code.app.symbol.resolve import _innermost
from server.code.domain.declaration import Declarations
from server.code.domain.symbol_body import complete_source_span
from server.code.store.index import CodeRepo
from server.embedding.embedder import served_model_label
from server.settings import settings

logger = logging.getLogger(__name__)


def _candidates(
    locations: list[tuple[str, int]],
    by_path: dict[str, list[dict[str, Any]]],
) -> list[tuple[dict[str, Any], set[int]]]:
    candidates: dict[str, tuple[dict[str, Any], set[int]]] = {}
    for path, line in locations:
        rows = by_path.get(path, [])
        if line < 1 or not path.lower().endswith((".py", ".pyi")) or _innermost(rows, line):
            continue
        row = min(
            (r for r in rows if r.get("symbol_id") and int(r["start_line"]) > line),
            key=lambda r: int(r["start_line"]),
            default=None,
        )
        if row is not None:
            candidates.setdefault(str(row["symbol_id"]), (row, set()))[1].add(line)
    return list(candidates.values())


def _retained_declaration(
    path: str, row: dict[str, Any], lines: set[int], chunks: list[dict[str, Any]]
) -> tuple[int, str] | None:
    if not chunks or len(chunks) > 400:
        return None
    first = min(int(chunk["start_line"]) for chunk in chunks)
    end = int(row["end_line"])
    text = complete_source_span(chunks, first, end, max_chars=0)
    if not text or (declarations := Declarations.parse(path, text.encode())) is None:
        return None
    start = declarations.decorator_start(
        int(row["start_line"]) - first + 1,
        end - first + 1,
        str(row["name"]),
        {line - first + 1 for line in lines},
    )
    if start is None:
        return None
    physical_lines = io.StringIO(text, newline="\n").readlines()
    return first + start - 1, "".join(physical_lines[start - 1 :])


async def recover_decorated_bodies(
    repo: CodeRepo,
    org: str,
    ns: str,
    locations: list[tuple[str, int]],
    by_path: dict[str, list[dict[str, Any]]],
    *,
    limit: int,
    concurrency: int,
) -> dict[str, str]:
    """Extend a function's span only when retained source establishes its decorators."""
    if not settings.embedding_model:
        return {}
    candidates = _candidates(locations, by_path)[:limit]
    if not candidates:
        return {}
    model = served_model_label(settings.embedding_model)
    semaphore = asyncio.Semaphore(concurrency)

    async def read(row: dict[str, Any], hits: set[int]) -> tuple[int, str] | None:
        path = str(row["path"])
        async with semaphore:
            try:
                chunks = await repo.source_chunks_for_span(
                    org, ns, path, min(hits), int(row["end_line"]), model
                )
                return await asyncio.to_thread(_retained_declaration, path, row, hits, chunks)
            except Exception:  # Exact line matches survive optional source recovery.
                logger.warning(
                    "code.enclosing decorator recovery failed for %s", path, exc_info=True
                )
                return None

    with code_span("enclosing.decorators", candidates=len(candidates)) as span:
        found = await asyncio.gather(*(read(row, hits) for row, hits in candidates))
        span_attrs(span, resolved=sum(hit is not None for hit in found))
    bodies: dict[str, str] = {}
    for (row, _), result in zip(candidates, found, strict=True):
        if result is not None:
            row["start_line"], bodies[str(row["symbol_id"])] = result
    return bodies
