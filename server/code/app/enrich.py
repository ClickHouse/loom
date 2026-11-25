"""Result enrichment: what a checkout can't contain.

Attached to code.symbol (and optionally code.search top hits): the last merged PRs that
touched the symbol's file, usage examples ranked by test coverage + recency, and the
memories linked to the symbol (`server.memory.app.code_links`).

Every enrichment is bounded (`code_enrich_timeout_s`) and degrades to an empty block —
enrichment must never fail or slow the core answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable
from typing import Any

from server.code.app.spans import code_span
from server.code.domain.index.test_conventions import is_test_unit_path
from server.code.domain.symbol_body import retained_source_window
from server.code.store.index import CodeRepo
from server.embedding.embedder import served_model_label
from server.settings import settings

logger = logging.getLogger(__name__)


async def bounded[T](coro: Awaitable[T], fallback: T, seconds: float | None = None) -> T:
    """`coro`'s answer within `seconds` (`code_enrich_timeout_s` when None), else
    `fallback`."""
    try:
        return await asyncio.wait_for(
            coro, timeout=settings.code_enrich_timeout_s if seconds is None else seconds
        )
    except Exception as e:  # noqa: BLE001 — enrichment degrades, never fails
        logger.debug("code enrich degraded: %s", e)
        return fallback


async def recent_prs(
    repo: CodeRepo, org: str, ns: str, paths: list[str], *, limit: int = 5
) -> list[dict[str, Any]]:
    rows = await bounded(repo.pr_history_for(org, ns, paths=paths, limit=limit), [])
    return [
        {
            "pr_number": int(r["pr_number"]),
            "title": str(r["title"]),
            "author": str(r["author"]),
            "merged_at": str(r["merged_at"]),
            # a revert touching this path: the area has bitten before
            "is_revert": bool(r.get("is_revert")),
        }
        for r in rows
    ]


async def usage_examples(
    repo: CodeRepo,
    org: str,
    ns: str,
    symbol_ids: list[str],
    *,
    limit: int = 3,
    context_lines: int = 0,
    tests_only: bool = False,
) -> list[dict[str, Any]]:
    """Call sites of the symbol, optionally restricted to indexed tests."""
    with_context = bool(context_lines and settings.embedding_model)
    edges = await bounded(
        repo.edges_for_symbols(
            org,
            ns,
            symbol_ids,
            direction="callers",
            limit=50,
            with_revision=with_context,
            tests_only=tests_only,
        ),
        [],
    )
    if not edges:
        return []
    caller_paths = sorted({str(e["path"]) for e in edges})  # fixed order into the 50-cut
    tested = await bounded(repo.test_map_for(org, ns, caller_paths), [])
    tested_paths = {str(t["src_path"]) for t in tested}
    ranked = sorted(
        edges,
        key=lambda e: (str(e["path"]) not in tested_paths, str(e["path"])),
    )
    by_location: dict[tuple[str, int], dict[str, Any]] = {}
    for edge in ranked:
        by_location.setdefault((str(edge["path"]), int(edge["line"])), edge)
    sites = list(by_location.values())
    baseline = sites[: max(1, limit)]
    requested = baseline
    if tests_only and with_context:
        preferred = _prefer_distinct_test_windows(sites, context_lines)[: len(baseline)]
        requested = preferred + [edge for edge in baseline if edge not in preferred]
    out = []
    revisions: list[str] = []
    for e in requested:
        out.append(
            {
                "caller": str(e["from_qualified_name"]),
                "path": str(e["path"]),
                "line": int(e["line"]),
                "tier": str(e["tier"]),
                "test_covered": str(e["path"]) in tested_paths,
            }
        )
        if with_context:
            revisions.append(str(e.get("indexed_commit") or "").rstrip("\0"))
    if with_context:
        model = served_model_label(settings.embedding_model)
        examples = await asyncio.gather(
            *(
                _example_context(repo, org, ns, site, context_lines, model, revision)
                for site, revision in zip(out, revisions, strict=True)
            )
        )
        # A promoted site only earns a slot when its revision still has source.
        return [
            example
            for edge, example in zip(requested, examples, strict=True)
            if "context" in example or edge in baseline
        ][: len(baseline)]
    return out


async def indirect_test_examples(
    repo: CodeRepo,
    org: str,
    ns: str,
    symbol_ids: list[str],
    definition: dict[str, Any],
    revision: str,
    context_lines: int,
) -> list[dict[str, Any]]:
    """Verified test source reached through one non-test caller at the same revision."""
    if not (revision and context_lines and settings.embedding_model):
        return []
    if definition["kind"] not in {"function", "method"}:
        return []
    callers = await repo.edges_for_symbols(
        org,
        ns,
        symbol_ids,
        direction="callers",
        limit=12,
        min_confidence=0.8,
        with_revision=True,
        distinct_callers_at=revision,
    )
    bridges: dict[str, dict[str, Any]] = {}
    for edge in callers:
        caller = str(edge["from_symbol_id"])
        if (
            caller not in symbol_ids
            and not is_test_unit_path(str(edge["path"]))
            and edge["to_qualified_name"] == definition["qualified_name"]
            and str(edge["indexed_commit"]).rstrip("\0") == revision
        ):
            bridges.setdefault(caller, edge)
    if not bridges:
        return []
    tests = await repo.edges_for_symbols(
        org,
        ns,
        list(bridges),
        direction="callers",
        limit=30,
        min_confidence=0.8,
        with_revision=True,
        tests_only=True,
        distinct_callers_at=revision,
    )
    if not tests:
        return []
    path = str(definition["path"])
    files = await repo.file_rows_for_paths(
        org, ns, [path, *(str(e["path"]) for e in (*bridges.values(), *tests))]
    )
    if files.get(path, {}).get("indexed_commit") != revision:
        return []
    sites = _verified_indirect_sites(tests, bridges, files, revision)
    candidates = _prefer_distinct_test_windows(sites, context_lines)[:6]
    for site in candidates:
        site.pop("indexed_commit")
    model = served_model_label(settings.embedding_model)
    examples = await asyncio.gather(
        *(
            _example_context(repo, org, ns, site, context_lines, model, revision)
            for site in candidates
        )
    )
    return [row for row in examples if "context" in row][:3]


def _verified_indirect_sites(
    tests: list[dict[str, Any]],
    bridges: dict[str, dict[str, Any]],
    files: dict[str, dict[str, Any]],
    revision: str,
) -> list[dict[str, Any]]:
    def current(edge: dict[str, Any]) -> bool:
        file = files.get(str(edge["path"]), {})
        return (
            file.get("indexed_commit") == revision
            and str(edge["indexed_commit"]).rstrip("\0") == revision
            and 0 < int(edge["line"]) <= file.get("loc", 0)
        )

    by_location: dict[tuple[str, int], dict[str, Any]] = {}
    for edge in tests:
        bridge = bridges.get(str(edge["to_symbol_id"]))
        if (
            bridge is None
            or not is_test_unit_path(str(edge["path"]))
            or edge["to_qualified_name"] != bridge["from_qualified_name"]
            or not (current(edge) and current(bridge))
        ):
            continue
        path, line = str(edge["path"]), int(edge["line"])
        by_location.setdefault(
            (path, line),
            {
                "caller": str(edge["from_qualified_name"]),
                "path": path,
                "line": line,
                "tier": str(edge["tier"]),
                "indexed_commit": revision,
                "evidence": "indirect_call",
                "via": {
                    "symbol": str(bridge["from_qualified_name"]),
                    "path": str(bridge["path"]),
                    "line": int(bridge["line"]),
                },
            },
        )
    return list(by_location.values())


def _prefer_distinct_test_windows(edges: list[dict[str, Any]], radius: int) -> list[dict[str, Any]]:
    """Prefer call sites outside the source windows already selected."""
    anchors: dict[tuple[str, str], list[int]] = defaultdict(list)
    distinct = []
    nearby = []
    for edge in edges:
        revision = str(edge.get("indexed_commit") or "").rstrip("\0")
        line = int(edge["line"])
        key = (str(edge["path"]), revision)
        if revision and any(abs(line - anchor) <= radius for anchor in anchors[key]):
            nearby.append(edge)
        else:
            if revision:
                anchors[key].append(line)
            distinct.append(edge)
    return distinct + nearby


async def _example_context(
    repo: CodeRepo,
    org: str,
    ns: str,
    site: dict[str, Any],
    radius: int,
    model: str,
    revision: str,
) -> dict[str, Any]:
    line = site["line"]
    if line <= 0 or not revision:
        return site
    start, end = max(1, line - radius), line + radius
    with code_span("enrich.example_context"):
        rows = await bounded(
            repo.source_chunks_for_span(
                org, ns, site["path"], start, end, model, with_revision=True
            ),
            [],
        )
    if len(rows) > 400:
        return site
    window = retained_source_window(rows, start, end, anchor=line, revision=revision)
    if window is None:
        return site
    start, end, text = window
    return {
        **site,
        "context": {"commit": revision, "start_line": start, "end_line": end, "text": text},
    }


async def lessons_for_symbol(
    org: str,
    ns: str,
    *,
    symbol_name: str,
    paths: list[str],
    extra_namespaces: list[str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """The memories linked to the symbol, from the caller's lesson scope: the readable
    `extra_namespaces` the call names (over MCP its memory home) and the repository's
    team namespace.
    Each carries `code_state`: `changed` when this branch's definition differs from the
    one the memory was written against. Any failure -> []."""
    from server.memory.app import code_plane_reads

    async def read() -> list[dict[str, Any]]:
        nss = await code_plane_reads.lesson_namespaces(org, ns, extra_namespaces)
        return await code_plane_reads.lessons_for_symbol(
            org,
            ns,
            qualified_name=symbol_name,
            paths=paths,
            namespaces=nss,
            limit=limit,
        )

    return await bounded(read(), [])
