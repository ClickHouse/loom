"""code.grep: literal and regex lines over the live chunks (with neighbours and a
cursor), the file-name mode and the commit-message mode."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from server.code.api.models_grep import (
    RELATED_GREP_CONTEXT_LINES,
    CodeGrepInput,
    CodeGrepOutput,
    RelatedGrepEvidence,
)
from server.code.app.common import (
    answer_args,
    index_as_of,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.grep_bodies import definitions_for, definitions_tokens
from server.code.app.grep_dependencies import add_dependencies, forwarded_dependency
from server.code.app.grep_source import (
    GREP_MATCH_TEXT_CHARS,
    CompleteContexts,
    ContextSource,
    collect_contexts,
    match_revisions,
    page_contexts,
    recover_page_context,
    recover_source_chunks,
    verify_complete_contexts,
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
from server.code.domain.index.grep_plan import ascii_lower
from server.code.domain.search_scope import SearchScope
from server.code.domain.symbol_body import retained_source_lines
from server.code.store.index import CodeRepo
from server.core.caller_regex import compile_caller_regex
from server.core.ch import caller_shaped, class_timeout_s
from server.core.errors import ValidationError
from server.settings import settings

logger = logging.getLogger(__name__)


async def handle_code_grep(input: CodeGrepInput, *, exact_path: str = "") -> CodeGrepOutput:
    """Exact literal / RE2 regex over indexed chunks, per-line results with the enclosing
    symbol attached. Matching lines are extracted app-side from the chunk content (line =
    chunk.start_line + offset).

    mode=files answers "which files"; mode=message takes a RENDERED message and finds the
    statement whose template printed it. A literal pattern that is regex-shaped and matches
    nothing is run once as a regex and says so in `note`.

    The caller's pattern shapes every scan, so they all run under the caller-shaped
    ClickHouse ceilings."""
    with caller_shaped():
        return await _code_grep(input, exact_path=exact_path)


async def _code_grep(
    input: CodeGrepInput,
    *,
    started: float | None = None,
    offset: int = 0,
    page_limit: int | None = None,
    exact_path: str = "",
) -> CodeGrepOutput:
    from server.observability.budget_shape import note_budget_shape

    require_code_intel()
    pattern = input.pattern
    if isinstance(pattern, list) and (input.regex or input.mode == "message"):
        raise ValidationError("regex=true and message mode each require one pattern string")
    if input.include_body and input.mode != "matches":
        raise ValidationError("include_body on code.grep requires mode: 'matches'")
    # Presence on several namespaces: which of them contain the pattern, one row each. Unset:
    # matches / files on the one namespace.
    if input.mode == "presence":
        from server.code.app import presence as code_presence

        return await code_presence.grep_presence(input, handle_code_grep)
    # Files and message are priced by their own p99 budgets
    # (Operation.shape_budgets_p99_ms); matches is the base budget.
    note_budget_shape("" if input.mode == "matches" else input.mode)
    started = time.monotonic() if started is None else started
    start_stage_wall()
    org, ns = resolve_code_identity(input.org, input.namespace)
    route = "grep_regex" if input.regex else "grep_literal"
    if isinstance(pattern, list):
        route = "grep_literals"
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

    # RE2, as ClickHouse's match() reads it, and case-folded the way the SQL step's `(?i)` is
    line_re = (
        compile_caller_regex(pattern, case_sensitive=input.case_sensitive)
        if input.regex and isinstance(pattern, str)
        else None
    )

    # rg -g / -t style file filters; unset = no SQL added
    scope = _scope_of(input)
    if exact_path:
        scope = replace(scope, exact_path=exact_path)
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
        return await _grep_page_by_rerun(input, offset=offset, reason=fallback, started=started)
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
        offset=offset,
        page_limit=page_limit,
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
    pattern = input.pattern
    if chunks or input.regex or not isinstance(pattern, str) or not mg.regex_shaped(pattern):
        return chunks, line_re, "", ""
    fallback = mg.compile_fallback(pattern, case_sensitive=input.case_sensitive)
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
    # The statement's own SETTINGS win over the URL ceiling, so clamp here. With no
    # caller-shaped ceiling, the read class timeout (the reader profile's cap) holds.
    ceiling_s = settings.ch_caller_shaped_max_execution_s or class_timeout_s("read")
    if ceiling_s > 0:
        limit_s = min(limit_s, ceiling_s)
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


@dataclass
class _GrepLineScan:
    chunks: list[dict[str, Any]]
    matches: list[dict[str, Any]]
    match_commits: dict[tuple[str, int], str]
    chunks_matched: int
    truncated: bool
    route: str
    note: str
    related_pattern: str
    batch_partial_reason: str = ""


def _terminated_lines(row: dict[str, Any]) -> int:
    # The file row counts its LF bytes plus one. Each lower coordinate has
    # a physical terminator, including when the chunker split a long line.
    return max(0, int(row.get("source_loc") or 0) - int(row["start_line"]))


def _missing_chunk_patterns(
    patterns: list[str],
    chunks: list[dict[str, Any]],
    *,
    case_sensitive: bool,
    excluded: set[str],
    deadline: float | None,
) -> list[str]:
    fold = str if case_sensitive else ascii_lower
    remaining = list(
        dict.fromkeys(fold(pattern) for pattern in patterns if "\n" not in pattern[:-1])
    )
    needs_endings = any(literal.endswith("\n") for literal in remaining)
    scanned = 0
    for chunk in chunks:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if str(chunk["path"]) in excluded:
            continue
        scanned += len(str(chunk["content"]))
        if scanned > _LINE_SCAN_MAX_CHARS:
            break
        if lines := retained_source_lines(chunk):
            source = fold("\n".join(line.removesuffix("\n").removesuffix("\r") for line in lines))
            raw = fold("".join(lines[: _terminated_lines(chunk)])) if needs_endings else ""
            remaining = [
                literal
                for literal in remaining
                if literal not in (raw if literal.endswith("\n") else source)
            ]
        if not remaining:
            break
    return remaining


async def _cover_batch_chunks(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    chunks: list[dict[str, Any]],
    scope: SearchScope,
    budget: code_deadline.Budget | None,
    page_limit: int,
) -> tuple[list[dict[str, Any]], str]:
    patterns = input.pattern
    if not isinstance(patterns, list) or len(patterns) < 2:
        return chunks, ""
    if not _second_scan_allowed(budget, "batch pattern scan"):
        return chunks, ""
    missing = await asyncio.to_thread(
        _missing_chunk_patterns,
        patterns,
        chunks,
        case_sensitive=input.case_sensitive,
        excluded=set(input.exclude_unit_ids),
        deadline=budget.until if budget is not None else None,
    )
    if not missing or not _second_scan_allowed(budget, "batch pattern scan"):
        return chunks, ""
    try:
        async with asyncio.timeout(None if budget is None else budget.left_ms() / 1000):
            witnesses: list[dict[str, Any]] = await _bounded_scan(
                budget,
                repo.grep_pattern_chunks,
                org,
                ns,
                missing,
                case_sensitive=input.case_sensitive,
                path_prefix=input.path_prefix,
                scope=scope,
                exclude_paths=input.exclude_unit_ids,
                limit=page_limit,
            )
    except TimeoutError:
        return chunks, "batch pattern scan timed out"
    except Exception as error:  # noqa: BLE001 — preserve exact hits on optional read failure
        logger.debug("batch pattern scan failed: %s", type(error).__name__)
        return chunks, "batch pattern scan failed"
    return sorted(chunks + witnesses, key=lambda row: (row["path"], row["start_line"])), ""


async def _scan_grep_lines(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    *,
    route: str,
    scope: Any,
    line_re: Any,
    budget: code_deadline.Budget | None,
    first_page: bool,
    limit: int,
    page_limit: int,
) -> _GrepLineScan:
    scan_limit = min(200, limit * 3)
    prefer_patterns = (
        input.prioritize_patterns and isinstance(input.pattern, list) and len(input.pattern) > 1
    )
    batch_partial_reason = ""
    with code_span("grep.chunks", route=route) as sp:
        # over-fetch chunks: several lines can match inside one chunk, and
        # several chunks can be windows of one oversized unit
        chunks, line_re, suffix, note = await _fetch_grep_chunks(
            repo,
            org,
            ns,
            input,
            scope=scope,
            limit=scan_limit,
            line_re=line_re,
            budget=budget,
        )
        route += suffix
        scan_input = input
        related_pattern = _related_grep_pattern(input, note) if not chunks and first_page else ""
        if related_pattern and _second_scan_allowed(budget, "related source scan"):
            scan_input = input.model_copy(update={"pattern": related_pattern})
            chunks, _, _, _ = await _fetch_grep_chunks(
                repo,
                org,
                ns,
                scan_input,
                scope=scope,
                limit=scan_limit,
                line_re=None,
                budget=budget,
            )
            line_re = _qualified_argument_line_re(related_pattern, input.case_sensitive)
            route += "+related_member" if related_pattern.startswith(".") else "+related_argument"
        if prefer_patterns and len(chunks) >= scan_limit:
            with code_span("grep.batch_coverage", route=route):
                chunks, batch_partial_reason = await _cover_batch_chunks(
                    repo, org, ns, input, chunks, scope, budget, page_limit
                )
        span_attrs(sp, chunks_matched=len(chunks))

    matched_chunks = len(chunks)
    chunks, matches, match_commits, scan_capped = await _read_line_matches(
        repo,
        org,
        ns,
        chunks,
        scan_input,
        line_re,
        limit + _GREP_POOL_EXTRA,
        budget,
        priority_limit=page_limit if prefer_patterns else 0,
    )

    return _GrepLineScan(
        chunks=chunks,
        matches=matches,
        match_commits=match_commits,
        chunks_matched=matched_chunks,
        truncated=scan_capped or len(matches) > limit or matched_chunks >= scan_limit,
        route=route,
        note=note,
        related_pattern=related_pattern,
        batch_partial_reason=batch_partial_reason,
    )


async def _grep_page_context(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    scan: _GrepLineScan,
    matches: list[dict[str, Any]],
    budget: code_deadline.Budget | None,
) -> tuple[dict[str, Any], ContextSource, int, CompleteContexts]:
    context_lines = input.context_lines
    if (
        scan.related_pattern
        and matches
        and not input.include_body
        and "context_lines" not in input.model_fields_set
    ):
        context_lines = RELATED_GREP_CONTEXT_LINES
    if not context_lines:
        return {}, {}, 0, frozenset()
    complete_contexts: CompleteContexts = frozenset()
    if input.prefer_complete_context and not input.include_body:
        with code_span("grep.declaration_extents") as span:
            complete_contexts = await verify_complete_contexts(
                repo, org, ns, scan.chunks, scan.matches, scan.match_commits, context_lines, budget
            )
            span_attrs(span, verified=len(complete_contexts))
    source = await asyncio.to_thread(
        collect_contexts,
        scan.chunks,
        scan.matches,
        context_lines,
        _LINE_SCAN_MAX_CHARS,
        match_commits=scan.match_commits,
        complete_contexts=complete_contexts,
    )
    if input.context_lines:
        await recover_page_context(
            repo,
            org,
            ns,
            source,
            matches,
            context_lines,
            match_revisions(scan.matches, scan.match_commits),
            complete_contexts=complete_contexts,
            max_source_bytes=_LINE_SCAN_MAX_CHARS,
            budget=budget,
        )
    page = page_contexts(source, matches, context_lines, complete_contexts=complete_contexts)
    return page, source, context_lines, complete_contexts


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
    offset: int = 0,
    page_limit: int | None = None,
) -> CodeGrepOutput:
    """mode=matches: matching lines with the enclosing unit's span."""
    limit = input.effective_limit
    served_limit = page_limit or limit
    as_of = _as_of_early(repo, org, ns)  # Beside the reads
    scan = await _scan_grep_lines(
        repo,
        org,
        ns,
        input,
        route=route,
        scope=scope,
        line_re=line_re,
        budget=budget,
        first_page=not offset,
        limit=limit,
        page_limit=served_limit,
    )
    route, related_pattern, note = scan.route, scan.related_pattern, scan.note
    trace_run_id = new_trace_run_id()
    matches = scan.matches[offset : offset + served_limit]
    # Keep the scan's coverage for the cursor and record enrichment cuts for this page.
    page_budget = replace(budget, cut=[]) if budget is not None else None
    context, context_source, context_lines, complete_contexts = await _grep_page_context(
        repo, org, ns, input, scan, matches, page_budget
    )
    bodies = await definitions_for(input, matches, page_budget, match_commits=scan.match_commits)
    neighbours, anchors_capped, neighbour_tokens, route = await _grep_neighbours(
        repo, org, ns, input, matches, route, budget=page_budget
    )
    exact_coverage = await asyncio.to_thread(
        _line_coverage_fields, scan.chunks, budget, additional_reason=scan.batch_partial_reason
    )
    coverage = dict(exact_coverage)
    if page_budget is not None and page_budget.partial:
        coverage.update(
            partial=True,
            partial_reason=_notes(coverage.get("partial_reason", ""), page_budget.reason()),
        )
    extra_related = None
    if not (related_pattern or offset or coverage.get("partial")):
        extra_related, extra_coverage = await _extra_related_source(
            repo,
            org,
            ns,
            input,
            scope=scope,
            matches=matches,
            scan=scan,
            exact_definitions=bodies.get("definitions", []),
            limit=served_limit,
            budget=page_budget,
        )
        coverage.update(extra_coverage)
        if extra_related is not None:
            route += (
                "+related_batch_argument"
                if isinstance(input.pattern, list)
                else "+related_call_prefix"
            )
    related, note = _label_related(
        related_pattern, matches, bodies, context, scan, extra_related, note
    )
    related, missing = await _related_dependencies(
        repo,
        org,
        ns,
        input,
        scope,
        scan,
        matches,
        related,
        budget=page_budget,
        incomplete=bool(coverage.get("partial")),
        whole_page=not offset and len(matches) == len(scan.matches),
        definitions=bodies.get("definitions", []) if not offset else [],
    )
    if missing:
        coverage.update(
            partial=True, partial_reason=_notes(coverage.get("partial_reason", ""), missing)
        )
    next_cursor = ""
    if not related_pattern:
        next_cursor = _issue_grep_cursor(
            input,
            org,
            ns,
            scan.matches,
            served=offset + served_limit,
            run_id=trace_run_id,
            chunks_matched=scan.chunks_matched,
            truncated=scan.truncated,
            partial=bool(exact_coverage.get("partial")),
            partial_reason=str(exact_coverage.get("partial_reason") or ""),
            context_source=context_source,
            complete_contexts=complete_contexts,
            match_commits=scan.match_commits,
        )
    # one entry per matched FILE (ids and paths alike — grep has no unit ids,
    # so the outcome fold skips this op; the paths still feed opened_file)
    returned = matches + (extra_related.matches if extra_related is not None else [])
    dependencies = related.dependencies if related else []
    files = list(dict.fromkeys([*(m["path"] for m in returned), *(d.path for d in dependencies)]))
    risk = "low" if returned else "abstain"
    if (matches and related_pattern) or (extra_related and extra_related.matches):
        risk = "medium"
    related_chars = _extra_related_chars(extra_related) + sum(len(d.body) for d in dependencies)
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.grep",
        query=_grep_trace_query(input),
        route=route,
        args=_grep_trace_args(input, context_lines=context_lines),
        returned_ids=files,
        returned_paths=files,
        returned_scores=[],
        top_k=served_limit,
        tokens_returned=sum(len(m["text"]) for m in returned) // 4
        + _context_tokens(context)
        + related_chars // 4
        + neighbour_tokens
        + definitions_tokens(bodies),
        abstain=not returned,
        retrieval_risk=risk,
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        chunks_matched=scan.chunks_matched,
        path_prefix=input.path_prefix,
    )
    set_code_span_attributes(
        route=route,
        results=len(returned),
        chunks_matched=scan.chunks_matched,
        path_prefix=input.path_prefix,
        truncated=scan.truncated,
        abstain=not returned,
        retrieval_risk=risk,
        consumer=input.consumer,
    )
    if (
        not matches
        and not note
        and not scan.truncated
        and not coverage.get("partial")
        and input.mode == "matches"
        and offset == 0
        and not input.regex
    ):
        note = (
            "No exact match in the indexed scope. Try shorter known fragments together in "
            "a pattern list, code.symbol for a known name, or code.search for behavior."
        )
    output = CodeGrepOutput(
        matches=[] if related_pattern else matches,
        related=related,
        next_cursor=next_cursor,
        has_more=bool(next_cursor),
        chunks_matched=0 if related_pattern else scan.chunks_matched,
        truncated=scan.truncated and not related_pattern,
        as_of=await as_of,
        note=note,
        trace_run_id=trace_run_id,
        neighbours=neighbours,
        anchors=len(neighbours),
        anchors_capped=anchors_capped,
        neighbour_tokens=neighbour_tokens,
        **({**bodies, **context} if not related_pattern else {}),
        **coverage,
    )
    output._match_commits = scan.match_commits
    return output


