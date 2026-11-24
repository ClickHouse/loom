"""code.symbol: definitions by name, id or location, with the bodies, enrichment,
uses, hierarchy and neighbour legs read beside them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Any

from server.code.api.models_symbol import (
    EDIT_TEST_CONTEXT_LINES,
    CodeEditContextInput,
    CodeSymbolInput,
    CodeSymbolOutput,
)
from server.code.app import symbol_uses as _uses_brief
from server.code.app.common import (
    answer_args,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
    tier_of,
)
from server.code.app.grep_source import GREP_MATCH_TEXT_CHARS
from server.code.app.index.symbol_forms import resolved_by_of
from server.code.app.spans import (
    code_span,
    current_stage_ms,
    set_code_span_attributes,
    span_attrs,
    start_stage_wall,
)
from server.code.app.symbol.hierarchy import _symbol_hierarchy
from server.code.app.symbol.named_examples import named_test_examples, owning_type
from server.code.app.symbol.neighbours import (
    _attach_enclosing_neighbours,
    _soft,
    documented_in_for,
)
from server.code.app.symbol.outline import _container_skeleton, _is_large_container
from server.code.app.symbol.resolve import (
    _parse_location,
    _resolve_symbols,
    _seed_ids_of,
    near_misses,
    symbol_body,
)
from server.code.app.symbol.uses import _mention_leg, _symbol_uses
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.identity.app.request_scope import request_narrowed
from server.observability.budget_shape import note_budget_shape

logger = logging.getLogger(__name__)


async def _symbol_neighbours(
    repo: CodeRepo, org: str, ns: str, rows: list[dict[str, Any]], input: CodeSymbolInput
) -> tuple[list[dict[str, Any]], int]:
    """The neighbours block (as code.grep serves it) with the resolved definitions as
    anchors (distinct qualified names, both tiers' ids joined from the rows in hand): k
    callers, callees, tests and citing documents each. Only when the request sets
    `neighbours` (unset = off, the measured default); fail-open."""
    if not input.neighbours or not rows:
        return [], 0
    anchors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows[:20]:
        qn = str(r.get("qualified_name") or "")
        if not qn or qn in seen or not r.get("symbol_id"):
            continue
        seen.add(qn)
        anchors.append(
            {
                "symbol_id": str(r["symbol_id"]),
                "qualified_name": qn,
                "symbol": str(r.get("name") or qn.rsplit("::", 1)[-1]),
                "path": str(r.get("path") or ""),
                "kind": str(r.get("kind") or "function"),
            }
        )
    try:
        entries, _capped, tokens = await _attach_enclosing_neighbours(
            repo, org, ns, anchors, rows, input.neighbour_k
        )
    except Exception:
        logger.warning("code.symbol: neighbours block failed for %r", input.name, exc_info=True)
        return [], 0
    return entries, tokens


async def _dependency_presence(
    input: CodeSymbolInput, org: str, ns: str
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """When the namespace defines no such symbol and its entry names `depends_on`
    namespaces, the definition's presence per dependency (the shared presence run: every
    namespace authorized at VIEWER first, fail closed, one deadline). A location, a
    symbol_id or an entry without dependencies asks nothing; an authorization failure
    answers nothing rather than failing the miss."""
    if input.symbol_id or not input.name or _parse_location(input.name):
        return [], {}
    from server.code.domain.index.repos import repo_for_namespace

    cfg = repo_for_namespace(org, ns)
    deps = tuple(d for d in cfg.depends_on if d != ns) if cfg is not None else ()
    if not deps:
        return [], {}
    from server.code.app import presence as code_presence

    def sub_for(dep: str) -> CodeSymbolInput:
        return input.model_copy(
            update={
                "namespace": dep,
                "namespaces": [],
                "presence": False,
                "include_body": False,
                "include_history": False,
                "include_examples": False,
                "include_lessons": False,
                "uses": False,
            }
        )

    try:
        with code_span("symbol.dependency", seeds=len(deps)):
            rows = await code_presence.run(
                input,
                list(deps),
                sub_for,
                handle_code_symbol,
                row_of=code_presence._symbol_row,
                op="code.symbol",
            )
    except Exception:
        logger.info("code.symbol: dependency presence declined for %s/%s", org, ns, exc_info=True)
        return [], {}
    return rows, code_presence.summarize(rows)


async def _staged(name: str, coro: Any) -> Any:
    """A leg's wall under its own stage name, so the trace row says which side read grew
    when a namespace did."""
    with code_span(name):
        return await coro


class _SymbolLegs:
    """The reads of a code.symbol answer that need only the resolved definitions, started
    together the moment the rows are in hand and awaited where their result is joined.
    Every leg is fail-open on its own (a missing block never fails the definitions);
    `cancel()` reaps the ones still running when the handler fails before joining them."""

    def __init__(
        self,
        repo: CodeRepo,
        org: str,
        ns: str,
        input: CodeSymbolInput,
        rows: list[dict[str, Any]],
        *,
        include_owner: bool,
        matched_members: bool,
    ) -> None:
        head = rows if matched_members else rows[:10]
        self.hashes = asyncio.ensure_future(_chunk_hashes(repo, org, ns, head))
        # uses leg: the grep for the name's mentions runs beside the body reads, never
        # after them; additive, never fatal.
        self.uses = (
            asyncio.ensure_future(_symbol_uses(repo, org, ns, head, limit=input.uses_limit))
            if input.uses
            else None
        )
        # the compact block: asked for; one declaration only; an explicit `uses`
        # (the full list) wins
        want_brief = input.uses_brief and not input.uses
        self.brief = (
            asyncio.ensure_future(
                _uses_brief.uses_brief(
                    repo,
                    org,
                    ns,
                    head,
                    mention_leg=_mention_leg,
                )
            )
            if want_brief and _uses_brief.single_declaration(head)
            else None
        )
        self.enrich = asyncio.ensure_future(
            _enrichment(
                repo,
                org,
                ns,
                input,
                head,
                include_owner=include_owner,
                matched_members=matched_members,
            )
        )
        self.documented_in = asyncio.ensure_future(
            _staged("symbol.documented_in", _soft(documented_in_for(repo, org, ns, rows), []))
        )
        self.block = asyncio.ensure_future(
            _staged("symbol.block", _symbol_neighbours(repo, org, ns, rows, input))
        )
        # a type's supertypes / subtypes (inherits edges); {} for functions
        self.hierarchy = asyncio.ensure_future(
            _staged("symbol.hierarchy", _symbol_hierarchy(repo, org, ns, rows))
        )

    def cancel(self) -> None:
        for t in (
            self.hashes,
            self.uses,
            self.brief,
            self.enrich,
            self.documented_in,
            self.block,
            self.hierarchy,
        ):
            if t is not None and not t.done():
                t.cancel()


async def _chunk_hashes(
    repo: CodeRepo, org: str, ns: str, head: list[dict[str, Any]]
) -> dict[str, str]:
    """Defining retrieval chunk hashes, including when the response elides a body.

    Memory links use complete normalized declaration fingerprints. A repository
    without this helper, or a failed read, leaves the retrieval hash unknown.
    """
    try:
        return await repo.chunk_hashes_for_symbols(
            org, ns, [str(r.get("symbol_id") or "") for r in head], rows=head
        )
    except Exception:  # noqa: BLE001 — additive, never fatal
        return {}


async def _symbol_bodies(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeSymbolInput,
    head: list[dict[str, Any]],
    *,
    expected_commit: str | None = None,
) -> list[tuple[str, list[dict[str, Any]], str, str]]:
    """Complete bodies by default; explicitly capped large containers use outlines."""

    async def one(r: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str, str]:
        if input.include_body:
            if not input.token_budget or not _is_large_container(r):
                if expected_commit is None:
                    text = await symbol_body(repo, org, ns, r)
                else:
                    row = {
                        **r,
                        "_extent_commit": r.get("extent_commit") or r.get("indexed_commit"),
                        "_tier_ids": r.get("tier_ids"),
                    }
                    text = await symbol_body(repo, org, ns, row, expected_commit=expected_commit)
                if text:
                    return text, [], "", "unit"
            if expected_commit is None and _is_large_container(r):
                body, outline, outline_next = await _container_skeleton(
                    repo, org, ns, r, input.token_budget
                )
                if body or outline or outline_next:
                    return body, outline, outline_next, "skeleton" if body else "card"
            start = int(r.get("start_line") or 1)
            span = max(1, int(r.get("end_line") or start) - start + 1)
            return "", [], f"Read {r['path']} offset={start} limit={span}", "card"
        return "", [], "", "unit"

    return list(await asyncio.gather(*(one(r) for r in head)))


def _unit_with_body(
    r: dict[str, Any], body: tuple[str, list[dict[str, Any]], str, str], hashes: dict[str, str]
) -> Any:
    from server.code.domain.units import unit_from_symbol

    text, outline, next_step, granularity = body
    u = unit_from_symbol(r, body=text)
    u.body_hash = hashes.get(str(r.get("symbol_id") or ""), "")
    if granularity == "skeleton":
        u.outline = outline
        u.granularity = "skeleton"
        u.next = next_step
    elif granularity == "card":
        u.elided = True
        u.granularity = "card"
        u.outline = outline
        start = max(1, u.start_line)
        span = max(1, u.end_line - start + 1)
        u.next = next_step or f"Read {u.path} offset={start} limit={span}"
    return u


async def _owner_context(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeSymbolInput,
    method: dict[str, Any],
    direct: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    from server.code.app import enrich as code_enrich

    want_examples = input.include_examples and not direct.get("examples")
    want_lessons = input.include_lessons and not request_narrowed() and not direct.get("lessons")
    if not (want_examples or want_lessons):
        return {}
    owner = await owning_type(repo, org, ns, method)
    if owner is None:
        return {}
    reads: dict[str, Awaitable[list[dict[str, Any]]]] = {}
    if want_examples:
        reads["examples"] = named_test_examples(repo, org, ns, owner, input.example_context_lines)
    if want_lessons:
        reads["lessons"] = code_enrich.lessons_for_symbol(
            org,
            ns,
            symbol_name=owner["qualified_name"],
            paths=[owner["path"]],
            extra_namespaces=input.lesson_namespaces,
        )
    values = await asyncio.gather(*(_soft(read, []) for read in reads.values()))
    return {
        key: [{**row, "owner_symbol": owner["qualified_name"]} for row in rows]
        for key, rows in zip(reads, values, strict=True)
        if rows
    }


async def _enrichment(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeSymbolInput,
    head: list[dict[str, Any]],
    *,
    include_owner: bool = False,
    matched_members: bool = False,
) -> dict[str, Any]:
    """Enrichment of the first definition (recent PRs, usage examples, lessons), the
    three reads gathered, each bounded and degradable; {} when not asked. Keyed on the
    resolved rows, which is what the units carry: their paths, their symbol ids, the
    first definition's qualified name."""
    if not head or not (input.include_history or input.include_examples or input.include_lessons):
        return {}
    from server.code.app import enrich as code_enrich

    paths = list(dict.fromkeys(str(r.get("path") or "") for r in head if r.get("path")))
    sids = _seed_ids_of(head)
    symbol = str(head[0].get("qualified_name") or "") or input.name

    async def none() -> None:
        return None

    async def lessons() -> list[dict[str, Any]]:
        if request_narrowed():
            return []  # a token that may not read every user's lessons

        async def read(name: str) -> list[dict[str, Any]]:
            return await code_enrich.lessons_for_symbol(
                org, ns, symbol_name=name, paths=paths, extra_namespaces=input.lesson_namespaces
            )

        if not matched_members:
            return await read(symbol)
        names = list(dict.fromkeys(row["qualified_name"] for row in head))
        groups = await asyncio.gather(*(read(name) for name in names))
        notes: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for name, group in zip(names, groups, strict=True):
            for note in group:
                key = (
                    note["namespace"],
                    note["memory_key"],
                    note.get("code_state") or "",
                    note.get("changed_since") or "",
                    note.get("suggestion") or "",
                )
                linked = notes.setdefault(key, {**note, "linked_symbols": []})
                linked["linked_symbols"].append(name)
        return list(notes.values())

    async def examples_for_definition() -> list[dict[str, Any]]:
        callers = code_enrich.usage_examples(
            repo,
            org,
            ns,
            sids,
            context_lines=input.example_context_lines,
            tests_only=input.test_examples_only,
        )
        if not (input.test_examples_only and _uses_brief.single_declaration(head)):
            return await callers
        named, sites = await asyncio.gather(
            code_enrich.bounded(
                named_test_examples(repo, org, ns, head[0], input.example_context_lines), []
            ),
            callers,
        )
        by_line = {(site["path"], site["line"]): site for site in sites}
        named = [by_line.pop((site["path"], site["line"]), site) for site in named]
        if by_line and all(site.get("evidence") == "name_reference" for site in named):
            named = named[:2]
        return (named + list(by_line.values()))[:3]

    with code_span("symbol.enrich"):
        # Each enrichment read is its own stage, so a regression names the read (one
        # `symbol.enrich` wall would hide which of the three grew).

        prs, examples, lessons_out = await asyncio.gather(
            _staged("enrich.prs", _soft(code_enrich.recent_prs(repo, org, ns, paths), None))
            if input.include_history
            else none(),
            _staged(
                "enrich.examples",
                _soft(examples_for_definition(), None),
            )
            if input.include_examples
            else none(),
            _staged("enrich.lessons", _soft(lessons(), None)) if input.include_lessons else none(),
        )
    out: dict[str, Any] = {}
    if prs is not None:
        out["recent_prs"] = prs
    if examples is not None:
        out["examples"] = examples
    if lessons_out is not None:
        out["lessons"] = lessons_out
    if include_owner and (matched_members or _uses_brief.single_declaration(head)):
        with code_span("enrich.owner_context"):
            out.update(
                await code_enrich.bounded(_owner_context(repo, org, ns, input, head[0], out), {})
            )
    if out.get("lessons"):
        from server.memory.app.code_plane_reads import fit_symbol_lessons

        out["lessons"] = fit_symbol_lessons(out["lessons"])
    return out


