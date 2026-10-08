"""The key-diff writes of a file's ingest: a stage's derived rows against the file's
live rows, inserting what is new, re-emitting what moved and tombstoning what is gone."""

from __future__ import annotations

import logging
from typing import Any

from server.code.app.index.ingest_edges import _derive_edges, _with_deep_recursion
from server.code.app.index.ingest_stats import FileIngestStats
from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_sql_dialect import CONF_FOLDER, FACET_DIALECT
from server.code.domain.extract.lang_sql_model import SQL_LANG, Facet
from server.code.domain.extract.treesitter import ParsedFile
from server.code.store.index import CodeRepo, build_row

logger = logging.getLogger("loom.code_index.ingest")


def _same_symbol_row(row: dict[str, Any], s: Any) -> bool:
    """Is the live symbol row still exact for this parse? Same kind AND same
    span AND same signature — keeping a row on key alone would leave spans
    stale whenever an edit above the symbol shifted its lines."""
    return (
        str(row.get("kind")) == s.kind
        and int(row.get("start_line") or 0) == int(s.start_line)
        and int(row.get("end_line") or 0) == int(s.end_line)
        and str(row.get("signature") or "") == str(s.signature or "")
        # A return type the parser now states re-versions the row; a row read without
        # the column (None) counts as unchanged
        and (
            row.get("return_type") is None
            or str(row.get("return_type") or "") == str(getattr(s, "return_type", "") or "")
        )
    )


async def _reconcile_symbols(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    path: str,
    commit_sha: str,
    stats: FileIngestStats,
    *,
    run_symbol: bool,
) -> bool:
    """The symbol stage of `ingest_file`: key-diff the parse's symbols
    against the live parser-tier rows (kept when kind, span and signature
    match; rewritten when moved; tombstoned when gone). Returns whether
    anything was written."""
    if not run_symbol:
        return False
    existing_syms = await repo.live_symbols_for_path(org, ns, path, "treesitter")
    existing_sym_keys = {(str(r["qualified_name"]), str(r["symbol_id"])): r for r in existing_syms}
    new_sym_keys = set()
    sym_rows = []
    for s in pf.symbols:
        key = (s.qualified_name, s.id)
        if key in new_sym_keys:
            continue
        new_sym_keys.add(key)
        existing_row = existing_sym_keys.get(key)
        if existing_row is not None and _same_symbol_row(existing_row, s):
            continue  # unchanged symbol — keep the live row
        if existing_row is not None:
            # key matches but the span, signature or kind changed (function
            # moved by an edit above it; parser upgrade): rewrite the row.
            stats.symbols_moved += 1
        else:
            stats.symbol_names_added.append(s.name)
        sym_rows.append(
            build_row(
                "code_symbols",
                org=org,
                namespace=ns,
                tier="treesitter",
                qualified_name=s.qualified_name,
                symbol_id=s.id,
                name=s.name,
                kind=s.kind,
                signature=s.signature,
                return_type=s.return_type,
                path=path,
                start_line=s.start_line,
                end_line=s.end_line,
                doc_comment=s.doc_comment,
                indexed_commit=commit_sha,
            )
        )
    sym_tombstones = [
        CodeRepo.tombstone_of("code_symbols", row)
        for key, row in existing_sym_keys.items()
        if key not in new_sym_keys
    ]
    stats.symbols_new = len(sym_rows)
    stats.symbols_tombstoned = len(sym_tombstones)
    for key, row in existing_sym_keys.items():
        if key not in new_sym_keys:
            stats.symbol_names_removed.append(str(row.get("name") or key[0].split("::")[-1]))
            stats.symbols_tombstoned_ids.append(key[1])
    stats.symbol_summaries = [
        {
            "qualified_name": s.qualified_name,
            "signature": s.signature,
            "symbol_id": s.id,
            "path": path,
            "kind": s.kind,
        }
        for s in pf.symbols
    ]
    await repo.insert_rows("code_symbols", sym_rows + sym_tombstones)
    return bool(sym_rows or sym_tombstones)


async def _reconcile_facets(
    repo: CodeRepo, org: str, ns: str, path: str, facets: list[Facet], commit_sha: str
) -> bool:
    """Key-diff one file's facet rows against the live ones: a (kind, name, ordinal) the
    parse still yields on the same line is kept, a moved or new one is written, a
    vanished one is tombstoned. Returns whether anything was written."""
    live = await repo.live_facets_for_path(org, ns, path)
    live_by_key = {(str(r["facet_kind"]), str(r["name"]), int(r["ordinal"])): r for r in live}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for f in facets:
        key = (f.kind, f.name, f.ordinal)
        if key in seen:
            continue
        seen.add(key)
        row = live_by_key.get(key)
        if (
            row is not None
            and int(row["line"]) == f.line
            and abs(float(row["confidence"]) - f.confidence) < 1e-6
        ):
            continue
        rows.append(
            build_row(
                "code_test_facets",
                org=org,
                namespace=ns,
                facet_kind=f.kind,
                name=f.name,
                path=path,
                ordinal=f.ordinal,
                line=f.line,
                confidence=f.confidence,
                indexed_commit=commit_sha,
            )
        )
    parse_decided = any(f.kind == FACET_DIALECT for f in facets)
    rows.extend(
        CodeRepo.tombstone_of("code_test_facets", row)
        for key, row in live_by_key.items()
        if key not in seen and not _folder_inherited(key, row, parse_decided)
    )
    if rows:
        await repo.insert_rows("code_test_facets", rows)
    return bool(rows)


