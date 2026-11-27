"""Replace unmappable summaries with verified retained source for exact grep."""

from __future__ import annotations

import asyncio
import logging
from io import StringIO
from typing import Any

from server.code.api.models_read import CodeReadRange
from server.code.app.read import fill_verified_ranges
from server.code.app.spans import code_span, span_attrs
from server.code.domain.deadline import Budget
from server.code.domain.symbol_body import complete_source_span, retained_source_lines
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)


ContextSource = dict[str, dict[str, str | None]]
CompleteContexts = frozenset[tuple[str, int]]
GREP_MATCH_TEXT_CHARS = 300


def match_revisions(
    matches: list[dict[str, Any]], commits: dict[tuple[str, int], str]
) -> dict[str, str]:
    """Use a path's revision only when every returned hit agrees."""
    revisions: dict[str, str] = {}
    for hit in matches:
        path = str(hit["path"])
        commit = commits.get((path, int(hit["line"])), "")
        if revisions.setdefault(path, commit) != commit:
            revisions[path] = ""
    return {path: commit for path, commit in revisions.items() if commit}


def _complete_excerpt(hit: dict[str, Any], line: int, window: range) -> range:
    end = hit.get("scope_end_line")
    if (
        not hit.get("scope")
        or hit.get("scope_start_line") != line
        or not isinstance(end, int)
        or not window.stop <= end < line + len(window)
    ):
        return window
    return range(end + 1 - len(window), end + 1)


def _retains_window(source: dict[str, str | None], window: range) -> bool:
    last = window.stop - 1
    return source.get(str(last)) is not None and all(
        (source.get(str(line)) or "").endswith("\n") for line in range(window.start, last)
    )


def _centered_window(line: int, radius: int) -> range:
    return range(max(1, line - radius), line + radius + 1)


async def verify_complete_contexts(
    repo: CodeRepo,
    org: str,
    ns: str,
    chunks: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    commits: dict[tuple[str, int], str],
    radius: int,
    budget: Budget | None,
) -> CompleteContexts:
    """Only the parser's current extent can confirm the end of a split declaration."""
    candidates = {}
    for hit in matches:
        path, line = str(hit["path"]), int(hit["line"])
        window = _centered_window(line, radius)
        if (commit := commits.get((path, line))) and _complete_excerpt(hit, line, window) != window:
            candidates[path, str(hit["scope"]), line, commit] = hit["scope_end_line"]
    if not candidates:
        return frozenset()
    scopes = {(path, scope) for path, scope, _, _ in candidates}
    ids = sorted(
        {
            str(sid)
            for chunk in chunks
            if (str(chunk["path"]), str(chunk.get("scope") or "")) in scopes
            for sid in (chunk.get("symbol_ids") or [])
            if sid
        }
    )
    if not ids or len(ids) > 500 or (budget is not None and budget.left_ms() <= 0):
        return frozenset()
    try:
        async with asyncio.timeout(None if budget is None else budget.left_ms() / 1000):
            rows = await repo.symbol_spans_by_ids(org, ns, ids, source_only=True)
    except Exception as exc:  # noqa: BLE001 — keep centered excerpts if the optional lookup fails
        logger.debug("grep declaration extent lookup failed: %s", type(exc).__name__)
        return frozenset()
    ends: dict[tuple[str, str, int, str], set[int]] = {}
    for row in rows:
        key = (
            str(row["path"]),
            str(row["qualified_name"]),
            int(row["start_line"]),
            str(row["extent_commit"]).rstrip("\0"),
        )
        ends.setdefault(key, set()).add(int(row["end_line"]))
    return frozenset((key[0], key[2]) for key, end in candidates.items() if ends.get(key) == {end})


def _wanted_lines(
    matches: list[dict[str, Any]],
    radius: int,
    *,
    complete_contexts: CompleteContexts,
    source: ContextSource | None = None,
    include_incomplete_preferred: bool = False,
) -> dict[str, set[int]]:
    wanted: dict[str, set[int]] = {}
    for hit in matches:
        line = int(hit["line"])
        path = str(hit["path"])
        window = _centered_window(line, radius)
        if (path, line) in complete_contexts and (
            complete := _complete_excerpt(hit, line, window)
        ) != window:
            if source is not None and _retains_window(source.get(path, {}), complete):
                window = complete
            elif source is None or include_incomplete_preferred:
                window = range(window.start, complete.stop)
        wanted.setdefault(path, set()).update(window)
    return wanted