def _apply_enrichment(units: list[Any], enrichment: dict[str, Any]) -> None:
    """The enrichment rides on the first definition served."""
    if not units or not enrichment:
        return
    for field, value in enrichment.items():
        setattr(units[0], field, value)


def _budget_definitions(units: list[Any], token_budget: int) -> tuple[list[Any], int, int]:
    """Reserve definitions and site metadata before optional context and lessons."""
    from server.code.domain.units import budget_units, estimate_unit_tokens

    if not token_budget or not any(
        u.lessons or any("context" in site for site in u.examples) for u in units
    ):
        kept, tokens = budget_units(units, token_budget)
        return kept, tokens, 0
    bare = [
        u.model_copy(
            update={
                "lessons": [],
                "examples": [
                    {k: v for k, v in site.items() if k != "context"} for site in u.examples
                ],
            }
        )
        for u in units
    ]
    kept, tokens = budget_units(bare, token_budget)
    left_out = sum(len(u.lessons) for u in units)
    for unit, original in zip(kept, units, strict=False):
        for lesson in original.lessons:
            candidate = unit.model_copy(update={"lessons": [*unit.lessons, lesson]})
            extra = estimate_unit_tokens(candidate) - estimate_unit_tokens(unit)
            if tokens + extra <= token_budget:
                unit.lessons.append(lesson)
                tokens += extra
                left_out -= 1
        for index, site in enumerate(original.examples):
            if "context" not in site:
                continue
            examples = list(unit.examples)
            examples[index] = site
            candidate = unit.model_copy(update={"examples": examples})
            extra = estimate_unit_tokens(candidate) - estimate_unit_tokens(unit)
            if tokens + extra <= token_budget:
                unit.examples = examples
                tokens += extra
    return kept, tokens, left_out