def _folder_inherited(key: tuple[str, str, int], row: dict[str, Any], parse_decided: bool) -> bool:
    """A file-level `dialect` row at the folder confidence was written by `loom code
    backfill --dialect-folder-pass`, not by this parse — the key-diff keeps it unless
    the parse now decides the dialect itself (then the parse's row replaces it)."""
    return (
        key[0] == FACET_DIALECT
        and key[2] == 0
        and not parse_decided
        and abs(float(row["confidence"]) - CONF_FOLDER) < 1e-6
    )


async def _reconcile_edges_and_misses(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    path: str,
    commit_sha: str,
    stats: FileIngestStats,
    *,
    write_guards: bool = True,
) -> list[Edge]:
    """Derive this parse's edges and declined sites against the CURRENT name
    table and key-diff them against the live rows: unchanged keys are kept
    (never re-inserted), new ones inserted, vanished ones tombstoned. Shared
    by the full ingest and the edges-only dependents path; returns the edges
    so the guard stage can derive its records from them. `write_guards`
    keeps the guard diff here (the dependents path, which re-resolves edges
    and so re-derives their guards); `ingest_file` runs the guard stage
    itself, scoped by the heal."""
    # ---- edges -------------------------------------------------------------
    edges, misses = await _derive_edges(repo, org, ns, pf, path, stats)

    existing_edges = await repo.live_edges_for_path(org, ns, path, "treesitter")

    def edge_key(r: dict[str, Any]) -> tuple[str, str, str, str, int]:
        return (
            str(r["edge_kind"]),
            str(r["from_symbol_id"]),
            str(r["to_symbol_id"]),
            str(r["path"]),
            int(r["line"]),
        )

    existing_edge_keys = {edge_key(r): r for r in existing_edges}
    new_edge_keys: set[tuple[str, str, str, str, int]] = set()
    edge_rows = []
    for e in edges:
        ekey = (e.edge_kind, e.from_symbol_id, e.to_symbol_id, e.path, e.line)
        new_edge_keys.add(ekey)
        if ekey in existing_edge_keys:
            continue
        edge_rows.append(
            build_row(
                "code_edges",
                org=org,
                namespace=ns,
                tier="treesitter",
                edge_kind=e.edge_kind,
                to_symbol_id=e.to_symbol_id,
                from_symbol_id=e.from_symbol_id,
                path=e.path,
                line=e.line,
                from_qualified_name=e.from_qualified_name,
                to_qualified_name=e.to_qualified_name,
                confidence=e.confidence,
                indexed_commit=commit_sha,
            )
        )
    edge_tombstones = [
        CodeRepo.tombstone_of("code_edges", row)
        for ekey, row in existing_edge_keys.items()
        if ekey not in new_edge_keys
    ]
    stats.edges_new = len(edge_rows)
    stats.edges_tombstoned = len(edge_tombstones)
    stats.callee_symbol_ids = sorted(
        {e.to_symbol_id for e in edges if e.to_symbol_id}
        | {str(r["to_symbol_id"]) for r in edge_tombstones if r.get("to_symbol_id")}
    )
    await repo.insert_rows("code_edges", edge_rows + edge_tombstones)

    if write_guards:
        await _maybe_write_call_guards(repo, org, ns, pf, path, commit_sha, edges, stats)

    # ---- declined call sites: same per-path tombstone diff
    # as edges, so a re-ingest that resolves a former miss retires its row.
    try:
        # A SQL file never records a declined call site (a table nobody creates in the
        # tree is the norm, not a miss), so its live rows are known empty — skip the
        # per-path read, about 1.4 MB a file
        existing_misses = (
            [] if pf.lang == SQL_LANG else await repo.live_call_misses_for_path(org, ns, path)
        )

        def miss_key(r: dict[str, Any]) -> tuple[str, str, int, str]:
            return (str(r["callee_name"]), str(r["path"]), int(r["line"]), str(r["from_symbol_id"]))

        existing_miss_keys = {miss_key(r): r for r in existing_misses}
        new_miss_keys: set[tuple[str, str, int, str]] = set()
        miss_rows = []
        refreshed = 0
        for m in misses:
            mkey = (m.callee_name, m.path, m.line, m.from_symbol_id)
            new_miss_keys.add(mkey)
            live = existing_miss_keys.get(mkey)
            if live is not None:
                # The key is the site; the verdict is not. A definition that appeared or
                # vanished in ANOTHER file flips `unresolved` <-> `ambiguous` or moves
                # the candidate count, and the dependents pass re-derives this path
                # exactly to refresh it — same ReplacingMergeTree key, newer version, no
                # tombstone.
                cands = min(m.candidates, 65535)
                if (
                    str(live.get("disposition") or "") != m.disposition
                    or int(live.get("candidates") or 0) != cands
                ):
                    miss_rows.append(
                        CodeRepo.moved_of(
                            "code_call_sites_unresolved",
                            live,
                            disposition=m.disposition,
                            candidates=cands,
                            indexed_commit=commit_sha,
                        )
                    )
                    refreshed += 1
                continue
            miss_rows.append(
                build_row(
                    "code_call_sites_unresolved",
                    org=org,
                    namespace=ns,
                    callee_name=m.callee_name,
                    path=m.path,
                    line=m.line,
                    from_symbol_id=m.from_symbol_id,
                    from_qualified_name=m.from_qualified_name,
                    call_text=m.call_text,
                    disposition=m.disposition,
                    candidates=min(m.candidates, 65535),
                    indexed_commit=commit_sha,
                )
            )
        miss_tombstones = [
            CodeRepo.tombstone_of("code_call_sites_unresolved", row)
            for mkey, row in existing_miss_keys.items()
            if mkey not in new_miss_keys
        ]
        stats.misses_new = len(miss_rows) - refreshed
        stats.misses_refreshed = refreshed
        stats.misses_tombstoned = len(miss_tombstones)
        await repo.insert_rows("code_call_sites_unresolved", miss_rows + miss_tombstones)
    except Exception as miss_exc:  # noqa: BLE001 — a disclosure table must never fail an ingest
        logger.warning("code ingest: declined-call rows failed for %s: %s", path, miss_exc)
    return edges