def collect_contexts(
    chunks: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    radius: int,
    max_chars: int,
    *,
    match_commits: dict[tuple[str, int], str],
    complete_contexts: CompleteContexts = frozenset(),
) -> ContextSource:
    """Keep context at each path's verified match revision, with omissions marked."""
    wanted = _wanted_lines(matches, radius, complete_contexts=complete_contexts)
    revisions = match_revisions(matches, match_commits)
    by_path: ContextSource = {
        path: {str(line): None for line in lines}
        for path, lines in wanted.items()
        if path not in revisions
    }
    other_revisions: dict[str, set[str]] = {}
    scanned = 0
    for chunk in chunks:
        path = str(chunk["path"])
        scanned += len(str(chunk.get("content") or ""))
        if scanned > max_chars:
            break
        if path not in revisions or (lines := retained_source_lines(chunk)) is None:
            continue
        commit = str(chunk.get("indexed_commit") or "").rstrip("\0")
        if commit != revisions[path]:
            start = int(chunk["start_line"])
            other_revisions.setdefault(path, set()).update(
                str(line) for line in wanted[path].intersection(range(start, start + len(lines)))
            )
            continue
        source = by_path.setdefault(path, {})
        for line, text in enumerate(lines, int(chunk["start_line"])):
            if line in wanted[path]:
                key = str(line)
                if source.setdefault(key, text) != text:
                    source[key] = None
    for path, keys in other_revisions.items():
        source = by_path.setdefault(path, {})
        for key in keys:
            source.setdefault(key, None)
    return by_path


def page_contexts(
    source: ContextSource,
    matches: list[dict[str, Any]],
    radius: int,
    *,
    complete_contexts: CompleteContexts = frozenset(),
) -> dict[str, Any]:
    """Render only this page's windows from retained lines that agree."""
    wanted = _wanted_lines(matches, radius, complete_contexts=complete_contexts, source=source)
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


def _missing_context_ranges(
    source: ContextSource, wanted: dict[str, set[int]]
) -> list[CodeReadRange]:
    ranges: list[CodeReadRange] = []
    for path, lines in wanted.items():
        retained = source.setdefault(path, {})
        for line in sorted(lines):
            key = str(line)
            if retained.get(key) is not None:
                continue
            retained[key] = None
            if ranges and ranges[-1].path == path and ranges[-1].end_line + 1 == line:
                ranges[-1].end_line = line
            else:
                ranges.append(
                    CodeReadRange(path=path, start_line=line, end_line=line, status="not_indexed")
                )
    return ranges


def _merge_verified_context(source: ContextSource, ranges: list[CodeReadRange]) -> None:
    for result in ranges:
        retained = source[result.path]
        if result.windows:
            window = result.windows[0]
            lines = StringIO(window.text, newline="\n")
            for line, text in enumerate(lines, window.start_line):
                retained[str(line)] = text
            eof_start = window.end_line + 1
        elif result.status == "out_of_range":
            eof_start = result.start_line
        else:
            continue
        for line in range(eof_start, result.end_line + 1):
            retained.pop(str(line), None)


async def recover_page_context(
    repo: CodeRepo,
    org: str,
    ns: str,
    source: ContextSource,
    matches: list[dict[str, Any]],
    radius: int,
    revisions: dict[str, str],
    *,
    max_source_bytes: int,
    complete_contexts: CompleteContexts = frozenset(),
    budget: Budget | None = None,
) -> None:
    """Fill gaps around served hits only at revisions agreed by the full pool."""
    wanted = _wanted_lines(
        matches,
        radius,
        complete_contexts=complete_contexts,
        source=source,
        include_incomplete_preferred=True,
    )
    missing = _missing_context_ranges(source, wanted)
    if not missing:
        return
    readable = [result for result in missing if result.path in revisions]
    with code_span("grep.context_recovery") as span:
        if not readable:
            span_attrs(span, abstain=True, reason="revision_unavailable")
            return
        if budget is not None and budget.left_ms() <= 0:
            budget.drop("grep context recovery skipped")
            span_attrs(span, abstain=True, reason="deadline")
            return
        try:
            async with asyncio.timeout(None if budget is None else budget.left_ms() / 1000):
                await fill_verified_ranges(
                    repo,
                    org,
                    ns,
                    readable,
                    expected_revisions=revisions,
                    max_source_bytes=max_source_bytes,
                )
        except TimeoutError:
            if budget is not None:
                budget.drop("grep context recovery timed out")
            span_attrs(span, reason="deadline")
        except Exception as exc:  # noqa: BLE001 — keep the surrounding indexed evidence when recovery fails
            logger.debug("grep context recovery failed: %s", type(exc).__name__)
            span_attrs(span, reason="source_unavailable")
        recovered = sum(bool(result.windows) for result in readable)
        eof = sum(result.status == "out_of_range" for result in readable)
        span_attrs(span, results=recovered, truncated=recovered + eof < len(missing))
    _merge_verified_context(source, readable)


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
    source_locs = {
        (row["path"], row.get("indexed_commit")): loc
        for row in chunks
        if (loc := row.get("source_loc"))
    }
    output = [
        row
        for row in chunks
        if row.get("kind") != "class_skeleton"
        or (str(row["path"]), str(row.get("embedding_model") or ""), str(row.get("scope") or ""))
        not in recovered
    ]
    for rows in recovered.values():
        for row in rows:
            if loc := source_locs.get((row["path"], row.get("indexed_commit"))):
                row["source_loc"] = loc
        output.extend(rows)
    output.sort(key=lambda row: (str(row["path"]), int(row["start_line"]), int(row["end_line"])))
    return output