async def handle_code_edit_context(input: CodeEditContextInput) -> CodeSymbolOutput:
    from server.code.app.symbol.edit_target import EditTarget, locate_edit_target

    if not input.name and not input.clues:
        raise ValidationError("code.edit_context requires a name or literal clues")
    target = await locate_edit_target(input) if input.clues else EditTarget(name=input.name)
    if not target.name:
        return CodeSymbolOutput(
            definitions=[], ambiguous=len(target.candidates) > 1, candidates=target.candidates
        )
    out = await handle_code_symbol(
        CodeSymbolInput(
            **input.model_dump(exclude={"name", "clues"}),
            name=target.name,
            include_history=False,
            test_examples_only=True,
            example_context_lines=EDIT_TEST_CONTEXT_LINES,
        ),
        op_name="code.edit_context",
        expected_commit=target.revision,
        matched_rows=target.rows,
        matched_members=target.grouped,
        match_witnesses=target.matches,
    )
    if target.revision is not None:
        if not out.definitions:
            out.candidates = target.candidates
            out.ambiguous = len(target.candidates) > 1
        else:
            if target.recovered_argument:
                out.resolved_by = "repository_argument"
            else:
                out.resolved_by = "file_clues" if input.name else "repository_clues"
            out.candidates = [
                candidate
                for candidate in target.candidates
                if not any(
                    unit.path == candidate["path"]
                    and unit.start_line <= candidate["start_line"] <= unit.end_line
                    for unit in out.definitions
                )
            ]
    return out


