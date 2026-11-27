"""Read batched source ranges from verified indexed Git blobs."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from server.code.api.models_common import _safe_path
from server.code.api.models_read import (
    CodeReadInput,
    CodeReadOutput,
    CodeReadRange,
    CodeReadRangeInput,
    CodeReadWindow,
)
from server.code.app.common import (
    index_as_of,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.index import gitrepo
from server.code.app.index.denylist import under_prefixes
from server.code.app.index.mirrors import present_dir
from server.code.app.spans import set_code_span_attributes
from server.code.domain.extract.py_notebooks import is_notebook_path
from server.code.domain.index.repos import repo_for_namespace
from server.code.domain.units import estimate_tokens
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.core.hashing import sha256_value
from server.settings import settings

# Bound byte buffering independently of whether an index limits ingest size.
_SOURCE_BYTES_PER_REQUEST = 8_000_000


def _checked_range(path: str, start: int, end: int | None) -> tuple[str, int, int]:
    path = path.removeprefix("./")
    if not _safe_path(path) or "\0" in path:
        raise ValidationError("Source ranges must use a repo-relative path")
    end = start if end is None else end
    if not 1 <= start <= end < 2**32:
        raise ValidationError("ranges must have a positive start and an end at or after the start")
    return path, start, end


def _parse_range(
    value: str | CodeReadRangeInput | list[int], shared_path: str | None = None
) -> tuple[str, int, int]:
    if isinstance(value, list):
        if shared_path is None:
            raise ValidationError("Line pairs require a repository-relative path")
        start, end = value
        return _checked_range(shared_path, start, end)
    if isinstance(value, CodeReadRangeInput):
        return _checked_range(value.path, value.start_line, value.end_line)
    path, colon, lines = value.rpartition(":")
    first, dash, last = lines.partition("-")
    last = last if dash else first
    if not colon or not all(text.isascii() and text.isdecimal() for text in (first, last)):
        raise ValidationError("ranges must use repo-relative path:start-end")
    return _checked_range(path, int(first), int(last))


def _request_ranges(input: CodeReadInput) -> list[tuple[str, int, int]]:
    if input.ranges and isinstance(input.ranges[0], list):
        if input.start_line is not None or input.end_line is not None:
            raise ValidationError("Put both line bounds in each range when using line pairs")
        return [_parse_range(value, input.path) for value in input.ranges]
    single = input.path is not None or input.start_line is not None or input.end_line is not None
    if single:
        if input.ranges or input.path is None or input.start_line is None:
            raise ValidationError("Provide ranges or path and start_line with optional end_line")
        return [_checked_range(input.path, input.start_line, input.end_line)]
    if not input.ranges:
        raise ValidationError("Provide at least one source range")
    return [_parse_range(value) for value in input.ranges]


async def _verified_blob(
    git_dir: str,
    path: str,
    commit: str,
    expected: str,
    *,
    max_bytes: int = _SOURCE_BYTES_PER_REQUEST,
) -> tuple[bytes | None, int]:
    limit = min(settings.code_ingest_max_file_bytes or max_bytes, max_bytes)
    try:
        raw = await gitrepo.show_file(git_dir, commit, path, max_bytes=limit)
    except gitrepo.BlobTooLarge as exc:
        return None, len(exc.head)
    except gitrepo.GitError:
        return None, max(limit, gitrepo.OVERSIZE_HEAD_BYTES)
    if raw is None:
        return None, 0
    spent = len(raw)
    if gitrepo.blob_sha(raw) != expected:
        return None, spent
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, spent
    return raw, spent


def _line_offsets(raw: bytes, lines: set[int]) -> dict[int, int]:
    """Find only the boundaries the caller needs, including the offset at EOF."""
    offsets = {}
    current, offset = 1, 0
    for line in sorted(lines):
        while current < line:
            boundary = raw.find(b"\n", offset)
            offset = len(raw) if boundary < 0 else boundary + 1
            current += 1
        offsets[line] = offset
    return offsets


def _fill_ranges(raw: bytes, ranges: list[CodeReadRange], remaining: int) -> int:
    line_count = raw.count(b"\n") + int(bool(raw) and not raw.endswith(b"\n"))
    endpoints = [(r, min(r.end_line, line_count)) for r in ranges if r.start_line <= line_count]
    boundaries = _line_offsets(
        raw, {line for r, last in endpoints for line in (r.start_line, last + 1)}
    )
    for result in ranges:
        if result.start_line > line_count:
            result.status = "out_of_range"
    for result, last in endpoints:
        start, end = boundaries[result.start_line], boundaries[last + 1]
        if end - start > remaining:
            continue
        result.windows = [
            CodeReadWindow(
                start_line=result.start_line, end_line=last, text=raw[start:end].decode()
            )
        ]
        result.status = "complete" if result.end_line == last else "partial"
        remaining -= end - start
    return remaining


async def fill_verified_ranges(
    repo: CodeRepo,
    org: str,
    ns: str,
    results: list[CodeReadRange],
    *,
    expected_revisions: dict[str, str] | None = None,
    max_source_bytes: int | None = None,
) -> None:
    """Fill source ranges after one file metadata lookup and one blob read per path."""
    cfg = repo_for_namespace(org, ns)
    prefixes = cfg.path_prefixes if cfg is not None else ()
    by_path: dict[str, list[CodeReadRange]] = defaultdict(list)
    for result in results:
        if (
            _safe_path(result.path)
            and "\0" not in result.path
            and under_prefixes(prefixes, result.path)
        ):
            by_path[result.path].append(result)
    if not by_path:
        return
    files, git_dir = await asyncio.gather(
        repo.file_rows_for_paths(org, ns, list(by_path)), present_dir(cfg)
    )
    remaining = _SOURCE_BYTES_PER_REQUEST
    for path, ranges in by_path.items():
        # Git may read a short preview of an oversized blob before refusing it.
        if max_source_bytes is not None and max_source_bytes < gitrepo.OVERSIZE_HEAD_BYTES:
            break
        if (file := files.get(path)) is None:
            continue
        commit = file["indexed_commit"]
        if expected_revisions is not None and commit != expected_revisions.get(path):
            continue
        for result in ranges:
            result.indexed_commit = commit
            result.status = "unavailable"
        blob_limit = _SOURCE_BYTES_PER_REQUEST if max_source_bytes is None else max_source_bytes
        if (
            git_dir
            and remaining
            and file["chunk_count"] > 0
            and commit
            and file["blob_sha"]
            and file["lang"] != "reference"
            and not is_notebook_path(path)
        ):
            raw, spent = await _verified_blob(
                git_dir, path, commit, file["blob_sha"], max_bytes=blob_limit
            )
            if max_source_bytes is not None:
                max_source_bytes -= spent
            if raw is not None:
                remaining = _fill_ranges(raw, ranges, remaining)


async def handle_code_read(input: CodeReadInput) -> CodeReadOutput:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    results = [
        CodeReadRange(path=path, start_line=start, end_line=end, status="not_indexed")
        for path, start, end in _request_ranges(input)
    ]
    repo = CodeRepo()
    as_of, _ = await asyncio.gather(
        index_as_of(repo, org, ns), fill_verified_ranges(repo, org, ns, results)
    )

    route = "verified_blob_ranges"
    served = [result for result in results if result.windows]
    tokens = sum(
        estimate_tokens(window.text, result.path) for result in served for window in result.windows
    )
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.read",
        query="",
        route=route,
        returned_ids=[
            sha256_value((result.path, result.indexed_commit, window.start_line, window.end_line))
            for result in served
            for window in result.windows
        ],
        returned_paths=[result.path for result in served],
        returned_scores=[],
        top_k=len(results),
        tokens_returned=tokens,
        abstain=not served,
        retrieval_risk="low" if served else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        as_of=as_of,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
    )
    set_code_span_attributes(
        route=route,
        results=len(served),
        tokens_returned=tokens,
        abstain=not served,
        truncated=any(result.status == "partial" for result in results),
        consumer=input.consumer,
    )
    return CodeReadOutput(results=results, as_of=as_of, trace_run_id=trace_run_id)
