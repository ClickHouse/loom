"""code.grep: literal and regex lines over the live chunks (with neighbours and a
cursor), the file-name mode and the commit-message mode."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from server.code.api.models_grep import CodeGrepInput, CodeGrepOutput
from server.code.app.common import (
    answer_args,
    index_as_of,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.search.shared import (
    _not_covered_output,
    _partial_fields,
    _read_pool_settling,
    _scope_of,
)
from server.code.app.spans import (
    code_span,
    current_stage_ms,
    set_code_span_attributes,
    span_attrs,
    start_stage_wall,
)
from server.code.domain import deadline as code_deadline
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


async def handle_code_grep(input: CodeGrepInput) -> CodeGrepOutput:
    """Exact literal / RE2 regex over indexed chunks, per-line results with the enclosing
    symbol attached. Matching lines are extracted app-side from the chunk content (line =
    chunk.start_line + offset).

    mode=files answers "which files"; mode=message takes a RENDERED message and finds the
    statement whose template printed it. A literal pattern that is regex-shaped and matches
    nothing is run once as a regex and says so in `note`."""
    from server.observability.budget_shape import note_budget_shape

    require_code_intel()
    # Presence on several namespaces: which of them contain the pattern, one row each. Unset:
    # matches / files on the one namespace.
    if input.mode == "presence":
        from server.code.app import presence as code_presence

        return await code_presence.grep_presence(input, handle_code_grep)
    # Files and message are priced by their own p99 budgets
    # (Operation.shape_budgets_p99_ms); matches is the base budget.
    note_budget_shape("" if input.mode == "matches" else input.mode)
    started = time.monotonic()
    start_stage_wall()
    org, ns = resolve_code_identity(input.org, input.namespace)
    route = "grep_regex" if input.regex else "grep_literal"
    if input.mode == "message":
        route = "grep_message"

    not_covered = _not_covered_output(org, ns, input.path_prefix, CodeGrepOutput)
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

    import re as _re

    if input.regex:
        try:
            line_re: _re.Pattern[str] | None = _re.compile(input.pattern)
        except _re.error as e:
            from server.core.errors import ValidationError

            raise ValidationError(f"invalid regex: {e}") from e
    else:
        line_re = None

    # rg -g / -t style file filters; unset = no SQL added
    scope = _scope_of(input)
    if scope.has_path_filters:
        route += "+scoped"
    # a path_prefix narrows the statement's read to that key range: the route says so and the
    # ledger row carries the prefix, so an abstain is read against the scope it ran under
    if input.path_prefix:
        route += "+prefix"

    budget = code_deadline.start(input.deadline_ms, started=started)
    if input.mode == "message":
        return await _grep_message(
            repo, org, ns, input, route=route, started=started, scope=scope, budget=budget
        )
    if input.mode == "files":
        return await _grep_files(
            repo, org, ns, input, route=route, started=started, scope=scope, budget=budget
        )

    # A continuation is a point read of the lines the first call extracted; a missing pool row
    # re-runs the grep and says so.
    if input.cursor:
        page, fallback, offset = await _serve_grep_page(input, org, ns, repo, route, started)
        if page is not None:
            return page
        return await _grep_page_by_rerun(input, offset=offset, reason=fallback)
    return await _grep_lines(
        repo,
        org,
        ns,
        input,
        route=route,
        started=started,
        scope=scope,
        line_re=line_re,
        budget=budget,
    )


async def _fetch_grep_chunks(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    *,
    scope: Any,
    limit: int,
    line_re: Any,
    budget: code_deadline.Budget | None = None,
) -> tuple[list[dict[str, Any]], Any, str, str]:
    """The literal/regex chunk read; when a LITERAL pattern is regex-shaped and admits no chunk,
    the same read once more as a regex. Returns (chunks, line matcher, route suffix, note): "" /
    "" when the literal read answered. Under a budget each read stops at what is left, and the
    second is skipped when nothing is."""
    from server.code.app import message_grep as mg

    kw = {"scope": scope} if scope.has_path_filters else {}

    async def _read(regex: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await _bounded_scan(
            budget,
            repo.grep_chunks,
            org,
            ns,
            input.pattern,
            regex=regex,
            case_sensitive=input.case_sensitive,
            path_prefix=input.path_prefix,
            limit=limit,
            **kw,
        )
        return rows

    chunks = await _read(input.regex)
    if chunks or input.regex or not mg.regex_shaped(input.pattern):
        return chunks, line_re, "", ""
    fallback = mg.compile_fallback(input.pattern)
    if fallback is None:
        return chunks, line_re, "", mg.REGEX_INVALID_NOTE
    if not _second_scan_allowed(budget, "regex retry"):
        return chunks, line_re, "", ""
    chunks = await _read(True)
    return chunks, fallback, "+regex_fallback", (mg.REGEX_FALLBACK_NOTE if chunks else "")


# What a bounded grep keeps back from its scan for the reads after it (unit
# spans, the freshness block).
_GREP_RESERVE_MS = 150


def _second_scan_allowed(budget: code_deadline.Budget | None, what: str) -> bool:
    """A second scan (the regex retry, the message literal fallback) starts only when the
    first finished and the budget has room for another; otherwise it is recorded as
    skipped. Always without a budget."""
    if budget is None:
        return True
    if budget.partial or not budget.allows(2 * _GREP_RESERVE_MS):
        budget.drop(f"{what} skipped")
        return False
    return True


async def _bounded_scan(
    budget: code_deadline.Budget | None, read: Any, *args: Any, **kw: Any
) -> Any:
    """One grep scan statement, bounded by what the budget has left: ClickHouse stops it
    there and hands back the rows read so far (`timeout_overflow_mode = 'break'`). A scan
    that ran to its bound is recorded on the budget as stopped. Unbounded without one."""
    if budget is None:
        return await read(*args, **kw)
    limit_s = budget.statement_s(_GREP_RESERVE_MS)
    t0 = time.monotonic()
    out = await read(*args, max_execution_s=limit_s, **kw)
    if code_deadline.broke(time.monotonic() - t0, limit_s):
        budget.drop("scan stopped at the deadline; hits are those read so far")
    return out


def _as_of_early(repo: CodeRepo, org: str, ns: str) -> asyncio.Future[dict[str, Any]]:
    """The freshness block starts with the grep, not after it — it depends on neither the
    chunks nor the spans. `index_as_of` is cached 30 s per namespace and fail-open (never
    raises), so a request that errors before awaiting it leaves no failed task behind."""
    return asyncio.ensure_future(index_as_of(repo, org, ns))


async def _grep_lines(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    *,
    route: str,
    started: float,
    scope: Any,
    line_re: Any,
    budget: code_deadline.Budget | None = None,
) -> CodeGrepOutput:
    """mode=matches: matching lines with the enclosing unit's span."""
    limit = input.effective_limit
    as_of = _as_of_early(repo, org, ns)  # Beside the reads
    with code_span("grep.chunks", route=route) as sp:
        # over-fetch chunks: several lines can match inside one chunk, and
        # several chunks can be windows of one oversized unit
        chunks, line_re, suffix, note = await _fetch_grep_chunks(
            repo,
            org,
            ns,
            input,
            scope=scope,
            limit=min(200, limit * 3),
            line_re=line_re,
            budget=budget,
        )
        route += suffix
        span_attrs(sp, chunks_matched=len(chunks))

    spans = await _unit_spans(repo, org, ns, chunks)
    needle = input.pattern if input.case_sensitive else input.pattern.lower()
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    excluded_paths = set(input.exclude_unit_ids)
    # extract past the page: the extra lines become the continuation pool
    keep = limit + _GREP_POOL_EXTRA
    for c in chunks:
        if str(c["path"]) in excluded_paths:
            continue
        content = str(c["content"])
        lines = content.splitlines()
        # a continuation window of an oversized unit opens with a synthetic
        # "// … continued from <header>" line that is not in the file: drop
        # it so line numbers stay exact and the header cannot re-match
        if lines and lines[0].startswith(_CONTINUED_PREFIX):
            lines = lines[1:]
        scope = str(c["scope"] or "")
        span = spans.get((str(c["path"]), scope), (0, 0)) if scope else (0, 0)
        for off, line in enumerate(lines):
            hay = line if input.case_sensitive else line.lower()
            hit = line_re.search(line) if line_re is not None else (needle in hay)
            if not hit:
                continue
            key = (str(c["path"]), int(c["start_line"] or 1) + off)
            if key in seen:
                continue  # the same span indexed twice (re-parse race)
            seen.add(key)
            matches.append(
                {
                    "path": key[0],
                    "line": key[1],
                    "text": line.strip()[:300],
                    "scope": scope,
                    "scope_start_line": span[0],
                    "scope_end_line": span[1],
                }
            )
            if len(matches) > keep:
                break
        if len(matches) > keep:
            break

    truncated = len(matches) > limit or len(chunks) >= min(200, limit * 3)
    trace_run_id = new_trace_run_id()
    next_cursor = _issue_grep_cursor(
        input,
        org,
        ns,
        matches,
        served=limit,
        run_id=trace_run_id,
        chunks_matched=len(chunks),
        truncated=truncated,
    )
    matches = matches[:limit]
    neighbours, anchors_capped, neighbour_tokens, route = await _grep_neighbours(
        repo, org, ns, input, matches, route, budget=budget
    )
    # one entry per matched FILE (ids and paths alike — grep has no unit ids,
    # so the outcome fold skips this op; the paths still feed opened_file)
    files = list(dict.fromkeys(m["path"] for m in matches))
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.grep",
        query=input.pattern,
        route=route,
        args=_grep_trace_args(input),
        returned_ids=files,
        returned_paths=files,
        returned_scores=[],
        top_k=limit,
        tokens_returned=sum(len(m["text"]) for m in matches) // 4 + neighbour_tokens,
        abstain=not matches,
        retrieval_risk="low" if matches else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        chunks_matched=len(chunks),
        path_prefix=input.path_prefix,
    )
    set_code_span_attributes(
        route=route,
        results=len(matches),
        chunks_matched=len(chunks),
        path_prefix=input.path_prefix,
        truncated=truncated,
        abstain=not matches,
        retrieval_risk="low" if matches else "abstain",
        consumer=input.consumer,
    )
    return CodeGrepOutput(
        matches=matches,
        next_cursor=next_cursor,
        has_more=bool(next_cursor),
        chunks_matched=len(chunks),
        truncated=truncated,
        as_of=await as_of,
        note=note,
        trace_run_id=trace_run_id,
        neighbours=neighbours,
        anchors=len(neighbours),
        anchors_capped=anchors_capped,
        neighbour_tokens=neighbour_tokens,
        **_partial_fields(budget),
    )


