"""code.context_pack / code.context_packs / code.map handlers."""

from __future__ import annotations

import logging
import time
from typing import Any

from server.code.api.models_context import (
    CodeContextPackInput,
    CodeContextPackOutput,
    CodeContextPackRow,
    CodeContextPacksInput,
    CodeContextPacksOutput,
    CodeMapInput,
    CodeMapOutput,
)
from server.code.api.models_search import CodeSearchInput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.index_status import _s, _ts_or_empty
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.ttl_cache import TtlCache
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)

_MAP_CACHE: TtlCache[tuple[str, str, str, int], str] = TtlCache(900.0, maxsize=200)


async def handle_code_context_pack(input: CodeContextPackInput) -> CodeContextPackOutput:
    from server.code.app.index.packs import estimate_tokens, fit_pack_budget

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    def _trace(*, route: str, ids: list[str], tokens: int) -> str:
        trace_run_id = new_trace_run_id()
        schedule_code_trace(
            org=org,
            ns=ns,
            op="code.context_pack",
            query=(
                f"pr:{input.pr_number}"
                if input.pr_number
                else f"task:{input.task[:200]}"
                if input.task.strip()
                else "orientation"
            ),
            route=route,
            returned_ids=ids,
            returned_paths=[],
            returned_scores=[],
            top_k=1,
            tokens_returned=tokens,
            abstain=not ids,
            retrieval_risk="low" if ids else "abstain",
            embedding_model="",
            started_monotonic=started,
            trace_run_id=trace_run_id,
            consumer=input.consumer,
            agent=input.agent,
            user_id=input.user_id,
        )
        set_code_span_attributes(
            route=route,
            source=route.removeprefix("pack_"),
            results=len(ids),
            tokens_returned=tokens,
            abstain=not ids,
            consumer=input.consumer,
        )
        return trace_run_id

    if input.pr_number:
        with code_span("context_pack.precomputed") as sp:
            row = await repo.context_pack(org, ns, input.pr_number)
            span_attrs(sp, results=int(row is not None))
        if row is not None:
            pack = str(row["pack"])
            if input.sections:
                from server.code.app.index.packs import filter_sections

                pack = filter_sections(pack, list(input.sections))
            pack, truncated = fit_pack_budget(pack, input.token_budget)
            tokens = estimate_tokens(pack)
            return CodeContextPackOutput(
                context=pack,
                tokens_used=tokens,
                source="precomputed",
                truncated=truncated,
                merge_commit_sha=_s(row.get("merge_commit_sha")),
                trace_run_id=_trace(
                    route="pack_precomputed", ids=[f"pr:{input.pr_number}"], tokens=tokens
                ),
            )
        # A generic repository map does not answer a request about this PR.
        # The pack may simply lag the merge; say so within a tiny response.
        logger.info("code.context_pack: no precomputed pack for PR %s", input.pr_number)
        context = (
            f"No precomputed context pack for PR #{input.pr_number} in {ns}. "
            "The PR may not be indexed yet. Inspect its diff with code.impact "
            "or retry after indexing."
        )
        tokens = estimate_tokens(context)
        return CodeContextPackOutput(
            context=context,
            tokens_used=tokens,
            source="unavailable",
            merge_commit_sha="",
            trace_run_id=_trace(route="pack_unavailable", ids=[], tokens=tokens),
        )

    if input.task.strip():
        briefing = await _task_briefing(repo, org, ns, input)
        if briefing:
            text, named = briefing
            tokens = estimate_tokens(text)
            out = CodeContextPackOutput(
                context=text,
                tokens_used=tokens,
                source="task",
                merge_commit_sha="",
                trace_run_id=_trace(route="pack_task", ids=named[:50], tokens=tokens),
            )
            out._paths = named
            return out

    # Orientation mode: repo map + top module cards.
    with code_span("context_pack.orientation"):
        map_text = await _render_map(repo, org, ns, "", input.token_budget)
    map_text = await _with_repo_docs(org, ns, map_text, input.token_budget)
    tokens = estimate_tokens(map_text)
    return CodeContextPackOutput(
        context=map_text,
        tokens_used=tokens,
        source="orientation",
        merge_commit_sha="",
        trace_run_id=_trace(
            route="pack_orientation", ids=["map"] if map_text else [], tokens=tokens
        ),
    )