def _matches_source(unit: Any, matches: list[dict[str, Any]]) -> bool:
    """A resolved span must contain a literal match at its original source line."""
    lines = unit.body.split("\n")
    return any(
        match["path"] == unit.path
        and 0 <= (offset := match["line"] - unit.start_line) < len(lines)
        and lines[offset].strip()[:GREP_MATCH_TEXT_CHARS] == match["text"]
        for match in matches
    )


async def _unresolved_symbol(
    repo: CodeRepo, org: str, ns: str, input: CodeSymbolInput
) -> CodeSymbolOutput:
    with code_span("symbol.resolve", route="symbol_fuzzy") as sp:
        near = await near_misses(repo, org, ns, input.name, fallback=input.symbol_id)
        span_attrs(sp, results=len(near))
    dep_rows, dep_summary = await _dependency_presence(input, org, ns)
    set_code_span_attributes(
        route="symbol_fuzzy" + ("+dependency" if dep_rows else ""),
        results=0,
        abstain=True,
        retrieval_risk="abstain",
        consumer=input.consumer,
    )
    return CodeSymbolOutput(
        definitions=[],
        ambiguous=False,
        candidates=near,
        presence=dep_rows,
        summary=dep_summary,
        resolved_by="dependency" if dep_summary.get("present") else "",
    )


