"""code.files: the namespace's paths under a prefix or glob, each with its reference
siblings."""

from __future__ import annotations

import logging
import time
from typing import Any

from server.code.api.models_grep import CodeFilesOutput
from server.code.app.common import (
    index_as_of,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.search.shared import _not_covered_output
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)


async def attach_reference_siblings(
    repo: Any, org: str, ns: str, rows: list[dict[str, Any]], *, key: str = "path"
) -> None:
    """Every stateless TEST row gains `reference` — the path of its indexed expectation file,
    or None. One membership read over the file table for all candidates; rows that are not
    tests (source files, the fixtures themselves) are left as they were."""
    from server.code.domain.extract.lang_reference import (
        reference_candidates,
        reference_sibling,
    )

    cands: dict[int, tuple[str, ...]] = {}
    for i, r in enumerate(rows):
        c = reference_candidates(str(r.get(key) or ""))
        if c:
            cands[i] = c
    if not cands:
        return
    try:
        present = await repo.live_paths_present(org, ns, [p for c in cands.values() for p in c])
    except Exception as e:  # noqa: BLE001 — the sibling is decoration, never the answer
        logger.debug("code.files: reference sibling read skipped: %s", e)
        present = set()
    for i in cands:
        rows[i]["reference"] = reference_sibling(str(rows[i].get(key) or ""), present)


async def handle_code_files(input: Any) -> Any:
    """code.files: indexed files by name — an rg-style glob, a basename stem, a path prefix —
    from loom.code_files, no chunk read. The answer to "does test 05138_with_fill exist, and
    where" in one call; grep matches content, not paths, so it cannot give it."""
    from server.code.domain.search_scope import glob_to_re2
    from server.core.errors import ValidationError

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    if not (input.glob or input.stem or input.path_prefix):
        raise ValidationError("code.files needs a glob, a stem or a path_prefix")
    not_covered = _not_covered_output(org, ns, input.path_prefix, CodeFilesOutput)
    if not_covered is not None:
        not_covered.as_of = await index_as_of(CodeRepo(), org, ns)
        set_code_span_attributes(
            route="not_covered",
            results=0,
            abstain=True,
            not_covered=True,
            retrieval_risk="abstain",
            consumer=input.consumer,
        )
        return not_covered
    repo = CodeRepo()
    glob_re = glob_to_re2(input.glob) if input.glob else ""
    route = "files" + ("+glob" if glob_re else "") + ("+stem" if input.stem else "")
    with code_span("files.list", route=route) as sp:
        files, total = await repo.list_files(
            org,
            ns,
            glob_re=glob_re,
            stem=input.stem.strip(),
            path_prefix=input.path_prefix,
            limit=input.limit,
        )
        span_attrs(sp, results=len(files), total=total)
    await attach_reference_siblings(repo, org, ns, files)
    paths = [str(f["path"]) for f in files]
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.files",
        query=" ".join(x for x in (input.glob, input.stem, input.path_prefix) if x) or "-",
        route=route,
        returned_ids=paths,
        returned_paths=paths,
        returned_scores=[],
        top_k=input.limit,
        tokens_returned=sum(len(p) for p in paths) // 4,
        abstain=not files,
        retrieval_risk="low" if files else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        path_prefix=input.path_prefix,
    )
    set_code_span_attributes(
        route=route,
        results=len(files),
        truncated=total > len(files),
        path_prefix=input.path_prefix,
        abstain=not files,
        retrieval_risk="low" if files else "abstain",
        consumer=input.consumer,
    )
    return CodeFilesOutput(
        files=files,
        total=total,
        truncated=total > len(files),
        as_of=await index_as_of(repo, org, ns),
        trace_run_id=trace_run_id,
    )