async def _maybe_write_call_guards(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    path: str,
    commit_sha: str,
    edges: list[Edge],
    stats: FileIngestStats,
) -> None:
    """The conditions that guard each caller's call sites: same per-path tombstone diff as the
    edges, written from the SAME pass so a guard row never outlives or predates its edges, and
    never a reason an ingest fails."""
    try:
        await _with_deep_recursion(
            lambda: _reconcile_call_guards(repo, org, ns, pf, path, commit_sha, edges, stats)
        )
    except Exception as guard_exc:  # noqa: BLE001 — a disclosure table must never fail an ingest
        logger.warning("code ingest: call-guard rows failed for %s: %s", path, guard_exc)


async def _reconcile_call_guards(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    path: str,
    commit_sha: str,
    edges: list[Edge],
    stats: FileIngestStats,
) -> None:
    """Derive the caller-guard records of this parse's call edges and key-diff them
    against the live rows of the path: a caller whose record is unchanged is kept, one
    whose guards or sites changed is re-emitted under its key (newer version, no
    tombstone), new callers inserted, vanished ones tombstoned. A caller none of whose
    calls is conditional has no record and so no row — the table is sparse."""
    from server.code.domain.extract.call_guards import extract_call_guards

    records = extract_call_guards(pf, edges)
    existing = await repo.live_call_guards_for_path(org, ns, path)

    def gkey(r: dict[str, Any]) -> tuple[str, str]:
        return (str(r["from_symbol_id"]), str(r["path"]))

    def content(r: dict[str, Any]) -> tuple[Any, ...]:
        return (
            [str(x) for x in r.get("guards") or []],
            [str(x) for x in r.get("guard_kinds") or []],
            [int(x) for x in r.get("sites") or []],
            [int(x) for x in r.get("site_callees") or []],
            [[int(i) for i in x] for x in r.get("site_guards") or []],
        )

    existing_keys = {gkey(r): r for r in existing}
    new_keys: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for g in records:
        key = (g.from_symbol_id, g.path)
        new_keys.add(key)
        fields = {
            "guards": list(g.guards),
            "guard_kinds": list(g.guard_kinds),
            "sites": list(g.sites),
            "site_callees": list(g.site_callees),
            "site_guards": [list(x) for x in g.site_guards],
        }
        live = existing_keys.get(key)
        if live is not None:
            if content(live) == content(fields):
                continue
            rows.append(
                CodeRepo.moved_of(
                    "code_caller_guards",
                    live,
                    indexed_commit=commit_sha,
                    **fields,
                )
            )
            continue
        rows.append(
            build_row(
                "code_caller_guards",
                org=org,
                namespace=ns,
                from_symbol_id=g.from_symbol_id,
                path=g.path,
                indexed_commit=commit_sha,
                **fields,
            )
        )
    tombstones = [
        CodeRepo.tombstone_of("code_caller_guards", row)
        for key, row in existing_keys.items()
        if key not in new_keys
    ]
    stats.guards_new = len(rows)
    stats.guards_tombstoned = len(tombstones)
    await repo.insert_rows("code_caller_guards", rows + tombstones)