async def _rank_symbol_rows(
    repo: CodeRepo, org: str, ns: str, input: CodeSymbolInput, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str]:
    if not (input.near_paths or input.pr_number):
        return rows, ""

    from server.code.app import symbol_context as code_symbol_context

    if _uses_brief.single_declaration(rows):
        return rows, code_symbol_context.NONE
    with code_span("symbol.context", results=len(rows)):
        return await code_symbol_context.rank_by_context(
            repo,
            org,
            ns,
            rows,
            near_paths=input.near_paths,
            pr_number=input.pr_number,
        )


async def handle_code_symbol(
    input: CodeSymbolInput,
    *,
    op_name: str = "code.symbol",
    expected_commit: str | None = None,
    matched_rows: list[dict[str, Any]] | None = None,
    matched_members: bool = False,
    match_witnesses: list[dict[str, Any]] | None = None,
) -> CodeSymbolOutput:
    from server.code.app import fan_in as code_fan_in

    require_code_intel()
    # Presence on several namespaces: one verdict per namespace, no bodies. Unset: the
    # plain single-namespace lookup below.
    if input.presence:
        from server.code.app import presence as code_presence

        return await code_presence.symbol_presence(input, handle_code_symbol)
    started = time.monotonic()
    start_stage_wall()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    with code_span("symbol.resolve") as sp:
        rows = matched_rows or await _resolve_symbols(
            repo, org, ns, symbol_id=input.symbol_id, name=input.name
        )
        span_attrs(sp, results=len(rows), tier=tier_of(rows))
    if not rows:
        return await _unresolved_symbol(repo, org, ns, input)

    rows, picked_by = await _rank_symbol_rows(repo, org, ns, input, rows)

    # Every read that needs only the resolved definitions starts here (the defining-chunk
    # hashes, the uses legs, the enrichment, the citing documents and the neighbours
    # block) and the bodies are read side by side too, so none of them waits on the
    # bodies. Each leg stays fail-open on its own.
    head = rows if matched_members else rows[:10]
    legs = _SymbolLegs(
        repo,
        org,
        ns,
        input,
        rows,
        include_owner=op_name == "code.edit_context",
        matched_members=matched_members,
    )
    try:
        with code_span("symbol.bodies", results=len(head)):
            bodies = await _symbol_bodies(
                repo, org, ns, input, head, expected_commit=expected_commit
            )
        hashes = await legs.hashes
        units = [_unit_with_body(r, b, hashes) for r, b in zip(head, bodies, strict=True)]
        if match_witnesses and not all(_matches_source(u, match_witnesses) for u in units):
            legs.cancel()
            route = "file_clues_unverified"
            trace_id = _symbol_trace(input, org, ns, route, [], 0, started, op_name=op_name)
            set_code_span_attributes(
                route=route,
                results=0,
                abstain=True,
                retrieval_risk="abstain",
                consumer=input.consumer,
            )
            return CodeSymbolOutput(definitions=[], trace_run_id=trace_id)

        # symbol fan-in: one point read on the stats table's ORDER BY; the field rides on
        # every definition when the weekly stats are enabled, and an explicit filter/sort
        # asks for it regardless.
        brief: dict[str, Any] = {}
        brief_stats: dict[str, dict[str, Any]] | None = None
        if legs.brief is not None:
            brief, brief_stats = await legs.brief
        if units:
            with code_span("symbol.fan_in") as sp:
                span_attrs(
                    sp,
                    results=await code_fan_in.annotate_units(
                        repo,
                        org,
                        ns,
                        units,
                        stats=brief_stats,
                    ),
                )
            if input.min_fan_in:
                units = [u for u in units if u.fan_in >= input.min_fan_in]
            if input.sort_by == "fan_in":
                units = code_fan_in.order_by_fan_in(units)

        _apply_enrichment(units, await legs.enrich)  # PRs, examples, lessons
        # the documents that cite the definition, beside it
        documented_in = await legs.documented_in
        hierarchy = await legs.hierarchy
        # the neighbours block on the definitions themselves
        block, block_tokens = await legs.block
        uses_leg = legs.uses
        uses_pair = await uses_leg if uses_leg is not None else ([], False)
    except BaseException:
        legs.cancel()
        raise

    from server.code.domain.units import estimate_json_tokens, remaining_token_budget

    # the compact block is budgeted before the bodies are trimmed
    uses_tokens = estimate_json_tokens(brief) if brief else 0
    brief_omitted = bool(input.token_budget and uses_tokens > input.token_budget)
    if brief_omitted:
        brief, uses_tokens = {}, 0
    with code_span("symbol.budget"):
        budgeted, tokens, lessons_left_out = _budget_definitions(
            units, remaining_token_budget(input.token_budget, uses_tokens)
        )
    tokens += uses_tokens
    if documented_in:
        tokens += estimate_json_tokens(documented_in)
    tokens += block_tokens
    ambiguous = not matched_members and len({(u.symbol, u.signature) for u in budgeted}) > 1
    uses: list[dict[str, Any]] | dict[str, Any]
    uses, uses_truncated = uses_pair
    uses_truncated |= brief_omitted
    if uses_leg is not None:
        tokens += estimate_json_tokens(uses)
    elif brief:
        uses = brief
    route = (
        "symbol_exact+uses"
        if input.uses
        else ("symbol_exact+uses_brief" if brief else "symbol_exact")
    )
    if matched_members:
        route = "edit_context_members"
    # The shape this call served prices its budget and is written on the trace row, so the
    # ranked lookup's own p99 is measurable.

    shape = "ranked" if (input.near_paths or input.pr_number) else ("uses_brief" if brief else "")
    note_budget_shape(shape)

    tokens += estimate_json_tokens(hierarchy) if hierarchy else 0
    trace_run_id = _symbol_trace(
        input,
        org,
        ns,
        route,
        budgeted,
        tokens,
        started,
        op_name=op_name,
        args=answer_args(
            input,
            "pr_number",
            "uses_brief",
            "uses",
            test_examples_only=(
                True if input.include_examples and input.test_examples_only else None
            ),
            example_context_lines=(
                (input.example_context_lines or None) if input.include_examples else None
            ),
            near_paths_n=len(input.near_paths) or None,
            picked_by=picked_by or None,
            shape=shape or None,
        ),
    )
    set_code_span_attributes(
        route=route,
        tier=tier_of(budgeted),
        results=len(budgeted),
        tokens_returned=tokens,
        ambiguous=ambiguous,
        abstain=not budgeted,
        retrieval_risk="low" if budgeted else "abstain",
        consumer=input.consumer,
    )
    return CodeSymbolOutput(
        definitions=budgeted,
        lessons_left_out=lessons_left_out,
        ambiguous=ambiguous,
        candidates=[],
        tokens_returned=tokens,
        trace_run_id=trace_run_id,
        uses=uses,
        uses_truncated=uses_truncated,
        uses_tokens=uses_tokens,
        picked_by=picked_by,
        resolved_by=resolved_by_of(rows),
        hierarchy=hierarchy,
        documented_in=documented_in,
        neighbours=block,
        anchors=len(block),
        neighbour_tokens=block_tokens,
    )


def _symbol_trace(
    input: Any,
    org: str,
    ns: str,
    route: str,
    budgeted: list[Any],
    tokens: int,
    started: float,
    *,
    op_name: str = "code.symbol",
    args: dict[str, Any] | None = None,
) -> str:
    """The code.symbol trace row; returns its run id. `args` are the knobs that shaped the
    answer (pr_number, near_paths_n, uses_brief, picked_by, shape); the bare lookup writes
    none."""

    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op=op_name,
        query=input.name or input.symbol_id,
        route=route,
        args=args or None,
        returned_ids=[u.unit_id for u in budgeted],
        returned_paths=[u.path for u in budgeted],
        returned_scores=[1.0] * len(budgeted),
        top_k=len(budgeted),
        tokens_returned=tokens,
        abstain=not budgeted,
        retrieval_risk="low" if budgeted else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        stage_ms=current_stage_ms(),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
    )
    return trace_run_id