async def _grep_neighbours(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    matches: list[dict[str, Any]],
    route: str,
    *,
    budget: code_deadline.Budget | None = None,
) -> tuple[list[dict[str, Any]], bool, int, str]:
    """The enclosing-neighbours graph block from the hits in hand, in the same call. The matched
    paths' functions are read once (the read `code.enclosing` makes for the same hits), each
    hit resolves to its innermost function, and the distinct functions are the anchors of
    `_attach_enclosing_neighbours` — same 20-anchor cap, 0.8 confidence floor, external and
    plumbing filters, epistemic envelope. Only when the request sets `neighbours` (unset = off).
    Fail-open: a failed read costs the block,
    never the hits. Returns (entries, anchors_capped, tokens, route). Skipped when the budget
    has nothing left for it."""
    if not input.neighbours or not matches:
        return [], False, 0, route
    if budget is not None and not budget.allows(_GREP_RESERVE_MS):
        budget.drop("neighbours skipped")
        return [], False, 0, route
    from server.code.app.symbol import (
        ENCLOSING_KINDS,
        _attach_enclosing_neighbours,
        _innermost,
    )

    paths = list(dict.fromkeys(str(m["path"]) for m in matches))
    try:
        with code_span("grep.anchors", paths=len(paths)) as sp:
            rows = await repo.functions_for_paths(org, ns, paths, limit=2000, kinds=ENCLOSING_KINDS)
            span_attrs(sp, rows=len(rows))
        by_path: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_path.setdefault(str(r["path"]), []).append(r)
        anchors: list[dict[str, Any]] = []
        for m in matches:
            hit = _innermost(by_path.get(str(m["path"]), []), int(m["line"]))
            if hit is None:
                continue
            anchors.append(
                {
                    "symbol_id": str(hit.get("symbol_id") or ""),
                    "qualified_name": str(hit["qualified_name"]),
                    "symbol": str(
                        hit.get("name") or str(hit["qualified_name"]).rsplit("::", 1)[-1]
                    ),
                    "path": str(hit["path"]),
                    "kind": str(hit.get("kind") or "function"),
                }
            )
        entries, capped, tokens = await _attach_enclosing_neighbours(
            repo, org, ns, anchors, rows, input.neighbour_k
        )
    except Exception:
        logger.warning("code.grep: neighbours block failed for %r", input.pattern, exc_info=True)
        return [], False, 0, route
    return entries, capped, tokens, route + "+neighbours"


