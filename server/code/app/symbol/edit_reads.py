"""Verified source behind an edit target's indexed member references."""

from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict
from typing import Any

from server.code.api.models_common import _safe_path
from server.code.api.models_symbol import (
    CodeEditContextTarget,
    CodeEditReadSite,
    CodeEditReadSource,
)
from server.code.app.enrich import bounded
from server.code.app.index.denylist import under_prefixes
from server.code.app.spans import code_span, span_attrs
from server.code.app.symbol.resolve import _indexed_commit, symbol_body
from server.code.domain.extract.treesitter import lang_for_path
from server.code.domain.index.repos import repo_for_namespace
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.settings import settings

_READ_CANDIDATES = 200
_AnchorKey = tuple[str, str]
_TargetKey = tuple[str, str, str]  # id, qualified name, and indexed revision


async def batch_read_sources(
    org: str,
    ns: str,
    definitions: list[CodeUnit],
    targets: list[CodeEditContextTarget],
) -> list[CodeEditReadSource]:
    """Only resolved targets in a batch can supply member-read anchors."""
    resolved = {
        index
        for target in targets
        if target.status == "resolved"
        for index in target.definition_indices
    }
    return await read_sources_for_definitions(org, ns, definitions, resolved)


async def read_sources_for_definitions(
    org: str,
    ns: str,
    definitions: list[CodeUnit],
    resolved: set[int],
) -> list[CodeEditReadSource]:
    with code_span("edit_context.read_sources") as span:
        sources = await edit_read_sources(CodeRepo(), org, ns, definitions, resolved)
        span_attrs(span, results=len(sources))
    return sources


async def edit_read_sources(
    repo: CodeRepo,
    org: str,
    ns: str,
    definitions: list[CodeUnit],
    resolved: set[int],
) -> list[CodeEditReadSource]:
    """Keep requested definitions and cite verified indexed reads and one forwarder."""
    until = time.monotonic() + settings.code_enrich_timeout_s
    anchors = {
        (unit.path, unit.symbol): unit
        for index, unit in enumerate(definitions)
        if (
            index in resolved
            and unit.unit_id
            and unit.body
            and not unit.elided
            and unit.granularity == "unit"
            and unit.kind == "symbol"
            and lang_for_path(unit.path) == "python"
        )
    }
    if not anchors:
        return []
    names = list(dict.fromkeys(symbol for _, symbol in anchors))
    ids_by_name = await bounded(
        repo.symbol_ids_for_names(org, ns, names), {}, seconds=max(0.0, until - time.monotonic())
    )
    ids = sorted({sid for values in ids_by_name.values() for sid in values})
    if not ids or len(ids) > 500:
        return []
    remaining = max(0.0, until - time.monotonic())
    reads, forwarding = await asyncio.gather(
        bounded(_member_read_sources(repo, org, ns, definitions, anchors, ids), [], remaining),
        bounded(_forward_source(repo, org, ns, definitions, anchors, ids_by_name), [], remaining),
    )
    return _append_forward_source(reads, forwarding)


async def _member_read_sources(
    repo: CodeRepo,
    org: str,
    ns: str,
    definitions: list[CodeUnit],
    anchors: dict[_AnchorKey, CodeUnit],
    ids: list[str],
) -> list[CodeEditReadSource]:
    edges = await repo.edges_for_symbols(
        org,
        ns,
        ids,
        direction="callees",
        edge_kinds=["reads"],
        limit=_READ_CANDIDATES,
        min_confidence=0.8,
        with_revision=True,
    )
    if len(edges) >= _READ_CANDIDATES:
        return []  # the read did not establish whether a target is unique
    candidates = _read_candidates(anchors, edges)
    if not candidates:
        return []
    rows = await repo.symbols_by_ids(org, ns, sorted({key[0] for key in candidates}))
    destinations = {
        (row["symbol_id"], row["qualified_name"], _revision(row)): row
        for row in rows
        if row["kind"] in {"function", "method"} and not _already_served(row, definitions)
    }
    groups = {
        key: [site for site in sites if site.path != destinations[key]["path"]]
        for key, sites in candidates.items()
        if key in destinations
    }
    if not any(groups.values()):
        return []
    return await _hydrate_read_sources(repo, org, ns, anchors, destinations, groups)