def _label_related(
    pattern: str,
    matches: list[dict[str, Any]],
    bodies: dict[str, Any],
    context: dict[str, Any],
    scan: _GrepLineScan,
    extra: RelatedGrepEvidence | None,
    note: str,
) -> tuple[RelatedGrepEvidence | None, str]:
    if pattern and matches:
        related = RelatedGrepEvidence(
            pattern=pattern, matches=matches, truncated=scan.truncated, **bodies, **context
        )
        if pattern.startswith("."):
            return related, _notes(note, "Related source matches the member name across receivers.")
        return (
            related,
            "No exact match in the indexed scope. Related source matches the call's argument.",
        )
    if extra and extra.matches:
        note = "Related source shows where the call's argument is used."
        if not matches and not scan.truncated:
            note = "No exact match in the indexed scope. " + note
    return extra, note


def _extra_related_chars(extra: RelatedGrepEvidence | None) -> int:
    if extra is None:
        return 0
    return sum(len(row["text"]) for row in extra.contexts) + sum(
        len(row.get("body") or "") for row in extra.definitions
    )


_IDENTIFIER = r"[^\W\d]\w*"
_CALL_PREFIX = (
    rf"{_IDENTIFIER}(?:(?:\.|::){_IDENTIFIER})*\([ \t]*"
    rf"({_IDENTIFIER}(?:\.{_IDENTIFIER})+)[ \t]*"
)
_QUALIFIED_ARGUMENT = re.compile(_CALL_PREFIX + r"\)")
_QUALIFIED_ARGUMENT_PREFIX = re.compile(_CALL_PREFIX)
_QUALIFIED_MEMBER = re.compile(rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})+")
_PLAIN_CALL_ALTERNATIVE = re.compile(r"[\w.:(), \t]+")


