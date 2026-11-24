"""Read batches of source ranges with per-file revision checks."""

from __future__ import annotations

import asyncio

from server.code.api.models_common import _safe_path
from server.code.api.models_read import CodeReadInput, CodeReadOutput, CodeReadRange, CodeReadWindow
from server.code.app.common import (
    index_as_of,
    path_coverage,
    require_code_intel,
    resolve_code_identity,
)
from server.code.app.index import gitrepo
from server.code.app.index.mirrors import present_dir
from server.code.app.spans import set_code_span_attributes
from server.code.domain.index.repos import repo_for_namespace
from server.code.domain.symbol_body import retained_source_ranges
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.embedding.embedder import served_model_label
from server.settings import settings


def _parse_range(value: str) -> tuple[str, int, int]:
    path, colon, lines = value.rpartition(":")
    path = path.removeprefix("./")
    first, dash, last = lines.partition("-")
    last = last if dash else first
    if (
        not colon
        or not _safe_path(path)
        or "\0" in path
        or not all(text.isascii() and text.isdecimal() for text in (first, last))
    ):
        raise ValidationError("ranges must use repo-relative path:start-end")
    start, end = int(first), int(last)
    if not 1 <= start <= end < 2**32:
        raise ValidationError("ranges must have a positive start and an end at or after the start")
    return path, start, end


async def _verified_blob(
    mirror: asyncio.Future[str], path: str, commit: str, expected: str
) -> str | None:
    git_dir = await mirror
    if not git_dir or not expected:
        return None
    try:
        raw = await gitrepo.show_file(
            git_dir, commit, path, max_bytes=settings.code_ingest_max_file_bytes
        )
        if raw is not None and gitrepo.blob_sha(raw) == expected:
            return raw.decode("utf-8")
    except (gitrepo.GitError, UnicodeDecodeError):
        return None
    return None


def _blob_window(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    lines = text.split("\n")
    terminated = lines[-1] == ""
    if terminated:
        lines.pop()
    last = min(end, len(lines))
    if last < start:
        return []
    source = "\n".join(lines[start - 1 : last])
    if terminated or last < len(lines):
        source += "\n"
    return [(start, last, source)]


async def handle_code_read(input: CodeReadInput) -> CodeReadOutput:
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    requested = [_parse_range(value) for value in input.ranges]
    cfg = repo_for_namespace(org, ns)
    paths = list(dict.fromkeys(path for path, _, _ in requested if path_coverage(cfg, path)[0]))
    repo = CodeRepo()
    files, as_of = await asyncio.gather(
        repo.file_rows_for_paths(org, ns, paths), index_as_of(repo, org, ns)
    )
    model = served_model_label(settings.embedding_model) if settings.embedding_model else ""
    mirror: asyncio.Task[str] | None = None
    blobs: dict[str, asyncio.Task[str | None]] = {}

    async def read_span(path: str, start: int, end: int) -> CodeReadRange:
        nonlocal mirror
        result = CodeReadRange(path=path, start_line=start, end_line=end, status="not_indexed")
        if (file := files.get(path)) is None:
            return result
        result.indexed_commit = file["indexed_commit"]
        if file["loc"] and start > file["loc"]:
            result.status = "out_of_range"
            return result
        result.status = "unavailable"
        if not result.indexed_commit:
            return result
        windows = []
        if model:
            rows = await repo.source_chunks_for_span(
                org, ns, path, start, end, model, with_revision=True
            )
            if len(rows) <= 400:
                windows = await asyncio.to_thread(
                    retained_source_ranges, rows, start, end, revision=result.indexed_commit
                )
        if not _complete(windows, start, end) and file.get("blob_sha"):
            if path not in blobs:
                if mirror is None:
                    mirror = asyncio.create_task(present_dir(cfg))
                blobs[path] = asyncio.create_task(
                    _verified_blob(mirror, path, result.indexed_commit, file["blob_sha"])
                )
            if (blob := await blobs[path]) is not None:
                windows = _blob_window(blob, start, end)
        result.windows = [
            CodeReadWindow(start_line=lo, end_line=hi, text=text) for lo, hi, text in windows
        ]
        if windows:
            result.status = "complete" if _complete(windows, start, end) else "partial"
        return result

    results = await asyncio.gather(*(read_span(*span) for span in requested))
    set_code_span_attributes(
        route="retained_ranges",
        results=sum(bool(result.windows) for result in results),
        partial=any(result.status == "partial" for result in results),
        consumer=input.consumer,
    )
    return CodeReadOutput(results=results, as_of=as_of)


def _complete(windows: list[tuple[int, int, str]], start: int, end: int) -> bool:
    return len(windows) == 1 and windows[0][:2] == (start, end)
