"""Read batched source ranges from verified indexed Git blobs."""

from __future__ import annotations

import asyncio
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
    require_code_intel,
    resolve_code_identity,
)
from server.code.app.index import gitrepo
from server.code.app.index.denylist import under_prefixes
from server.code.app.index.mirrors import present_dir
from server.code.app.spans import set_code_span_attributes
from server.code.domain.extract.py_notebooks import is_notebook_path
from server.code.domain.index.repos import repo_for_namespace
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
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


def _parse_range(value: str | CodeReadRangeInput) -> tuple[str, int, int]:
    if isinstance(value, CodeReadRangeInput):
        return _checked_range(value.path, value.start_line, value.end_line)
    path, colon, lines = value.rpartition(":")
    first, dash, last = lines.partition("-")
    last = last if dash else first
    if not colon or not all(text.isascii() and text.isdecimal() for text in (first, last)):
        raise ValidationError("ranges must use repo-relative path:start-end")
    return _checked_range(path, int(first), int(last))


def _request_ranges(input: CodeReadInput) -> list[tuple[str, int, int]]:
    single = input.path is not None or input.start_line is not None or input.end_line is not None
    if single:
        if input.ranges or input.path is None or input.start_line is None:
            raise ValidationError("Provide ranges or path and start_line with optional end_line")
        return [_checked_range(input.path, input.start_line, input.end_line)]
    if not input.ranges:
        raise ValidationError("Provide at least one source range")
    return [_parse_range(value) for value in input.ranges]


async def _verified_blob(git_dir: str, path: str, commit: str, expected: str) -> bytes | None:
    limit = min(
        settings.code_ingest_max_file_bytes or _SOURCE_BYTES_PER_REQUEST, _SOURCE_BYTES_PER_REQUEST
    )
    try:
        raw = await gitrepo.show_file(git_dir, commit, path, max_bytes=limit)
        if raw is not None and gitrepo.blob_sha(raw) == expected:
            raw.decode("utf-8")
            return raw
    except (gitrepo.BlobTooLarge, gitrepo.GitError, UnicodeDecodeError):
        return None
    return None


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


async def handle_code_read(input: CodeReadInput) -> CodeReadOutput:
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    requested = _request_ranges(input)
    cfg = repo_for_namespace(org, ns)
    prefixes = cfg.path_prefixes if cfg is not None else ()
    results = [
        CodeReadRange(path=path, start_line=start, end_line=end, status="not_indexed")
        for path, start, end in requested
    ]
    by_path: dict[str, list[CodeReadRange]] = defaultdict(list)
    for result in results:
        if under_prefixes(prefixes, result.path):
            by_path[result.path].append(result)
    repo = CodeRepo()
    files, as_of, git_dir = await asyncio.gather(
        repo.file_rows_for_paths(org, ns, list(by_path)),
        index_as_of(repo, org, ns),
        present_dir(cfg),
    )
    remaining = _SOURCE_BYTES_PER_REQUEST
    for path, ranges in by_path.items():
        if (file := files.get(path)) is None:
            continue
        commit = file["indexed_commit"]
        for result in ranges:
            result.indexed_commit = commit
            result.status = "unavailable"
        if (
            git_dir
            and remaining
            and file["chunk_count"] > 0
            and commit
            and file["blob_sha"]
            and file["lang"] != "reference"
            and not is_notebook_path(path)
        ):
            raw = await _verified_blob(git_dir, path, commit, file["blob_sha"])
            if raw is not None:
                remaining = _fill_ranges(raw, ranges, remaining)

    set_code_span_attributes(
        route="verified_blob_ranges",
        results=sum(bool(result.windows) for result in results),
        partial=any(result.status == "partial" for result in results),
        consumer=input.consumer,
    )
    return CodeReadOutput(results=results, as_of=as_of)