def _one_forwarder(
    anchors: dict[_AnchorKey, CodeUnit],
) -> tuple[CodeUnit, int, str, str] | None:
    from server.code.app.grep_dependencies import typed_forward_call

    witness = None
    for unit in anchors.values():
        if len(unit.body) > settings.code_chunk_max_chars:
            continue
        name = unit.symbol.rsplit("::", 1)[-1]
        if call := typed_forward_call(unit.body, name):
            if witness is not None:
                return None
            offset, owner, member = call
            witness = unit, unit.start_line + offset - 1, owner, member
    return witness


async def _forward_source(
    repo: CodeRepo,
    org: str,
    ns: str,
    definitions: list[CodeUnit],
    anchors: dict[_AnchorKey, CodeUnit],
    ids_by_name: dict[str, list[str]],
) -> list[CodeEditReadSource]:
    from server.code.app.grep_dependencies import _helper_edges, _one_target

    witness = await asyncio.to_thread(_one_forwarder, anchors)
    if witness is None:
        return []
    unit, line, owner, member = witness
    ids = ids_by_name.get(unit.symbol, [])
    if not ids:
        return []
    edges = await _helper_edges(repo, org, ns, sorted(set(ids)))
    site_edges = [
        edge
        for edge in edges
        if (edge["path"], edge["from_qualified_name"], edge["line"])
        == (unit.path, unit.symbol, line)
    ]
    edge = _one_target(site_edges)
    if edge is None:
        return []
    name = edge["to_qualified_name"]
    revision = _indexed_commit(edge["indexed_commit"])
    if not revision or name.rsplit("::", 2)[-2:] != [owner, member]:
        return []
    rows = await repo.symbols_by_ids(org, ns, [edge["to_symbol_id"]])
    if len(rows) != 1 or not _safe_forward_destination(org, ns, unit, rows[0], name, revision):
        return []
    row = rows[0]
    if _already_served(row, definitions):
        return []
    key = edge["to_symbol_id"], name, revision
    site = CodeEditReadSite(path=unit.path, symbol=unit.symbol, line=line)
    return await _hydrate_read_sources(repo, org, ns, anchors, {key: row}, {key: [site]})


def _safe_forward_destination(
    org: str, ns: str, unit: CodeUnit, row: dict[str, Any], name: str, revision: str
) -> bool:
    cfg = repo_for_namespace(org, ns)
    path = row["path"]
    return bool(
        cfg
        and path != unit.path
        and _safe_path(path)
        and _safe_path(unit.path)
        and lang_for_path(path) == "python"
        and under_prefixes(cfg.path_prefixes, path)
        and under_prefixes(cfg.path_prefixes, unit.path)
        and row["kind"] in {"function", "method"}
        and row["qualified_name"] == name
        and _revision(row) == revision
    )


def _append_forward_source(
    reads: list[CodeEditReadSource], forwarding: list[CodeEditReadSource]
) -> list[CodeEditReadSource]:
    if not forwarding:
        return reads
    (target,) = forwarding
    key = target.path, target.qualified_name, target.commit
    if any((row.path, row.qualified_name, row.commit) == key for row in reads):
        return reads
    all_sources = [*reads, target]
    return (
        all_sources
        if sum(len(row.model_dump_json()) for row in all_sources) <= settings.code_chunk_max_chars
        else reads
    )