# server/code/domain/extract/chunker.py::_split_oversized prefixes every
# window after the first with this marker + the unit's header line.
_CONTINUED_PREFIX = "// … continued from "
# Matching lines extracted past the served page and kept in the pool row.
_GREP_POOL_EXTRA = 100


def _grep_trace_args(input: CodeGrepInput) -> dict[str, Any]:
    """The grep arguments that change the answer, for the trace row, so a flip between two
    same-second calls (0 -> N matches) can be pinned on the argument that differed."""
    return answer_args(
        input,
        "mode",
        "regex",
        "case_sensitive",
        "path_globs",
        "exclude_globs",
        "langs",
        "exclude_unit_ids",
        "limit",
        "deadline_ms",
        paged=bool(input.cursor) or None,
    )


def _grep_params_hash(input: CodeGrepInput) -> str:
    import hashlib

    parts = {
        "pattern": input.pattern,
        "regex": input.regex,
        "case_sensitive": input.case_sensitive,
        "path_prefix": input.path_prefix,
        "mode": input.mode,
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def _issue_grep_cursor(
    input: CodeGrepInput,
    org: str,
    ns: str,
    matches: list[dict[str, Any]],
    *,
    served: int,
    run_id: str,
    chunks_matched: int,
    truncated: bool,
) -> str:
    """Store every extracted line (served ones included, so offsets are
    pool positions) and return the cursor for the page after `served`;
    "" when nothing was extracted past it or the tier is off."""
    from server.code.app import search_cursor as paging

    if not paging.enabled() or len(matches) <= served:
        return ""
    ph = _grep_params_hash(input)
    paging.put_pool(
        org,
        ns,
        paging.pool_key("g", run_id),
        {
            "v": paging.CURSOR_VERSION,
            "kind": "g",
            "params": ph,
            "matches": matches,
            "chunks_matched": chunks_matched,
            "truncated": truncated,
        },
        indexed_commit="",
        route="grep",
    )
    return paging.encode(kind="g", run_id=run_id, offset=served, params=ph, indexed_commit="")


async def _serve_grep_page(
    input: CodeGrepInput, org: str, ns: str, repo: CodeRepo, route: str, started: float
) -> tuple[CodeGrepOutput | None, str, int]:
    from server.code.app import search_cursor as paging
    from server.core.errors import ValidationError

    ph = _grep_params_hash(input)
    try:
        fields = paging.decode(input.cursor, kind="g", params=ph)
    except paging.CursorError as e:
        raise ValidationError(f"invalid cursor: {e}") from e
    offset = int(fields["o"])
    if paging.expired(fields):
        return None, "expired", offset
    with code_span("grep.page_read", route=route):
        row = await _read_pool_settling(org, ns, paging.pool_key("g", str(fields["r"])), fields)
    if row is None:
        return None, "pool_not_found", offset
    payload = row["payload"]
    pool = [dict(m) for m in (payload.get("matches") or [])]
    limit = input.effective_limit
    excluded = set(input.exclude_unit_ids)
    page: list[dict[str, Any]] = []
    i = offset
    while i < len(pool) and len(page) < limit:
        m = pool[i]
        i += 1
        if str(m.get("path") or "") in excluded:
            continue
        page.append(m)
    has_more = i < len(pool)
    next_cursor = (
        paging.encode(
            kind="g",
            run_id=str(fields["r"]),
            offset=i,
            params=ph,
            indexed_commit="",
            ts=int(fields["t"]),
        )
        if has_more
        else ""
    )
    files = list(dict.fromkeys(m["path"] for m in page))
    trace_run_id = new_trace_run_id()
    route = route + "+page"
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.grep",
        query=input.pattern,
        route=route,
        args=_grep_trace_args(input),
        returned_ids=files,
        returned_paths=files,
        returned_scores=[],
        top_k=limit,
        tokens_returned=sum(len(str(m.get("text") or "")) for m in page) // 4,
        abstain=not page,
        retrieval_risk="low" if page else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        chunks_matched=int(payload.get("chunks_matched") or 0),
        path_prefix=input.path_prefix,
    )
    set_code_span_attributes(
        route=route,
        results=len(page),
        abstain=not page,
        page_offset=offset,
        retrieval_risk="low" if page else "abstain",
        consumer=input.consumer,
    )
    return (
        CodeGrepOutput(
            matches=page,
            next_cursor=next_cursor,
            has_more=has_more,
            chunks_matched=int(payload.get("chunks_matched") or 0),
            truncated=bool(payload.get("truncated")) or has_more,
            as_of=paging.disclose(
                await index_as_of(repo, org, ns), page_commit="", offset=offset, fallback=""
            ),
            trace_run_id=trace_run_id,
        ),
        "",
        offset,
    )


