"""Enclosing spans proved by the Python chunker's declaration ownership."""

from __future__ import annotations

import logging
from typing import Any

from server.code.app.spans import code_span, span_attrs
from server.code.app.symbol.resolve import _innermost
from server.code.store.index import CodeRepo
from server.embedding.embedder import served_model_label
from server.settings import settings

logger = logging.getLogger(__name__)


async def extend_decorated_spans(
    repo: CodeRepo,
    org: str,
    ns: str,
    locations: list[tuple[str, int]],
    by_path: dict[str, list[dict[str, Any]]],
    *,
    revisions: dict[str, str] | None = None,
) -> set[tuple[str, str]]:
    """A function chunk may start at its first Python decorator, before the symbol's def."""
    if not settings.embedding_model:
        return set()
    candidates: dict[tuple[str, str], tuple[dict[str, Any], set[int]]] = {}
    for path, line in locations:
        rows = by_path.get(path, [])
        if not path.lower().endswith((".py", ".pyi", ".pyw")):
            continue
        candidate = _innermost(rows, line)
        if candidate is not None:
            # An older declaration can be current, but the match revision
            # needs its owning chunk's start, including any decorators.
            if revisions is None or str(candidate.get("extent_commit") or "").rstrip("\0") == (
                revisions.get(path)
            ):
                continue
        else:
            candidate = min(
                (row for row in rows if row.get("symbol_id") and int(row["start_line"]) > line),
                key=lambda row: int(row["start_line"]),
                default=None,
            )
        if candidate is not None:
            key = (path, str(candidate["symbol_id"]))
            candidates.setdefault(key, (candidate, set()))[1].add(line)
    if not candidates:
        return set()
    keys = list(
        dict.fromkeys(
            (path, str(sid), int(row["start_line"]))
            for (path, _), (row, _) in candidates.items()
            for sid in (row.get("tier_ids") or [row["symbol_id"]])
        )
    )
    try:
        with code_span("enclosing.decorators", candidates=len(candidates)) as span:
            starts = await repo.function_chunk_starts(
                org, ns, served_model_label(settings.embedding_model), keys, revisions=revisions
            )
            span_attrs(span, rows=len(starts))
    except Exception:  # Optional ownership evidence can be absent while exact matches still work.
        logger.warning("code.enclosing could not read declaration extents", exc_info=True)
        return set()
    widened: set[tuple[str, str]] = set()
    for (path, _), (row, lines) in candidates.items():
        definition = int(row["start_line"])
        start = min(
            starts.get((path, str(sid), definition), definition)
            for sid in (row.get("tier_ids") or [row["symbol_id"]])
        )
        if 0 < start < definition and any(start <= line for line in lines):
            row["start_line"] = start
            widened.add((path, str(row["symbol_id"])))
    return widened
