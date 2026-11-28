"""Source identity checks before applying measured line numbers to indexed code."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from server.code.domain.index import coverage_claim as cc
from server.code.domain.index import coverage_runs as cr
from server.code.store.index import CodeRepo


async def source_identities(
    repo: CodeRepo, org: str, ns: str, paths: list[str]
) -> tuple[str, dict[str, dict[str, Any]]]:
    head, files = await asyncio.gather(
        repo.get_state(org, ns, "git_head"), repo.file_rows_for_paths(org, ns, paths)
    )
    return head, files


def span_identities(
    spans: dict[str, list[tuple[int, int]]] | None,
    commits: dict[str, set[str]] | None,
) -> dict[str, set[str]]:
    """A commit without a usable line extent cannot identify a symbol span."""
    extents = spans or {}
    return {
        path: identities
        for path, identities in (commits or {}).items()
        if extents.get(path) and all(a > 0 and b >= a for a, b in extents[path])
    }


def verified_source(
    path: str,
    commit: str,
    head: str,
    files: dict[str, dict[str, Any]],
    span_commits: dict[str, set[str]] | None = None,
) -> bool:
    """An unchanged file keeps its parsing commit when the repository advances."""
    indexed = str(files.get(path, {}).get("indexed_commit") or "")
    return bool(
        head
        and commit
        and indexed == commit
        and (span_commits is None or span_commits.get(path) == {indexed})
    )


def evidence_commit(evidence: str) -> str:
    try:
        data = json.loads(evidence or "{}")
    except ValueError:
        return ""
    return str(data.get("commit") or "") if isinstance(data, dict) else ""


async def partition_coverage_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    rows: list[dict[str, Any]],
    *,
    span_commits: dict[str, set[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep unverified file associations without interpreting their line numbers."""
    if not rows:
        return [], []
    paths = list(
        dict.fromkeys(str(r.get(key) or "") for r in rows for key in ("src_path", "test_path"))
    )
    head, files = await source_identities(repo, org, ns, paths)
    current, historical = [], []
    for row in rows:
        path = str(row.get("src_path") or "")
        commit = evidence_commit(str(row.get("evidence") or ""))
        test = str(row.get("test_path") or "")
        if verified_source(path, commit, head, files, span_commits) and verified_source(
            test, commit, head, files
        ):
            current.append(row)
        else:
            historical.append(
                {
                    **row,
                    "method": "coverage_history",
                    "evidence": f"historical coverage of {path}; freshness unverified "
                    f"(measurement commit {commit or 'unknown'}, indexed commit "
                    f"{files.get(path, {}).get('indexed_commit') or 'unknown'}, "
                    f"test indexed commit "
                    f"{files.get(test, {}).get('indexed_commit') or 'unknown'})",
                }
            )
    return current, historical


async def measured_for(
    repo: CodeRepo,
    org: str,
    ns: str,
    src_paths: list[str],
    spans: dict[str, list[tuple[int, int]]] | None,
    *,
    span_commits: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Only verified sources support current execution or nonexecution claims."""
    files = await repo.coverage_files_for(org, ns, src_paths) if src_paths else {}
    if not files:
        return {}
    head, indexed = await source_identities(repo, org, ns, src_paths)
    identities = span_identities(spans, span_commits) if spans is not None else None
    details: dict[str, dict[str, Any]] = {
        p: {
            "commit": str(f.get("commit_sha") or ""),
            "indexed_commit": str(indexed.get(p, {}).get("indexed_commit") or ""),
            "symbol_commits": sorted((span_commits or {}).get(p, set())),
            "freshness": "verified"
            if verified_source(p, str(f.get("commit_sha") or ""), head, indexed, identities)
            else "unverified",
        }
        for p, f in files.items()
    }
    ranges = {
        p: cr.json_ranges(str(f.get("lines") or "[]"))
        for p, f in files.items()
        if details[p]["freshness"] == "verified"
    }
    whole = {p: [(1, 10**9)] for p in src_paths}
    out = cc.measured_execution(spans if spans else whole, ranges)
    commits = sorted({d["commit"] for d in details.values()})
    out.update(
        commit=commits[0] if len(commits) == 1 else "",
        commits=commits,
        index_commit=head,
        run_current=bool(head and commits == [head]),
        files=details,
        freshness="verified"
        if len(ranges) == len(set(src_paths))
        else ("partial" if ranges else "unverified"),
        unverified_paths=[p for p, d in details.items() if d["freshness"] == "unverified"],
        unmeasured_paths=[p for p in src_paths if p not in files],
    )
    return out


def body_extents(row: dict[str, Any]) -> list[tuple[str, int, int]]:
    """(symbol_id, start, end) of every tier row of a definition that holds a body: the
    row's `extents`, or its own span. A one-line extent is left out: a compiler row may be
    the declaration line only, and whether that line ran says nothing about the body."""
    own = (
        str(row.get("symbol_id") or ""),
        int(row.get("start_line") or 0),
        int(row.get("end_line") or 0),
    )
    return [e for e in row.get("extents") or [own] if 0 < e[1] < e[2]]


async def measured_definitions(
    repo: CodeRepo, org: str, ns: str, rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """What a coverage run measured of each definition row (`symbol_id`, `path` and its
    spans, see `body_extents`): symbol_id -> {executed, commit}. A definition is absent
    unless the run measured its file at the commit the index parsed both the file and one
    of the definition's body extents at; one measured unexecuted also needs the run at the
    index watermark, as the coverage claim's `uncovered` does. No coverage rows, one read."""
    paths = list(dict.fromkeys(str(r.get("path") or "") for r in rows if r.get("path")))
    files = await repo.coverage_files_for(org, ns, paths) if paths else {}
    if not files:
        return {}
    kinds = tuple(sorted({str(r.get("kind") or "function") for r in rows}))
    (head, indexed), extents = await asyncio.gather(
        source_identities(repo, org, ns, list(files)),
        repo.functions_for_paths(org, ns, list(files), limit=2000, kinds=kinds, with_revision=True),
    )
    parsed_at = {
        (str(e["symbol_id"]), int(e["start_line"] or 0), int(e["end_line"] or 0)): str(
            e.get("extent_commit") or ""
        ).rstrip("\0")
        for e in extents
    }
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        path, sid = str(r.get("path") or ""), str(r.get("symbol_id") or "")
        if path not in files:
            continue
        commit = str(files[path].get("commit_sha") or "")
        ran = cr.json_ranges(str(files[path].get("lines") or "[]"))
        verified = [
            (start, end)
            for extent_id, start, end in body_extents(r)
            if verified_source(
                path, commit, head, indexed, {path: {parsed_at.get((extent_id, start, end), "")}}
            )
        ]
        executed = any(cr.intersects(ran, start, end) for start, end in verified)
        if verified and (executed or commit == head):
            out[sid] = {"executed": executed, "commit": commit}
    return out
