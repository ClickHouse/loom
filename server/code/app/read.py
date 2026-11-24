"""Read batched source ranges from verified indexed Git blobs."""

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
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
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


async def _verified_blob(git_dir: str, path: str, commit: str, expected: str) -> list[str] | None:
    try:
        raw = await gitrepo.show_file(
            git_dir, commit, path, max_bytes=settings.code_ingest_max_file_bytes
        )
        if raw is not None and gitrepo.blob_sha(raw) == expected:
            pieces = raw.decode("utf-8").split("\n")
            lines = [line + "\n" for line in pieces[:-1]]
            if pieces[-1]:
                lines.append(pieces[-1])
            return lines
    except (gitrepo.BlobTooLarge, gitrepo.GitError, UnicodeDecodeError):
        return None
    return None


async def handle_code_read(input: CodeReadInput) -> CodeReadOutput:
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    requested = [_parse_range(value) for value in input.ranges]
    cfg = repo_for_namespace(org, ns)
    paths = list(dict.fromkeys(path for path, _, _ in requested if path_coverage(cfg, path)[0]))
    repo = CodeRepo()
    files, as_of, git_dir = await asyncio.gather(
        repo.file_rows_for_paths(org, ns, paths), index_as_of(repo, org, ns), present_dir(cfg)
    )
    eligible = {
        path: file
        for path, file in files.items()
        if git_dir and file["chunk_count"] > 0 and file["indexed_commit"] and file["blob_sha"]
    }
    bodies = await asyncio.gather(
        *(
            _verified_blob(git_dir, path, file["indexed_commit"], file["blob_sha"])
            for path, file in eligible.items()
        )
    )
    source_files = dict(zip(eligible, bodies, strict=True))
    results = []
    for path, start, end in requested:
        result = CodeReadRange(path=path, start_line=start, end_line=end, status="not_indexed")
        results.append(result)
        if (file := files.get(path)) is None:
            continue
        result.indexed_commit = file["indexed_commit"]
        lines = source_files.get(path)
        if lines is None:
            result.status = "unavailable"
        elif start > len(lines):
            result.status = "out_of_range"
        else:
            last = min(end, len(lines))
            result.windows = [
                CodeReadWindow(
                    start_line=start, end_line=last, text="".join(lines[start - 1 : last])
                )
            ]
            result.status = "complete" if end == last else "partial"

    set_code_span_attributes(
        route="verified_blob_ranges",
        results=sum(bool(result.windows) for result in results),
        partial=any(result.status == "partial" for result in results),
        consumer=input.consumer,
    )
    return CodeReadOutput(results=results, as_of=as_of)