def _batch_dependency_argument(input: CodeGrepInput) -> str:
    if input.regex or not isinstance(input.pattern, list):
        return ""
    arguments = set()
    for raw in input.pattern:
        pattern = raw.strip()
        if input.related_on_miss and (call := _QUALIFIED_ARGUMENT.fullmatch(pattern)):
            arguments.add(call[1])
        if input.related_on_prefix and (call := _QUALIFIED_ARGUMENT_PREFIX.fullmatch(pattern)):
            arguments.add(call[1])
    return next(iter(arguments)) if len(arguments) == 1 else ""


def _missing_batch_argument(input: CodeGrepInput, matches: list[dict[str, Any]]) -> str:
    if not input.related_on_miss or not isinstance(input.pattern, list):
        return ""
    calls = {raw: argument for raw in input.pattern if (argument := qualified_call_argument(raw))}
    if len(calls) != 1:
        return ""
    ((literal, argument),) = calls.items()
    if literal != literal.strip():
        return ""
    fold = str if input.case_sensitive else ascii_lower
    if fold(argument) in {fold(text) for text in input.pattern}:
        return ""  # the exact scan already searched for every occurrence of the argument
    literal = fold(literal)
    for match in matches:
        text = match["text"]
        if len(text) >= GREP_MATCH_TEXT_CHARS or literal in fold(text):
            return ""
    return argument