async def _task_briefing(
    repo: CodeRepo, org: str, ns: str, input: CodeContextPackInput
) -> tuple[str, list[str]] | None:
    """One code.search for the task, then the briefing built around its files. None when the
    search finds nothing in the index — the orientation map answers then."""
    from server.code.app.index.task_pack import build_task_pack
    from server.code.app.search.handler import handle_code_search

    with code_span("context_pack.task_search"):
        found = await handle_code_search(
            CodeSearchInput(
                org=org,
                namespace=ns,
                query=input.task.strip(),
                top_k=12,
                bodies="none",
                consumer=input.consumer,
                agent=input.agent,
                user_id=input.user_id,
            )
        )
    hits = [r if isinstance(r, dict) else r.model_dump() for r in (found.results or [])]
    if not hits:
        return None
    with code_span("context_pack.task_build"):
        return await build_task_pack(
            repo, org, ns, task=input.task, hits=hits, token_budget=input.token_budget
        )


async def _with_repo_docs(org: str, ns: str, text: str, token_budget: int) -> str:
    """The orientation pack names the repository's documents after the map, in what the budget
    leaves (at most 300 tokens); the documents themselves stay in the knowledge plane."""
    from server.code.app.index.packs import estimate_tokens
    from server.knowledge.app.repo_docs import DOCS_SECTION_MAX_TOKENS, docs_section, list_repo_docs

    room = min(DOCS_SECTION_MAX_TOKENS, int(token_budget) - estimate_tokens(text))
    if room < 40:
        return text
    docs_ns, docs = await list_repo_docs(org, ns)
    section = docs_section(docs_ns, docs, room)
    return f"{text}\n\n{section}" if section and text else (section or text)


async def handle_code_map(input: CodeMapInput) -> CodeMapOutput:
    from server.code.app.index.packs import estimate_tokens

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    with code_span("map.render"):
        text = await _render_map(repo, org, ns, input.module_prefix, input.token_budget)
    head = await repo.get_state(org, ns, "git_head")
    tokens = estimate_tokens(text)
    # the header line alone means no module cards matched the prefix
    entries = max(0, text.count("\n- **")) + text.count("\n- `")
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.map",
        query=input.module_prefix or "/",
        route="map_render",
        returned_ids=[input.module_prefix or "/"] if entries else [],
        returned_paths=[input.module_prefix] if input.module_prefix else [],
        returned_scores=[],
        top_k=1,
        tokens_returned=tokens,
        abstain=not entries,
        retrieval_risk="low" if entries else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
    )
    set_code_span_attributes(
        route="map_render",
        results=entries,
        tokens_returned=tokens,
        abstain=not entries,
        consumer=input.consumer,
    )
    return CodeMapOutput(
        map=text,
        tokens_used=tokens,
        commit_sha=head,
        trace_run_id=trace_run_id,
    )


async def _render_map(
    repo: CodeRepo, org: str, ns: str, module_prefix: str, token_budget: int
) -> str:

    from server.code.app import fan_in as code_fan_in
    from server.code.app.index.packs import estimate_tokens
    from server.embedding.embedder import get_embedder

    key = (org, ns, module_prefix, token_budget)
    cached = _MAP_CACHE.get(key)
    if cached is not None:
        return cached

    model = ""
    try:
        model = get_embedder().model_name
    except Exception:  # noqa: BLE001 — summaries read with no model
        pass
    rows = await repo.top_summaries(org, ns, model, module_prefix=module_prefix, limit=60)
    callers, cards = await _call_graph_hubs(repo, org, ns, model, module_prefix)
    lines = ["# Repo map (include-graph and call-graph hubs; per-merge fresh)"]
    for entry in blend_map_entries(rows, callers, cards):
        lines.append(entry)
        text = "\n".join(lines)
        if estimate_tokens(text) > token_budget:
            lines.pop()
            break
    # Most-called symbols under the prefix: a symbol-level section from the weekly stats, tests
    # excluded.
    try:
        hubs = await code_fan_in.top_under(repo, org, ns, module_prefix, limit=10)
    except Exception:  # noqa: BLE001 — the file cards stand alone
        hubs = []
    if hubs:
        head = "## Most-called symbols" + (f" under {module_prefix}" if module_prefix else "")
        lines.extend(["", head + " (non-test call sites; weekly)"])
        for h in hubs:
            lines.append(
                f"- `{h['qualified_name']}` — {int(h['fan_in_nontest'])} call sites in "
                f"{int(h['caller_files'])} files ({h['path']})"
            )
            if estimate_tokens("\n".join(lines)) > token_budget:
                lines.pop()
                break
    text = "\n".join(lines)
    _MAP_CACHE.put(key, text)
    return text