async def _grep_page_by_rerun(input: CodeGrepInput, *, offset: int, reason: str) -> CodeGrepOutput:
    """The pool row is gone: run the grep again deep enough to reach the
    offset, serve the slice past it, disclose why."""
    from server.code.app import search_cursor as paging

    limit = input.effective_limit
    fresh = input.model_copy(update={"cursor": "", "limit": min(200, offset + limit)})
    out = await handle_code_grep(fresh)
    out.matches = out.matches[offset : offset + limit]
    out.as_of = paging.disclose(out.as_of, page_commit="", offset=offset, fallback=reason)
    set_code_span_attributes(page_offset=offset, page_fallback=reason)
    return out


async def _unit_spans(
    repo: CodeRepo, org: str, ns: str, chunks: list[dict[str, Any]]
) -> dict[tuple[str, str], tuple[int, int]]:
    """(path, scope) -> (start_line, end_line) of the enclosing unit for the
    matched chunks, so a grep hit can say how big the function around it is
    without a code.enclosing round trip.

    A chunk normally IS the unit, so its own span answers. Oversized units
    are indexed as several windows, each carrying only its own span: those
    are detected (continuation prefix, or two windows of one scope in the
    result) and resolved to the symbol row's full span in one batched
    lookup — only then, so the common grep pays no extra query. Should the
    symbol lookup miss, the union of the fetched windows stands in."""
    spans: dict[tuple[str, str], tuple[int, int]] = {}
    sym_ids: dict[tuple[str, str], list[str]] = {}
    windowed: set[tuple[str, str]] = set()
    for c in chunks:
        scope = str(c.get("scope") or "")
        if not scope:
            continue
        key = (str(c["path"]), scope)
        start, end = int(c.get("start_line") or 0), int(c.get("end_line") or 0)
        prev = spans.get(key)
        if prev is None:
            spans[key] = (start, end)
        else:
            spans[key] = (min(prev[0], start) if start else prev[0], max(prev[1], end))
            windowed.add(key)
        if str(c.get("content") or "").startswith(_CONTINUED_PREFIX):
            windowed.add(key)
        sym_ids.setdefault(key, []).extend(str(s) for s in (c.get("symbol_ids") or []) if s)
    ids = list(dict.fromkeys(s for key in windowed for s in sym_ids.get(key, [])))
    if not ids:
        return spans
    # The span columns in one round trip
    by_id = {str(r.get("symbol_id")): r for r in await repo.symbol_spans_by_ids(org, ns, ids)}
    for key in windowed:
        for sid in dict.fromkeys(sym_ids.get(key, [])):
            row = by_id.get(sid)
            if row is None:
                continue
            start, end = int(row.get("start_line") or 0), int(row.get("end_line") or 0)
            # a single-line symbol row (compiler saw a declaration) is no
            # extent: the union of the fetched windows is the better answer
            if start and end > start and str(row.get("path") or key[0]) == key[0]:
                spans[key] = (start, end)
                break
    return spans