def _batch_prefix_argument(input: CodeGrepInput, patterns: list[str], *, truncated: bool) -> str:
    if not input.related_on_prefix or not any(
        _QUALIFIED_ARGUMENT_PREFIX.fullmatch(pattern.strip()) for pattern in patterns
    ):
        return ""
    argument = _batch_dependency_argument(input)
    fold = str if input.case_sensitive else ascii_lower
    if not truncated and fold(argument) in {fold(pattern) for pattern in patterns}:
        return ""
    return argument


async def _related_dependencies(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    scope: Any,
    scan: _GrepLineScan,
    matches: list[dict[str, Any]],
    related: RelatedGrepEvidence | None,
    *,
    budget: code_deadline.Budget | None,
    incomplete: bool,
    whole_page: bool,
    definitions: list[dict[str, Any]],
) -> tuple[RelatedGrepEvidence | None, str]:
    if incomplete or not input.include_dependencies:
        return related, ""
    if related is not None:
        if related.pattern.startswith("."):
            return related, ""  # a shared member name does not establish a binding
        evidence = (
            related.model_copy(update={"matches": [*matches, *related.matches]})
            if isinstance(input.pattern, list)
            else related
        )
        reason = await add_dependencies(
            repo, org, ns, input, evidence, scope, scan.match_commits, budget
        )
        related.dependencies = evidence.dependencies
        return related, reason
    argument = (
        _batch_dependency_argument(input) if matches and whole_page and not scan.truncated else ""
    )
    if not argument:
        return await forwarded_dependency(
            repo, org, ns, input, scope, definitions, matches, scan.match_commits, budget
        )
    evidence = RelatedGrepEvidence(pattern=argument, matches=matches)
    reason = await add_dependencies(
        repo, org, ns, input, evidence, scope, scan.match_commits, budget
    )
    if not evidence.dependencies:
        return None, reason
    # The exact batch results already carry these witnesses.
    evidence.matches = []
    return evidence, reason


def _related_grep_pattern(input: CodeGrepInput, note: str) -> str:
    from server.code.app.message_grep import REGEX_INVALID_NOTE

    if not input.related_on_miss or input.regex or not isinstance(input.pattern, str):
        return ""
    if note == REGEX_INVALID_NOTE:
        return _malformed_alternative_member(input.pattern)
    return qualified_call_argument(input.pattern)