async def _hydrate_read_sources(
    repo: CodeRepo,
    org: str,
    ns: str,
    anchors: dict[_AnchorKey, CodeUnit],
    destinations: dict[_TargetKey, dict[str, Any]],
    groups: dict[_TargetKey, list[CodeEditReadSite]],
) -> list[CodeEditReadSource]:
    files = await repo.file_rows_for_paths(
        org, ns, [*(path for path, _ in anchors), *(destinations[key]["path"] for key in groups)]
    )
    live_revisions = {
        path: _indexed_commit(row.get("indexed_commit")) for path, row in files.items()
    }
    live_groups: dict[_TargetKey, list[CodeEditReadSite]] = {}
    required_anchors: dict[_AnchorKey, tuple[CodeUnit, str]] = {}
    for key, sites in groups.items():
        revision = key[2]
        if live_revisions.get(destinations[key]["path"]) != revision:
            continue
        current = [site for site in sites if live_revisions.get(site.path) == revision]
        if current:
            live_groups[key] = current
            for site in current:
                identity = site.path, site.symbol
                required_anchors[identity] = anchors[identity], revision

    async def read_member(key: _TargetKey) -> str:
        return await symbol_body(
            repo, org, ns, {**destinations[key], "_extent_commit": key[2]}, expected_commit=key[2]
        )

    checked, bodies = await asyncio.gather(
        _verified_anchors(repo, org, ns, required_anchors),
        asyncio.gather(*(read_member(key) for key in live_groups)),
    )
    remaining = settings.code_chunk_max_chars
    sources = []
    for (key, sites), body in zip(live_groups.items(), bodies, strict=True):
        proven = [site for site in sites if (site.path, site.symbol) in checked]
        if not body or len(body) > remaining or not proven:
            continue
        related = destinations[key]
        row = CodeEditReadSource(
            qualified_name=related["qualified_name"],
            path=related["path"],
            start_line=related["start_line"],
            end_line=related["end_line"],
            commit=key[2],
            body=body,
            sites=proven,
        )
        size = len(row.model_dump_json())
        if size <= remaining:
            sources.append(row)
            remaining -= size
    return sources


def _revision(row: dict[str, Any]) -> str:
    return _indexed_commit(row.get("extent_commit") or row.get("indexed_commit"))


def _already_served(row: dict[str, Any], definitions: list[CodeUnit]) -> bool:
    return any(
        unit.path == row["path"]
        and unit.body
        and not unit.elided
        and unit.granularity == "unit"
        and unit.start_line <= row["start_line"] <= row["end_line"] <= unit.end_line
        for unit in definitions
    )


def _read_candidates(
    anchors: dict[_AnchorKey, CodeUnit], edges: list[dict[str, Any]]
) -> dict[_TargetKey, list[CodeEditReadSite]]:
    grouped: dict[_AnchorKey, dict[_TargetKey, set[int]]] = defaultdict(lambda: defaultdict(set))
    for edge in edges:
        anchor = edge["path"], edge["from_qualified_name"]
        unit = anchors.get(anchor)
        name = str(edge["to_qualified_name"])
        revision = _indexed_commit(edge["indexed_commit"])
        line = int(edge["line"])
        if (
            unit is not None
            and edge["tier"] == "treesitter"
            and revision
            and name.count("::") >= 2
            and unit.start_line <= line <= unit.end_line
        ):
            grouped[anchor][edge["to_symbol_id"], name, revision].add(line)
    out: dict[_TargetKey, list[CodeEditReadSite]] = defaultdict(list)
    selected = {next(iter(targets)) for targets in grouped.values() if len(targets) == 1}
    for (path, symbol), targets in grouped.items():
        # A definition with several member reads can cite source already selected for
        # another definition. A line with competing targets carries no citation.
        counts = Counter(line for lines in targets.values() for line in lines)
        for key, lines in targets.items():
            if key in selected:
                out[key].extend(
                    CodeEditReadSite(path=path, symbol=symbol, line=line)
                    for line in sorted(lines)
                    if counts[line] == 1
                )
    return dict(out)


async def _verified_anchors(
    repo: CodeRepo, org: str, ns: str, anchors: dict[_AnchorKey, tuple[CodeUnit, str]]
) -> set[_AnchorKey]:
    async def current(unit: CodeUnit, revision: str) -> bool:
        metadata = {
            "path": unit.path,
            "start_line": unit.start_line,
            "end_line": unit.end_line,
            "qualified_name": unit.symbol,
            "symbol_id": unit.unit_id,
            "_extent_commit": revision,
        }
        body = await symbol_body(repo, org, ns, metadata, expected_commit=revision)
        return body == unit.body

    verified = await asyncio.gather(
        *(current(unit, revision) for unit, revision in anchors.values())
    )
    return {key for key, matches in zip(anchors, verified, strict=True) if matches}