async def _grep_files(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    *,
    route: str,
    started: float,
    scope: Any = None,
    budget: code_deadline.Budget | None = None,
) -> CodeGrepOutput:
    """mode=files: which files contain the pattern, without line text. One
    GROUP BY over the chunk index instead of fetching chunk bodies, so the
    cap can be 500 files where the line mode stops at 200 lines."""
    from server.code.app import message_grep as mg

    limit = input.effective_limit
    as_of = _as_of_early(repo, org, ns)  # Beside the reads
    note = ""
    kw: dict[str, Any] = {"scope": scope} if scope is not None and scope.has_path_filters else {}
    kw.update(case_sensitive=input.case_sensitive, path_prefix=input.path_prefix, limit=limit)
    with code_span("grep.files", route=route) as sp:
        files, total = await _bounded_scan(
            budget, repo.grep_files, org, ns, input.pattern, regex=input.regex, **kw
        )
        # a regex-shaped literal that names no file: once more as a regex; a non-empty literal
        # answer is untouched
        if not files and not input.regex and mg.regex_shaped(input.pattern):
            if mg.compile_fallback(input.pattern) is None:
                note = mg.REGEX_INVALID_NOTE
            elif _second_scan_allowed(budget, "regex retry"):
                files, total = await _bounded_scan(
                    budget, repo.grep_files, org, ns, input.pattern, regex=True, **kw
                )
                route += "+regex_fallback"
                note = mg.REGEX_FALLBACK_NOTE if files else ""
        span_attrs(sp, results=len(files))
    paths = [str(f["path"]) for f in files]
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.grep",
        query=input.pattern,
        route=route + "_files",
        args=_grep_trace_args(input),
        returned_ids=paths,
        returned_paths=paths,
        returned_scores=[],
        top_k=limit,
        tokens_returned=sum(len(p) for p in paths) // 4,
        abstain=not files,
        retrieval_risk="low" if files else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        chunks_matched=sum(int(f.get("chunks") or 0) for f in files),
        path_prefix=input.path_prefix,
    )
    set_code_span_attributes(
        route=route + "_files",
        results=len(files),
        truncated=total > len(files),
        path_prefix=input.path_prefix,
        abstain=not files,
        retrieval_risk="low" if files else "abstain",
        consumer=input.consumer,
    )
    return CodeGrepOutput(
        matches=[],
        files=files,
        files_total=total,
        chunks_matched=sum(int(f.get("chunks") or 0) for f in files),
        truncated=total > len(files),
        as_of=await as_of,
        note=note,
        trace_run_id=trace_run_id,
        **_partial_fields(budget),
    )