def _malformed_alternative_member(pattern: str) -> str:
    terms = [term.strip() for term in pattern.split("|")]
    if len(terms) < 2 or any(not _PLAIN_CALL_ALTERNATIVE.fullmatch(term) for term in terms):
        return ""
    if not any(
        _QUALIFIED_ARGUMENT.fullmatch(term) or _QUALIFIED_ARGUMENT_PREFIX.fullmatch(term)
        for term in terms
    ):
        return ""
    # A later argument or a bare alternative can make the member ambiguous.
    members = {
        match[0].rsplit(".", 1)[-1]
        for term in terms
        for match in _QUALIFIED_MEMBER.finditer(term.split("(", 1)[-1])
    }
    if len(members) != 1:
        return ""
    return "." + members.pop()


def qualified_call_argument(pattern: str) -> str:
    call = _QUALIFIED_ARGUMENT.fullmatch(pattern.strip())
    return call[1] if call else ""


def _qualified_argument_line_re(argument: str, case_sensitive: bool) -> re.Pattern[str]:
    flags = 0 if case_sensitive else re.IGNORECASE
    if argument.startswith("."):
        receiver = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*"
        return re.compile(rf"(?<![\w.]){receiver}{re.escape(argument)}(?!\w)", flags)
    # A longer name or a different receiver is a different argument.
    return re.compile(rf"(?<![\w.]){re.escape(argument)}(?!\w|\.\w)", flags)


async def _read_line_matches(
    repo: CodeRepo,
    org: str,
    ns: str,
    chunks: list[dict[str, Any]],
    input: CodeGrepInput,
    line_re: Any,
    keep: int,
    budget: code_deadline.Budget | None,
    *,
    priority_limit: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, int], str], bool]:
    chunks = await recover_source_chunks(
        repo, org, ns, chunks, max_chars=_LINE_SCAN_MAX_CHARS, budget=budget
    )
    spans = await _unit_spans(repo, org, ns, chunks)
    # Scanning the chunk pool must not stall other requests on this worker.
    commits: dict[tuple[str, int], str] = {}
    priority_until = budget.until if priority_limit and budget is not None else None
    matches, capped = await asyncio.to_thread(
        _matching_lines,
        chunks,
        spans,
        input,
        line_re,
        keep,
        match_commits=commits,
        priority_limit=priority_limit,
        priority_until=priority_until,
    )
    if priority_until is not None and time.monotonic() >= priority_until:
        assert budget is not None
        budget.drop("batch pattern prioritization stopped")
    return chunks, matches, commits, capped