async def _call_graph_hubs(
    repo: CodeRepo, org: str, ns: str, model: str, module_prefix: str
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The files most other files call into, with their
    cards. Fail-soft: the include-graph map stands alone."""
    try:
        callers = await repo.top_files_by_callers(org, ns, path_prefix=module_prefix, limit=20)
        paths = [str(r["path"]) for r in callers]
        cards = await repo.cards_for_paths(org, ns, model, paths) if paths else {}
    except Exception:
        logger.info("code map: call-graph hubs unavailable for %s/%s", org, ns, exc_info=True)
        return [], {}
    return callers, cards


def blend_map_entries(
    pagerank_rows: list[dict[str, Any]],
    caller_rows: list[dict[str, Any]],
    cards: dict[str, str],
) -> list[str]:
    """The map's file lines, alternating the include graph's hubs (pagerank) with the call
    graph's (files called from the most other files). A language whose imports the include
    graph does not carry — a Python tree scores pagerank 0 on every file — still gets a map
    instead of losing it to the frontend's includes. A file on both lists shows once, first
    where it ranks higher; a pagerank-0 card is not an include hub."""
    ranked = [
        r
        for r in pagerank_rows
        if float(r.get("pagerank") or 0.0) > 0.0 or int(r.get("in_degree") or 0) > 0
    ]
    by_include = [
        (
            str(r["scope_path"]),
            f"(included by {int(r.get('in_degree') or 0)})",
            str(r.get("summary") or "").strip(),
        )
        for r in ranked
    ]
    by_calls = [
        (
            str(r["path"]),
            f"(called from {int(r.get('callers') or 0)} files)",
            cards.get(str(r["path"]), "").strip(),
        )
        for r in caller_rows
    ]
    if not by_calls:  # no stats: the include-graph map, pagerank-0 cards included
        by_include = [
            (
                str(r["scope_path"]),
                f"(included by {int(r.get('in_degree') or 0)})",
                str(r.get("summary") or "").strip(),
            )
            for r in pagerank_rows
        ]
    out: list[str] = []
    seen: set[str] = set()
    for i in range(max(len(by_include), len(by_calls))):
        for lane in (by_include, by_calls):
            if i >= len(lane) or lane[i][0] in seen:
                continue
            path, why, summary = lane[i]
            seen.add(path)
            out.append(f"- **{path}** {why}" + (f" — {summary}" if summary else ""))
    return out


async def handle_code_context_packs(input: CodeContextPacksInput) -> CodeContextPacksOutput:
    """List the stored per-PR context packs of a repository, optionally for one PR."""
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    rows, total = await CodeRepo().list_context_packs(
        org,
        ns,
        limit=input.limit,
        offset=input.offset,
        pr_number=input.pr_number,
    )
    set_code_span_attributes(results=total, consumer=input.consumer)
    return CodeContextPacksOutput(
        rows=[
            CodeContextPackRow(
                pr_number=int(r["pr_number"]),
                merge_commit_sha=_s(r.get("merge_commit_sha")),
                pack_tokens=int(r.get("pack_tokens") or 0),
                created_at=str(r.get("created_at") or ""),
                title=str(r.get("title") or ""),
                author=str(r.get("author") or ""),
                merged_at=_ts_or_empty(r.get("merged_at")),
            )
            for r in rows
        ],
        total=total,
    )