async def _grep_message(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    *,
    route: str,
    started: float,
    scope: Any = None,
    budget: code_deadline.Budget | None = None,
) -> CodeGrepOutput:
    """mode=message: the rendered text's literal spans
    retrieve raising/logging statements; each statement's own template is
    verified against the message; the best-covered statement is where it
    was raised. `matches` carry `template` and `coverage` per statement.

    A pattern no template matches — most often a code line, not a rendered
    message — is run once more as a literal matches grep inside what the
    budget has left; the answer then carries those lines, `fallback:
    literal`, and the message read's verdict fields."""
    from server.code.app import message_grep as mg

    limit = input.effective_limit
    as_of = _as_of_early(repo, org, ns)  # Beside the reads
    parsed = mg.split_message(input.pattern)
    # the ErrorCodes read needs the message alone, so it runs beside the scan
    (chunks, admitted, note), code_name = await asyncio.gather(
        _message_scan(repo, org, ns, input, parsed, route=route, scope=scope, budget=budget),
        mg.error_code_name(repo, org, ns, None if parsed.error_name else parsed.error_code),
    )
    error_name = parsed.error_name or code_name
    if parsed.error_code is not None and not error_name:
        note = f"Code: {parsed.error_code} is not in the indexed ErrorCodes table; " + note
    cap = settings.code_search_pattern_max_files
    if admitted > cap:
        # the constrained-search pre-count cap: a message whose spans admit more files than that
        # is common prose, not a template — refused with the count, as a broad `pattern` is
        note += f"pattern_too_broad: the message's spans admit {admitted} files (cap {cap})"
        chunks = []
    ranked = mg.rank_statements(parsed, chunks, error_name=error_name)
    best = ranked[0] if ranked else None
    verdict = mg.verdict_for(best)
    verdict_fields: dict[str, Any] = {
        "verdict": verdict,
        "template": parsed.template,
        "error_code": error_name,
        "coverage": best.coverage if best is not None else 0.0,
    }
    literal = None
    if verdict != "raised_at":
        literal = await _message_literal_fallback(
            repo, org, ns, input, route=route, started=started, scope=scope, budget=budget
        )
    if literal is not None and literal.matches:
        return literal.model_copy(
            update={**verdict_fields, "note": _notes(note, _MESSAGE_FALLBACK_NOTE, literal.note)}
        )
    unit_spans = await _unit_spans(repo, org, ns, chunks) if ranked else {}
    matches = _statement_matches(ranked[:limit], unit_spans)
    if literal is not None:
        # the literal read traced this call and found no line either
        return CodeGrepOutput(
            matches=matches,
            chunks_matched=len(chunks),
            truncated=len(ranked) > limit,
            as_of=await as_of,
            note=_notes(note, "the text as a literal matched no line either"),
            trace_run_id=literal.trace_run_id,
            fallback="literal",
            **verdict_fields,
            **_partial_fields(budget),
        )
    files = list(dict.fromkeys(m["path"] for m in matches))
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.grep",
        query=input.pattern,
        route=route,
        args=_grep_trace_args(input),
        returned_ids=files,
        returned_paths=files,
        returned_scores=[float(m["coverage"]) for m in matches],
        top_k=limit,
        tokens_returned=sum(len(m["text"]) + len(m["template"]) for m in matches) // 4,
        abstain=verdict != "raised_at",
        retrieval_risk="low" if verdict == "raised_at" else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        chunks_matched=len(chunks),
        path_prefix=input.path_prefix,
        verdict=verdict,
    )
    set_code_span_attributes(
        route=route,
        results=len(matches),
        chunks_matched=len(chunks),
        verdict=verdict,
        path_prefix=input.path_prefix,
        abstain=verdict != "raised_at",
        retrieval_risk="low" if verdict == "raised_at" else "abstain",
        consumer=input.consumer,
    )
    return CodeGrepOutput(
        matches=matches,
        chunks_matched=len(chunks),
        truncated=len(ranked) > limit,
        as_of=await as_of,
        note=note.strip().rstrip(";"),
        trace_run_id=trace_run_id,
        **verdict_fields,
        **_partial_fields(budget),
    )


