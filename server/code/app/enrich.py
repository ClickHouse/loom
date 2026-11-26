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
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from itertools import pairwise
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from server.code.app.spans import code_span, span_attrs
from server.code.domain.extract.treesitter import lang_for_path
from server.code.domain.index.test_conventions import is_test_unit_path, sibling_test_paths, snake
from server.code.domain.symbol_body import retained_source_window
from server.code.store.index import CodeRepo
from server.embedding.embedder import served_model_label
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.units import CodeUnit

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
    ready: list[dict[str, Any]] | None = None,
    related_names: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Call sites of the symbol, optionally restricted to indexed tests."""
    with_context = bool(context_lines and settings.embedding_model)
    edges = await bounded(
        repo.edges_for_symbols(
            org,
            ns,
            symbol_ids,
            direction="callers",
            limit=settings.code_enrich_example_candidate_limit,
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
    limit = max(1, limit)
    ranked = _rank_test_sites(sites, related_names=related_names) if tests_only else sites

    def location(edge: dict[str, Any]) -> dict[str, Any]:
        return {
            "caller": str(edge["from_qualified_name"]),
            "path": str(edge["path"]),
            "line": int(edge["line"]),
            "tier": str(edge["tier"]),
            "test_covered": str(edge["path"]) in tested_paths,
        }

    if not with_context:
        return [location(edge) for edge in ranked[:limit]]

    model = served_model_label(settings.embedding_model)

    async def read(edge: dict[str, Any]) -> dict[str, Any]:
        revision = str(edge.get("indexed_commit") or "").rstrip("\0")
        return await _example_context(repo, org, ns, location(edge), context_lines, model, revision)

    if not tests_only:
        return await asyncio.gather(*(read(edge) for edge in ranked[:limit]))
    preferred = _prefer_distinct_test_windows(ranked, context_lines)[:limit]
    baseline = sites[:limit]
    backups = _prefer_distinct_test_windows(sites, context_lines)[:limit] + baseline
    backed = await _retained_examples(preferred, backups, limit=limit, read=read, ready=ready)
    found = {(example["path"], example["line"]) for example in backed}
    locations = [location(edge) for edge in baseline if (edge["path"], edge["line"]) not in found]
    return (backed + locations)[:limit]


async def _retained_examples(
    preferred: list[dict[str, Any]],
    backups: list[dict[str, Any]],
    *,
    limit: int,
    read: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ready: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Read together and publish each verified window to the caller's timeout fallback."""
    candidates: dict[tuple[str, int], dict[str, Any]] = {}
    for edge in (*preferred, *backups):
        candidates.setdefault((edge["path"], edge["line"]), edge)
    completed: list[dict[str, Any] | None] = [None] * len(candidates)

    async def capture(index: int, edge: dict[str, Any]) -> None:
        example = await read(edge)
        if "context" in example:
            completed[index] = example
            if ready is not None:
                ready[:] = [row for row in completed if row is not None][:limit]

    await asyncio.gather(*(capture(i, edge) for i, edge in enumerate(candidates.values())))
    return [row for row in completed if row is not None][:limit]


