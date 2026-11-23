"""Replace unmappable summaries with verified retained source for exact grep."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.code.app.spans import code_span, span_attrs
from server.code.domain.deadline import Budget
from server.code.domain.symbol_body import complete_source_span, retained_source_lines
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)


ContextSource = dict[str, dict[str, str | None]]


def _wanted_lines(matches: list[dict[str, Any]], radius: int) -> dict[str, set[int]]:
    wanted: dict[str, set[int]] = {}
    for hit in matches:
        line = int(hit["line"])
        wanted.setdefault(str(hit["path"]), set()).update(
            range(max(1, line - radius), line + radius + 1)
        )
    return wanted


def collect_contexts(
    chunks: list[dict[str, Any]], matches: list[dict[str, Any]], radius: int, max_chars: int
) -> ContextSource:
    """Keep each requested retained line once, including conflicts between chunk views."""
    wanted = _wanted_lines(matches, radius)
    by_path: ContextSource = {}
    scanned = 0
    for chunk in chunks:
        path = str(chunk["path"])
        scanned += len(str(chunk.get("content") or ""))
        if scanned > max_chars:
            break
        if path not in wanted or (lines := retained_source_lines(chunk)) is None:
            continue
        source = by_path.setdefault(path, {})
        for line, text in enumerate(lines, int(chunk["start_line"])):
            if line in wanted[path]:
                key = str(line)
                if source.setdefault(key, text) != text:
                    source[key] = None
    return by_path


def page_contexts(
    source: ContextSource, matches: list[dict[str, Any]], radius: int
) -> dict[str, Any]:
    """Render only this page's windows from retained lines that agree."""
    wanted = _wanted_lines(matches, radius)
    partial = False
    contexts = []
    for path, lines in wanted.items():
        retained = source.get(path, {})
        window: list[str] = []
        start = previous = 0
        for line in sorted(lines):
            key = str(line)
            text = retained.get(key)
            if text is None and key in retained:
                partial = True
            if window and (text is None or line != previous + 1 or not window[-1].endswith("\n")):
                contexts.append(
                    {
                        "path": path,
                        "start_line": start,
                        "end_line": previous,
                        "text": "".join(window),
                    }
                )
                window = []
            if text is not None:
                if not window:
                    start = line
                window.append(text)
                previous = line
        if window:
            contexts.append(
                {"path": path, "start_line": start, "end_line": previous, "text": "".join(window)}
            )
    return {"contexts": contexts, "contexts_partial": partial}


def _summary_spans(chunks: list[dict[str, Any]]) -> dict[tuple[str, str, str], tuple[int, int]]:
    spans: dict[tuple[str, str, str], tuple[int, int]] = {}
    for chunk in chunks:
        if chunk.get("kind") != "class_skeleton" or not chunk.get("embedding_model"):
            continue
        key = (str(chunk["path"]), str(chunk["embedding_model"]), str(chunk.get("scope") or ""))
        start, end = int(chunk["start_line"]), int(chunk["end_line"])
        if key in spans:
            start, end = min(start, spans[key][0]), max(end, spans[key][1])
        spans[key] = start, end
    return spans


async def recover_source_chunks(
    repo: CodeRepo,
    org: str,
    ns: str,
    chunks: list[dict[str, Any]],
    *,
    max_chars: int,
    budget: Budget | None = None,
) -> list[dict[str, Any]]:
    """Recover a summary only when its raw source has no gaps or disagreements.

    Source reads share the grep scan's existing character bound and the caller's
    optional deadline. Failed or incomplete reads retain the summary and its warning.
    """
    recovered: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    remaining = max_chars - sum(len(str(row.get("content") or "")) for row in chunks)
    for key, (start, end) in _summary_spans(chunks).items():
        if remaining <= 0:
            break
        if budget is not None and not budget.allows(150):
            budget.drop("grep source recovery skipped")
            break
        path, model, _ = key
        try:
            with code_span("grep.source", path=path) as span:
                async with asyncio.timeout(None if budget is None else budget.left_ms() / 1000):
                    rows = await repo.grep_source_chunks_for_span(org, ns, path, start, end, model)
                    chars = sum(len(str(row.get("content") or "")) for row in rows)
                    body = ""
                    if len(rows) <= 400 and chars <= remaining:
                        known = [
                            row
                            for row in chunks
                            if row.get("kind") != "class_skeleton"
                            and row.get("path") == path
                            and row.get("embedding_model") == model
                            and int(row["start_line"]) <= end
                            and int(row["end_line"]) >= start
                        ]
                        body = await asyncio.to_thread(
                            complete_source_span, rows + known, start, end, max_chars=0
                        )
                span_attrs(span, rows=len(rows))
        except TimeoutError:
            if budget is not None:
                budget.drop("grep source recovery timed out")
            break
        except Exception as exc:  # noqa: BLE001 — retain disclosed partial grep hits when source recovery fails
            logger.warning("code.grep source recovery failed for %s: %s", path, exc)
            continue
        remaining -= chars
        if remaining < 0:
            break
        if body:
            recovered[key] = [dict(row, path=path, embedding_model=model) for row in rows]
    if not recovered:
        return chunks
    output = [
        row
        for row in chunks
        if row.get("kind") != "class_skeleton"
        or (str(row["path"]), str(row.get("embedding_model") or ""), str(row.get("scope") or ""))
        not in recovered
    ]
    for rows in recovered.values():
        output.extend(rows)
    output.sort(key=lambda row: (str(row["path"]), int(row["start_line"]), int(row["end_line"])))
    return output