def _shares_grep_scope(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Two scans can derive different windows for the same retained definition."""
    return bool(
        left["scope"]
        and left["scope"] == right["scope"]
        and left["scope_start_line"]
        and right["scope_start_line"]
        and left["scope_start_line"] <= right["scope_end_line"]
        and right["scope_start_line"] <= left["scope_end_line"]
    )


async def _extra_related_source(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    *,
    scope: Any,
    matches: list[dict[str, Any]],
    scan: _GrepLineScan,
    exact_definitions: list[dict[str, Any]],
    limit: int,
    budget: code_deadline.Budget | None,
) -> tuple[RelatedGrepEvidence | None, dict[str, Any]]:
    if input.regex:
        return None, {}
    batch = isinstance(input.pattern, list)
    if isinstance(input.pattern, list):
        argument = _batch_prefix_argument(input, input.pattern, truncated=scan.truncated)
        if not argument and not scan.truncated:
            argument = _missing_batch_argument(input, scan.matches)
        stage = "related argument scan"
    else:
        call = _QUALIFIED_ARGUMENT_PREFIX.fullmatch(input.pattern.strip())
        argument = call[1] if input.related_on_prefix and not scan.truncated and call else ""
        stage = "related call prefix scan"
    if not argument or not _second_scan_allowed(budget, stage):
        return None, _partial_fields(budget)
    try:
        async with asyncio.timeout(None if budget is None else budget.left_ms() / 1000):
            evidence, coverage = await _read_related_source(
                repo, org, ns, input, argument, scope, matches, scan, limit, budget
            )
    except TimeoutError:
        return None, {"partial": True, "partial_reason": f"{stage} timed out"}
    except Exception as error:  # noqa: BLE001 — preserve exact evidence on optional read failure
        logger.debug("%s failed: %s", stage, type(error).__name__)
        return None, {"partial": True, "partial_reason": f"{stage} failed"}
    if batch and evidence is not None:
        bodies = await definitions_for(
            input,
            evidence.matches,
            budget,
            match_commits=scan.match_commits,
            covered_by=exact_definitions,
        )
        evidence = evidence.model_copy(update=bodies)
    return evidence, coverage


async def _read_related_source(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeGrepInput,
    argument: str,
    scope: Any,
    matches: list[dict[str, Any]],
    scan: _GrepLineScan,
    limit: int,
    budget: code_deadline.Budget | None,
) -> tuple[RelatedGrepEvidence | None, dict[str, Any]]:
    scan_limit = min(200, limit * 3)
    kw = {"scope": scope} if scope.has_path_filters else {}
    chunks = await _bounded_scan(
        budget,
        repo.grep_chunks,
        org,
        ns,
        argument,
        regex=False,
        case_sensitive=input.case_sensitive,
        path_prefix=input.path_prefix,
        limit=scan_limit,
        **kw,
    )
    scanned = len(chunks)
    line_re = _qualified_argument_line_re(argument, input.case_sensitive)
    chunks, candidates, commits, capped = await _read_line_matches(
        repo, org, ns, chunks, input, line_re, limit + _GREP_POOL_EXTRA, budget
    )
    exact_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        exact_by_path[match["path"]].append(match)
    exact_lines = {(match["path"], match["line"]) for match in matches}
    exact_paths = {match["path"] for match in scan.matches}
    exact_revisions = match_revisions(scan.matches, scan.match_commits)
    related_revisions = match_revisions(candidates, commits)
    prefix = isinstance(input.pattern, str)
    other_sites = [
        match
        for match in candidates
        if (match["path"], match["line"]) not in exact_lines
        and not (
            prefix
            and any(_shares_grep_scope(match, hit) for hit in exact_by_path.get(match["path"], ()))
        )
    ]
    related = [
        match
        for match in other_sites
        if match["path"] in related_revisions
        and (
            match["path"] not in exact_paths
            or exact_revisions.get(match["path"]) == related_revisions[match["path"]]
        )
    ]
    coverage = await asyncio.to_thread(_line_coverage_fields, chunks, budget)
    if len(related) != len(other_sites):
        coverage.update(
            partial=True,
            partial_reason=_notes(
                str(coverage.get("partial_reason", "")),
                "related source omitted where its revision could not be verified",
            ),
        )
    truncated = (
        capped or len(related) > limit or scanned >= scan_limit or bool(coverage.get("partial"))
    )
    if not related and not truncated:
        return None, coverage
    page = related[:limit]
    radius = input.context_lines
    if "context_lines" not in input.model_fields_set and (
        not isinstance(input.pattern, list) or not input.include_body
    ):
        radius = RELATED_GREP_CONTEXT_LINES
    context = {}
    if radius and page:
        source = await asyncio.to_thread(
            collect_contexts, chunks, page, radius, _LINE_SCAN_MAX_CHARS, match_commits=commits
        )
        context = page_contexts(source, page, radius)
    evidence = RelatedGrepEvidence(pattern=argument, matches=page, truncated=truncated, **context)
    scan.match_commits.update(
        {key: commits[key] for match in page if (key := (match["path"], match["line"])) in commits}
    )
    return evidence, coverage


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
        # A failed graph read leaves the grep hits intact.
        logger.warning("code.grep: neighbours block failed for %r", input.pattern, exc_info=True)
        return [], False, 0, route
    return entries, capped, tokens, route + "+neighbours"


# server/code/domain/extract/chunker.py::_split_oversized prefixes every
# window after the first with this marker + the unit's header line.
_CONTINUED_PREFIXES = ("// … continued from ", "# … continued from ", "/* … continued from ")

# Most chunk text the line step scans per call: 200 chunks of at most
# LOOM_CODE_CHUNK_MAX_CHARS (6000) is 1.2 MB, so 2 MB leaves room for older, larger
# chunks and still bounds the work one request can ask of a worker.
_LINE_SCAN_MAX_CHARS = 2_000_000
_GREP_LINE_MAPPING_VERSION = 2


def _promote_pattern_hits(
    matches: list[dict[str, Any]], first_hits: dict[str, int], limit: int
) -> list[dict[str, Any]]:
    if not first_hits:
        return matches
    order = dict.fromkeys(index for index in first_hits.values() if index >= 0)
    promoted = set(list(order)[:limit])
    if all(index < limit for index in promoted):
        return matches
    head = [row for index, row in enumerate(matches) if index in promoted]
    tail = [row for index, row in enumerate(matches) if index not in promoted]
    fill = limit - len(head)
    first_page = sorted(head + tail[:fill], key=lambda row: (row["path"], row["line"]))
    return first_page + tail[fill:]


def _matching_source_lines(
    lines: list[str],
    pattern: str | list[str],
    line_re: Any,
    fold_case: Callable[[str], str],
    *,
    terminated_lines: int,
) -> Iterator[tuple[int, str, str, str]]:
    literals = [pattern] if isinstance(pattern, str) else pattern
    endings = (
        [literal for literal in literals if literal.endswith("\n") and "\n" not in literal[:-1]]
        if terminated_lines and line_re is None
        else []
    )
    plain = [literal for literal in literals if "\n" not in literal] if endings else pattern
    for offset, raw in enumerate(lines):
        line = raw.removesuffix("\n").removesuffix("\r")
        hay = fold_case(line)
        with_ending = fold_case(raw) if endings and offset < terminated_lines else ""
        if line_re is not None:
            hit = line_re.search(line)
        else:
            hit = plain in hay if isinstance(plain, str) else any(text in hay for text in plain)
        if hit or (endings and any(text in with_ending for text in endings)):
            yield offset, line, hay, with_ending


def _matching_lines(
    chunks: list[dict[str, Any]],
    spans: dict[tuple[str, str], tuple[int, int]],
    input: CodeGrepInput,
    line_re: Any,
    keep: int,
    *,
    match_commits: dict[tuple[str, int], str] | None = None,
    priority_limit: int = 0,
    priority_until: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """(matching lines, capped): the lines of `chunks` the pattern matches, stopping past
    `keep` lines plus first hits for requested literals, or `_LINE_SCAN_MAX_CHARS` of text
    (capped=True). Pure; runs in a thread."""
    pattern = input.pattern
    # ClickHouse's case-insensitive multiSearch folds ASCII only.
    fold_case: Callable[[str], str] = str
    if not input.case_sensitive:
        fold_case = ascii_lower if isinstance(pattern, list) else str.lower
        pattern = (
            fold_case(pattern)
            if isinstance(pattern, str)
            else [fold_case(text) for text in pattern]
        )
    first_hits = (
        dict.fromkeys((literal for literal in pattern if "\n" not in literal[:-1]), -1)
        if priority_limit and line_re is None and isinstance(pattern, list) and len(pattern) > 1
        else {}
    )
    pending = set(first_hits)
    excluded_paths = set(input.exclude_unit_ids)
    matches: list[dict[str, Any]] = []
    seen: dict[tuple[str, int], str] = {}
    scanned = 0
    for c in chunks:
        if pending and priority_until is not None and time.monotonic() >= priority_until:
            pending.clear()
        if len(matches) > keep and not pending:
            return _promote_pattern_hits(matches, first_hits, priority_limit), False
        if str(c["path"]) in excluded_paths:
            continue
        content = str(c["content"])
        scanned += len(content)
        if scanned > _LINE_SCAN_MAX_CHARS:
            return _promote_pattern_hits(matches, first_hits, priority_limit), True
        lines = retained_source_lines(c)
        if lines is None:
            continue
        scope = str(c["scope"] or "")
        span = spans.get((str(c["path"]), scope), (0, 0)) if scope else (0, 0)
        pending_here = set()
        if pending:
            source = fold_case(content)
            pending_here = {literal for literal in pending if literal in source}
        if len(matches) > keep and not pending_here:
            continue
        for off, line, hay, with_ending in _matching_source_lines(
            lines, pattern, line_re, fold_case, terminated_lines=_terminated_lines(c)
        ):
            key = (str(c["path"]), int(c["start_line"] or 1) + off)
            if match_commits is not None:
                commit = str(c.get("indexed_commit") or "").rstrip("\0")
                if match_commits.setdefault(key, commit) != commit or seen.get(key, line) != line:
                    match_commits[key] = ""
            if key in seen:
                continue  # the same span indexed twice (re-parse race)
            new_patterns = {
                literal
                for literal in pending_here
                if literal in (with_ending if literal.endswith("\n") else hay)
            }
            if len(matches) > keep and not new_patterns:
                if not pending:
                    return _promote_pattern_hits(matches, first_hits, priority_limit), False
                continue
            seen[key] = line
            for literal in new_patterns:
                first_hits[literal] = len(matches)
            pending_here.difference_update(new_patterns)
            pending.difference_update(new_patterns)
            matches.append(
                {
                    "path": key[0],
                    "line": key[1],
                    "text": line.strip()[:GREP_MATCH_TEXT_CHARS],
                    "scope": scope,
                    "scope_start_line": span[0],
                    "scope_end_line": span[1],
                }
            )
            if len(matches) > keep and not pending:
                return _promote_pattern_hits(matches, first_hits, priority_limit), False
    return _promote_pattern_hits(matches, first_hits, priority_limit), False


# Matching lines extracted past the served page and kept in the pool row.
_GREP_POOL_EXTRA = 100


def _grep_trace_query(input: CodeGrepInput) -> str:
    return (
        input.pattern
        if isinstance(input.pattern, str)
        else json.dumps(input.pattern, ensure_ascii=False)
    )


def _context_tokens(context: dict[str, Any]) -> int:
    return sum(len(row["text"]) for row in context.get("contexts", ())) // 4


def _grep_trace_args(input: CodeGrepInput, *, context_lines: int | None = None) -> dict[str, Any]:
    """The grep arguments that change the answer, for the trace row, so a flip between two
    same-second calls (0 -> N matches) can be pinned on the argument that differed."""
    return answer_args(
        input,
        "mode",
        "regex",
        "prioritize_patterns",
        "case_sensitive",
        "path_globs",
        "exclude_globs",
        "langs",
        "exclude_unit_ids",
        "limit",
        "deadline_ms",
        "include_body",
        "related_on_miss",
        "related_on_prefix",
        "include_dependencies",
        "context_lines",
        "prefer_complete_context",
        context_lines=context_lines,
        paged=bool(input.cursor) or None,
    )


def _grep_params_hash(input: CodeGrepInput) -> str:
    import hashlib

    parts: dict[str, Any] = {
        "pattern": input.pattern,
        "regex": input.regex,
        "case_sensitive": input.case_sensitive,
        "path_prefix": input.path_prefix,
        "mode": input.mode,
    }
    if input.context_lines:
        parts["context_lines"] = input.context_lines
    # Ordering is stored in the pool so either server version can read a live page.
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
    partial: bool = False,
    partial_reason: str = "",
    context_source: ContextSource | None = None,
    complete_contexts: CompleteContexts = frozenset(),
    match_commits: dict[tuple[str, int], str],
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
            "match_commits": [match_commits.get((m["path"], m["line"]), "") for m in matches],
            "chunks_matched": chunks_matched,
            "truncated": truncated,
            "line_mapping": _GREP_LINE_MAPPING_VERSION,
            "partial": partial,
            "partial_reason": partial_reason,
            **({"context_source": context_source} if context_source else {}),
            **({"complete_contexts": sorted(complete_contexts)} if complete_contexts else {}),
        },
        indexed_commit="",
        route="grep",
    )
    return paging.encode(kind="g", run_id=run_id, offset=served, params=ph, indexed_commit="")


async def _serve_grep_page(
    input: CodeGrepInput, org: str, ns: str, repo: CodeRepo, route: str, started: float
) -> tuple[CodeGrepOutput | None, str, int]:
    from server.code.app import search_cursor as paging

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
    if payload.get("line_mapping") != _GREP_LINE_MAPPING_VERSION:
        return None, "source_line_mapping_changed", offset
    pool = [dict(m) for m in (payload.get("matches") or [])]
    pool_commits = payload.get("match_commits") or []
    limit = input.effective_limit
    excluded = set(input.exclude_unit_ids)
    page: list[dict[str, Any]] = []
    match_commits: dict[tuple[str, int], str] = {}
    i = offset
    while i < len(pool) and len(page) < limit:
        m = pool[i]
        commit = pool_commits[i] if i < len(pool_commits) else ""
        i += 1
        if str(m.get("path") or "") in excluded:
            continue
        match_commits[(m["path"], m["line"])] = str(commit or "")
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
    context: dict[str, Any] = {}
    budget = code_deadline.start(input.deadline_ms, started=started)
    if input.context_lines:
        complete_contexts = (
            frozenset(
                (str(path), int(line)) for path, line in (payload.get("complete_contexts") or ())
            )
            if input.prefer_complete_context and not input.include_body
            else frozenset()
        )
        source = payload.get("context_source") or {}
        revisions = match_revisions(
            pool,
            dict(zip(((hit["path"], hit["line"]) for hit in pool), pool_commits, strict=False)),
        )
        await recover_page_context(
            repo,
            org,
            ns,
            source,
            page,
            input.context_lines,
            revisions,
            complete_contexts=complete_contexts,
            max_source_bytes=_LINE_SCAN_MAX_CHARS,
            budget=budget,
        )
        context = page_contexts(
            source, page, input.context_lines, complete_contexts=complete_contexts
        )
    context_budget = _partial_fields(budget)
    trace_run_id = new_trace_run_id()
    route = route + "+page"
    bodies = await definitions_for(
        input,
        page,
        budget,
        match_commits=match_commits,
    )
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.grep",
        query=_grep_trace_query(input),
        route=route,
        args=_grep_trace_args(input),
        returned_ids=files,
        returned_paths=files,
        returned_scores=[],
        top_k=limit,
        tokens_returned=sum(len(str(m.get("text") or "")) for m in page) // 4
        + _context_tokens(context)
        + definitions_tokens(bodies),
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
            partial=bool(payload.get("partial") or context_budget.get("partial")),
            partial_reason=_notes(
                str(payload.get("partial_reason") or ""),
                str(context_budget.get("partial_reason") or ""),
            ),
            **bodies,
            **context,
        ),
        "",
        offset,
    )


async def _grep_page_by_rerun(
    input: CodeGrepInput, *, offset: int, reason: str, started: float
) -> CodeGrepOutput:
    """The pool row is gone: run the grep again deep enough to reach the
    offset, serve the slice past it, disclose why."""
    from server.code.app import search_cursor as paging

    if input.prioritize_patterns and isinstance(input.pattern, list) and len(input.pattern) > 1:
        raise ValidationError(f"grep cursor unavailable ({reason}); start a new search")
    limit = input.effective_limit
    fresh = input.model_copy(update={"cursor": "", "limit": min(200, offset + limit)})
    out = await _code_grep(fresh, started=started, offset=offset, page_limit=limit)
    out.as_of = paging.disclose(out.as_of, page_commit="", offset=offset, fallback=reason)
    if reason == "source_line_mapping_changed":
        out.partial = True
        out.partial_reason = _notes(out.partial_reason, "cursor offset used another source mapping")
        out.note = _notes(out.note, "restart pagination for complete results")
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
        if str(c.get("content") or "").startswith(_CONTINUED_PREFIXES):
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
    pattern = input.pattern
    with code_span("grep.files", route=route) as sp:
        files, total = await _bounded_scan(
            budget, repo.grep_files, org, ns, pattern, regex=input.regex, **kw
        )
        # a regex-shaped literal that names no file: once more as a regex; a non-empty literal
        # answer is untouched
        if not files and not input.regex and isinstance(pattern, str) and mg.regex_shaped(pattern):
            if mg.compile_fallback(pattern, case_sensitive=input.case_sensitive) is None:
                note = mg.REGEX_INVALID_NOTE
            elif _second_scan_allowed(budget, "regex retry"):
                files, total = await _bounded_scan(
                    budget, repo.grep_files, org, ns, pattern, regex=True, **kw
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
        query=_grep_trace_query(input),
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
    pattern = input.pattern
    assert isinstance(pattern, str)  # Validated in _code_grep.
    parsed = mg.split_message(pattern)
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
    coverage = await asyncio.to_thread(_line_coverage_fields, chunks, budget, previous=literal)
    if literal is not None and literal.matches:
        return literal.model_copy(
            update={
                **verdict_fields,
                "note": _notes(note, _MESSAGE_FALLBACK_NOTE, literal.note),
                **coverage,
            }
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
            **coverage,
        )
    files = list(dict.fromkeys(m["path"] for m in matches))
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.grep",
        query=pattern,
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
        **coverage,
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
    chunks = await recover_source_chunks(
        repo, org, ns, chunks, max_chars=_LINE_SCAN_MAX_CHARS, budget=budget
    )
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


def _line_coverage_fields(
    chunks: list[dict[str, Any]],
    budget: code_deadline.Budget | None,
    *,
    previous: CodeGrepOutput | None = None,
    additional_reason: str = "",
) -> dict[str, Any]:
    fields = _partial_fields(budget)
    reasons = [str(fields.get("partial_reason") or ""), additional_reason]
    if additional_reason:
        fields["partial"] = True
    scanned = 0
    for chunk in chunks:
        scanned += len(str(chunk.get("content") or ""))
        if scanned > _LINE_SCAN_MAX_CHARS:
            fields["partial"] = True
            reasons.append("source line coverage validation stopped at the scan limit")
            break
        if retained_source_lines(chunk) is None:
            fields["partial"] = True
            reasons.append(
                "some indexed chunks have no exact source line mapping; inspect the source file",
            )
            break
    if previous is not None and previous.partial:
        fields["partial"] = True
        reasons.append(previous.partial_reason)
    if fields.get("partial"):
        fields["partial_reason"] = _notes(*dict.fromkeys(reasons))
    return fields


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
        input.model_copy(
            update={
                "mode": "matches",
                "regex": False,
                "neighbours": None,
                "related_on_miss": False,
                "related_on_prefix": False,
            }
        ),
        route=route + "+literal_fallback",
        started=started,
        scope=scope,
        line_re=None,
        budget=budget,
    )
    out.fallback = "literal"
    return out