def _test_name_words(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", snake(name))) - {"test", "tests"}


def _test_case(site: dict[str, Any]) -> tuple[str, str]:
    """Group a Python test's local route handlers under their enclosing test."""
    caller = site["from_qualified_name"]
    path = site["path"]
    if path.endswith(".py"):
        segments = caller.split("::")
        for i, name in enumerate(segments[1:], start=1):
            if name.startswith("test_"):
                return path, "::".join(segments[: i + 1])
    return path, caller


def _rank_test_sites(
    sites: list[dict[str, Any]],
    *,
    related_names: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Put related test functions first, with one source window per test before repeats."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    related_words = {word for name in related_names for word in _test_name_words(name)}
    for site in sites:
        counts[_test_case(site)] += 1

    def key(site: dict[str, Any]) -> tuple[bool, bool, int, int]:
        path, caller = _test_case(site)
        name = caller.rsplit("::", 1)[-1]
        _, *target = str(site.get("to_qualified_name") or "").split("::")
        target_words = _test_name_words("_".join(target[-2:]))
        words = _test_name_words(name)
        related = words & target_words
        shared = bool(related and ((related_words - target_words) & words))
        return not snake(name).startswith("test_"), not shared, -len(related), -counts[path, caller]

    picked: dict[tuple[str, str], dict[str, Any]] = {}
    repeats = []
    for site in sorted(sites, key=key):
        caller = _test_case(site)
        if caller in picked:
            repeats.append(site)
        else:
            picked[caller] = site
    return list(picked.values()) + repeats


async def indirect_test_examples(
    repo: CodeRepo,
    org: str,
    ns: str,
    symbol_ids: list[str],
    definition: dict[str, Any],
    revision: str,
    context_lines: int,
    *,
    ready: list[dict[str, Any]] | None = None,
    related_names: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Same-revision test source for a verified non-test caller."""
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
        caller_revision=revision,
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
    verified = _verified_indirect_examples(
        repo,
        org,
        ns,
        bridges,
        str(definition["path"]),
        revision,
        context_lines,
        ready,
        related_names=related_names,
    )
    aliases = list(bridges.values())
    bridge = aliases[0]
    # Keep every ID for incoming test edges. Both tiers can index one call site.
    if len(aliases) > 1 and (
        len(aliases) != 2
        or {edge["tier"] for edge in aliases} != {"treesitter", "scip"}
        or _via_caller(bridge) != _via_caller(aliases[1])
    ):
        return await verified
    if lang_for_path(str(bridge["path"])) != "python":
        return await verified
    reference_lookup = bounded(
        _repeated_test_reference(
            repo, org, ns, bridge, str(definition["path"]), revision, context_lines
        ),
        None,
    )
    examples: list[dict[str, Any]]
    reference: dict[str, Any] | None
    examples, reference = await asyncio.gather(verified, reference_lookup)
    if reference and all(
        (example["path"], example["caller"]) != (reference["path"], reference["caller"])
        for example in examples
    ):
        examples.append(reference)
    if ready is not None:
        ready[:] = examples
    if bridge["path"] != definition["path"]:
        await bounded(
            _attach_caller_source(repo, org, ns, bridge, revision, examples, context_lines), None
        )
    return examples


async def _attach_caller_source(
    repo: CodeRepo,
    org: str,
    ns: str,
    bridge: dict[str, Any],
    revision: str,
    examples: list[dict[str, Any]],
    context_lines: int,
) -> None:
    """Return the verified caller when its source fits a test example's window."""
    from server.code.app.symbol.resolve import _indexed_commit, symbol_body

    via = _via_caller(bridge)
    example = next(
        (
            site
            for site in examples
            if site.get("evidence") == "indirect_call"
            and site.get("via") == via
            and (site.get("context") or {}).get("commit") == revision
        ),
        None,
    )
    if example is None:
        return
    path = via["path"]
    with code_span("enrich.indirect_caller_source", results=0) as sp:
        rows, files = await asyncio.gather(
            repo.symbols_by_ids(org, ns, [str(bridge["from_symbol_id"])]),
            repo.file_rows_for_paths(org, ns, [path]),
        )
        if _indexed_commit(files.get(path, {}).get("indexed_commit")) != revision:
            return
        callers = [
            row
            for row in rows
            if row["path"] == path
            and row["qualified_name"] == via["symbol"]
            and row["kind"] in {"function", "method"}
            and row["start_line"] <= via["line"] <= row["end_line"]
            and _indexed_commit(row.get("extent_commit") or row.get("indexed_commit")) == revision
        ]
        if len(callers) != 1:
            return
        (caller,) = callers
        if caller["end_line"] - caller["start_line"] > 2 * context_lines:
            return  # a larger caller remains available from its exact source location
        body = await symbol_body(
            repo, org, ns, {**caller, "_extent_commit": revision}, expected_commit=revision
        )
        fits = bool(body) and len(body) <= settings.code_chunk_max_chars
        span_attrs(sp, results=int(fits))
    if fits:
        example["via"] = {
            **via,
            "context": {
                "start_line": caller["start_line"],
                "end_line": caller["end_line"],
                "commit": revision,
                "text": body,
            },
        }


def merge_indirect_caller_sources(target: CodeUnit, repeated: CodeUnit) -> None:
    """Keep verified optional caller source from either spelling of a definition."""
    for old, new in zip(target.examples, repeated.examples, strict=True):
        old_via, new_via = old.get("via") or {}, new.get("via") or {}
        if new_via.get("context") and not old_via.get("context"):
            old["via"] = new_via


def deduplicate_indirect_caller_sources(definitions: list[CodeUnit]) -> None:
    """Keep the caller body once, with its definition or with its first test."""
    covered = {
        (unit.path, unit.symbol, unit.start_line, unit.end_line, unit.body)
        for unit in definitions
        if unit.body and not unit.elided and unit.granularity == "unit"
    }
    attached = set()
    for unit in definitions:
        for example in unit.examples:
            via = example.get("via") or {}
            if context := via.get("context"):
                key = (via["path"], via["symbol"])
                start, end = context["start_line"], context["end_line"]
                tag = (*key, context["commit"], start, end)
                if tag in attached or (*key, start, end, context["text"]) in covered:
                    example["via"] = {
                        field: value for field, value in via.items() if field != "context"
                    }
                attached.add(tag)


async def _verified_indirect_examples(
    repo: CodeRepo,
    org: str,
    ns: str,
    bridges: dict[str, dict[str, Any]],
    path: str,
    revision: str,
    context_lines: int,
    ready: list[dict[str, Any]] | None,
    *,
    related_names: tuple[str, ...],
) -> list[dict[str, Any]]:
    caller_limit = settings.code_enrich_indirect_test_caller_limit
    tests = await repo.edges_for_symbols(
        org,
        ns,
        list(bridges),
        direction="callers",
        limit=2 * caller_limit,
        min_confidence=0.8,
        with_revision=True,
        tests_only=True,
        caller_revision=revision,
        sites_per_caller=2,
    )
    if not tests:
        return []
    files = await repo.file_rows_for_paths(
        org, ns, [path, *(str(e["path"]) for e in (*bridges.values(), *tests))]
    )
    if files.get(path, {}).get("indexed_commit") != revision:
        return []
    sites = _verified_indirect_sites(tests, bridges, files, revision)
    ranked = _rank_test_sites(sites, related_names=related_names)
    candidates = _prefer_distinct_test_windows(ranked, context_lines)[:6]
    by_caller: dict[str, dict[str, Any]] = {}
    for edge in tests:
        by_caller.setdefault(str(edge["from_symbol_id"]), edge)
    original = _verified_indirect_sites(
        list(by_caller.values())[:caller_limit], bridges, files, revision
    )
    backups = _prefer_distinct_test_windows(original, context_lines)[:6]
    model = served_model_label(settings.embedding_model)

    async def read(edge: dict[str, Any]) -> dict[str, Any]:
        bridge = bridges[str(edge["to_symbol_id"])]
        site = {
            "caller": str(edge["from_qualified_name"]),
            "path": str(edge["path"]),
            "line": int(edge["line"]),
            "tier": str(edge["tier"]),
            "evidence": "indirect_call",
            "via": _via_caller(bridge),
        }
        return await _example_context(repo, org, ns, site, context_lines, model, revision)

    return await _retained_examples(candidates, backups, limit=3, read=read, ready=ready)


def _via_caller(bridge: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(bridge["from_qualified_name"]),
        "path": str(bridge["path"]),
        "line": int(bridge["line"]),
    }


def _related_test_reference_sites(
    rows: list[dict[str, Any]], bare: str, revision: str, radius: int
) -> list[tuple[str, str, set[int], int]]:
    lines: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in rows:
        candidate_path, caller = str(row["path"]), str(row["from_qualified_name"])
        if (
            row["call_text"] == bare
            and row["indexed_commit"] in (revision, revision.encode())
            and caller.rsplit("::", 1)[-1].startswith("test_")
            and int(row["line"]) > 0
        ):
            lines[candidate_path, caller].add(int(row["line"]))
    ranked = []
    for (candidate_path, caller), locations in lines.items():
        for index, anchor in enumerate(_distinct_call_windows(locations, radius)):
            ranked.append((index, candidate_path, anchor, caller, locations))
    return [
        (path, caller, locations, anchor)
        for _, path, anchor, caller, locations in sorted(ranked)[:6]
    ]


def _distinct_call_windows(locations: set[int], radius: int) -> list[int]:
    anchors: list[int] = []
    for first, second in pairwise(sorted(locations)):
        if second - first <= radius and (not anchors or second - anchors[-1] > radius):
            anchors.append(first)
    return anchors


def _repeated_argument_sites(
    context: dict[str, Any], positions: set[int], pattern: re.Pattern[str]
) -> list[list[int]]:
    calls: dict[tuple[str, str], list[int]] = defaultdict(list)
    for line, text in enumerate(context["text"].splitlines(), context["start_line"]):
        match = pattern.match(text) if line in positions else None
        if match:
            receiver, argument = match.group(1), match.group(2)
            calls[receiver, argument].append(line)
    return [lines for lines in calls.values() if len(lines) > 1]


async def _repeated_test_reference(
    repo: CodeRepo,
    org: str,
    ns: str,
    bridge: dict[str, Any],
    path: str,
    revision: str,
    radius: int,
) -> dict[str, Any] | None:
    """Show repeated source calls through a wrapper name the resolver could not bind."""
    _, _, bare = str(bridge["from_qualified_name"]).rpartition("::")
    if not bare.isidentifier():
        return None
    siblings = {*sibling_test_paths(path), f"tests/test_{PurePosixPath(path).stem}.py"}
    test_paths = sorted(path for path in siblings if is_test_unit_path(path))
    with code_span("enrich.indirect_test_reference"):
        rows = await repo.live_call_misses_for_names(org, ns, [bare], paths=test_paths)
        sites = _related_test_reference_sites(rows, bare, revision, radius)
        if not sites:
            return None
        files = await repo.file_rows_for_paths(
            org, ns, [path, str(bridge["path"]), *(site[0] for site in sites)]
        )
        if any(files.get(p, {}).get("indexed_commit") != revision for p in (path, bridge["path"])):
            return None
        return await _read_repeated_test_reference(
            repo, org, ns, bridge, files, sites, bare, revision, radius
        )


async def _read_repeated_test_reference(
    repo: CodeRepo,
    org: str,
    ns: str,
    bridge: dict[str, Any],
    files: dict[str, dict[str, Any]],
    sites: list[tuple[str, str, set[int], int]],
    bare: str,
    revision: str,
    radius: int,
) -> dict[str, Any] | None:
    pattern = re.compile(rf"\s*(\w+(?:\.\w+)*)\.{re.escape(bare)}\(\s*(\w+)\s*[,)]")
    model = served_model_label(settings.embedding_model)

    async def read(
        path: str, caller: str, lines: set[int], anchor: int
    ) -> tuple[dict[str, Any], list[list[int]]] | None:
        file = files.get(path, {})
        if file.get("indexed_commit") != revision or max(lines) > file.get("loc", 0):
            return None
        site = {"path": path, "caller": caller, "line": anchor}
        example = await _example_context(repo, org, ns, site, radius, model, revision)
        context = example.get("context")
        groups = _repeated_argument_sites(context, lines, pattern) if context else []
        return (example, groups) if groups else None

    candidates = [
        result for result in await asyncio.gather(*(read(*site) for site in sites)) if result
    ]
    locations = sorted(
        {
            (example["path"], line)
            for example, groups in candidates
            for lines in groups
            for line in lines
        }
    )
    contradicted = {
        (edge["path"], edge["line"])
        for edge in await repo.member_edges_at_locations(org, ns, locations, bare)
        if edge["tier"] == "scip"
        and edge["indexed_commit"] in (revision, revision.encode())
        and edge["to_qualified_name"] != bridge["from_qualified_name"]
    }
    for example, groups in candidates:
        for lines in groups:
            available = [line for line in lines if (example["path"], line) not in contradicted]
            if len(available) >= 2:
                return {
                    **example,
                    "line": available[0],
                    "tier": "treesitter",
                    "evidence": "name_reference",
                    "via": _via_caller(bridge),
                }
    return None


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
        by_location.setdefault((path, line), edge)
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
    fresh_at_commit: str = "",
    seconds: float | None = None,
) -> list[dict[str, Any]] | None:
    """The memories linked to the symbol, from the caller's lesson scope: the readable
    `extra_namespaces` the call names (over MCP its memory home) and the repository's
    team namespace.
    Each carries `code_state`: `changed` when this branch's definition differs from the
    one the memory was written against. None when the read failed or ran past its
    bound, so the answer can say lessons went unread rather than that there are none."""
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
            fresh_at_commit=fresh_at_commit,
        )

    return await bounded(read(), None, seconds)