async def _message_scan(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    parsed: Any,
    *,
    route: str,
    scope: Any,
    budget: code_deadline.Budget | None,
) -> tuple[list[dict[str, Any]], int, str]:
    """The message's statement scan: (chunks, admitted files, note). No scan when the
    message has no literal span to look for."""
    if not parsed.needles:
        return [], 0, "no literal span of three words or twelve characters in the message"
    with code_span("grep.message", route=route) as sp:
        chunks, admitted = await _bounded_scan(
            budget,
            repo.grep_message_chunks,
            org,
            ns,
            parsed.needles,
            path_prefix=input.path_prefix,
            limit=min(200, max(60, input.effective_limit * 3)),
            **({"scope": scope} if scope is not None and scope.has_path_filters else {}),
        )
        span_attrs(sp, chunks_matched=len(chunks), admitted_files=admitted)
    return chunks, admitted, ""


def _statement_matches(
    ranked: list[Any], unit_spans: dict[tuple[str, str], tuple[int, int]]
) -> list[dict[str, Any]]:
    """The ranked statements as match rows, each with its enclosing unit's span."""
    matches: list[dict[str, Any]] = []
    for st in ranked:
        span = unit_spans.get((st.path, st.scope), (0, 0)) if st.scope else (0, 0)
        matches.append(
            {
                "path": st.path,
                "line": st.line,
                "text": st.text,
                "scope": st.scope,
                "scope_start_line": span[0],
                "scope_end_line": span[1],
                "template": st.template[:300],
                "coverage": st.coverage,
                "names_error_code": st.names_code,
            }
        )
    return matches


def _notes(*parts: str) -> str:
    """Non-empty notes joined with '; ', each stripped of a trailing ';'."""
    return "; ".join(p for p in (str(x).strip().rstrip(";").strip() for x in parts) if p)


_MESSAGE_FALLBACK_NOTE = (
    "no throw / LOG_ statement template matches this text, so it was grepped as a literal: "
    "mode 'message' is for rendered error or log text; send a code line with mode 'matches' "
    "(next_cursor pages on under that mode)"
)


async def _message_literal_fallback(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    *,
    route: str,
    started: float,
    scope: Any,
    budget: code_deadline.Budget | None,
) -> CodeGrepOutput | None:
    """The message pattern as a plain literal matches grep, marked `fallback: literal`; None
    when the budget has nothing left for a second scan."""
    if not _second_scan_allowed(budget, "literal fallback"):
        return None
    out = await _grep_lines(
        repo,
        org,
        ns,
        input.model_copy(update={"mode": "matches", "regex": False, "neighbours": None}),
        route=route + "+literal_fallback",
        started=started,
        scope=scope,
        line_re=None,
        budget=budget,
    )
    out.fallback = "literal"
    return out
