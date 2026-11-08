"""Dependents re-resolution and closure repair for the fast tier.

Edges are derived per path when THAT path changes. The name table they
resolve against is repo-wide, so a commit that adds, removes or re-signs a
symbol in one file silently changes the right answer for every untouched
file that names it: an edge into a tombstoned target outlives it (an empty
blast radius reads as safe), a site declined for lack of a definition stays
declined after one appears, an `ambiguous` count goes stale. This module
closes that hole per commit, bounded:

    dependents set   untouched paths whose edges or declined sites depend on
                     what this commit changed —
                     (a) live declined sites naming a symbol added or removed,
                     (b) live edges into a symbol tombstoned this commit,
                     (d) live repo-wide (0.6) edges into a name that now has
                         several definitions — they must fall back to a miss.
                     ((c), a header's includers, is not a rule: they
                     re-extract to the same edges.)
                     The commit's own paths qualify under (a), (b) and (d):
                     touched files are derived in sequence against a name
                     table the same commit is still changing.
                     Capped per commit; the overflow waits in code_index_state
                     `dependents_pending` and drains through reconcile.
    re-extraction    ingest_file(edges_only=True) at the same commit: content
                     unchanged, so chunks/symbols/file row are untouched and
                     only edges + declined sites are re-derived and key-diffed
                     (an unchanged edge is never re-inserted).
    closure repair   a bare name added this commit that now has exactly ONE
                     live definition binds its live `unresolved` sites at 0.6
                     directly from the rows — no parse — under the extractor's
                     own rule-3 candidate filter (edges_treesitter.
                     candidates_for_call), so the next full ingest of the
                     site's file agrees. Several definitions leave the site
                     declined; its count is refreshed by (a).

All CH I/O goes through CodeRepo; the derivation is pure so the fixtures in
tests/unit/test_code_ingest_dependents.py exercise it without a database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.edges_treesitter import (
    REPO_WIDE_CONFIDENCE,
    candidates_for_call,
)
from server.code.store.index import CodeRepo, build_row

logger = logging.getLogger("loom.code_index.dependents")

PENDING_KEY = "dependents_pending"


@dataclass
class CommitDelta:
    """What one commit changed in the name table, gathered from the
    FileIngestStats of its touched files (added names, removed names,
    tombstoned ids) and the paths it touched (never dependents of
    themselves)."""

    touched_paths: set[str] = field(default_factory=set)
    names_added: set[str] = field(default_factory=set)
    names_removed: set[str] = field(default_factory=set)
    symbols_tombstoned: set[str] = field(default_factory=set)

    @classmethod
    def from_stats(cls, stats: list[Any], touched_paths: set[str]) -> CommitDelta:
        d = cls(touched_paths=set(touched_paths))
        for s in stats:
            if s is None:
                continue
            d.names_added.update(n for n in getattr(s, "symbol_names_added", ()) if n)
            d.names_removed.update(n for n in getattr(s, "symbol_names_removed", ()) if n)
            d.symbols_tombstoned.update(i for i in getattr(s, "symbols_tombstoned_ids", ()) if i)
        return d

    @property
    def empty(self) -> bool:
        return not (self.names_added or self.names_removed or self.symbols_tombstoned)


@dataclass
class ClosureRepairResult:
    names: int = 0  # added names considered (after the cap)
    names_unique: int = 0  # of those, exactly one live definition repo-wide
    sites_read: int = 0  # live declined rows read for the commit's names
    resolved: int = 0  # unresolved sites bound at 0.6 (edge + miss tombstone)
    left_declined: int = 0  # unresolved sites whose name has several definitions
    truncated: bool = False  # more added names than the cap
    ms: int = 0


@dataclass
class DependentsResult:
    candidates: int = 0  # distinct dependent paths before the cap
    reingested: int = 0
    skipped: int = 0  # not at this commit / not indexable
    queued: int = 0  # overflow written to dependents_pending this commit
    pending_total: int = 0  # size of the queue after this commit
    dropped: int = 0  # overflow beyond code_ingest_dependents_pending_max
    edges_new: int = 0
    edges_tombstoned: int = 0
    misses_new: int = 0
    misses_tombstoned: int = 0
    misses_refreshed: int = 0  # verdict rewritten under the same site key
    errors: int = 0
    by_source: dict[str, int] = field(default_factory=dict)  # a/b/d -> paths
    ms: int = 0


def plan_closure_repair(
    misses: list[dict[str, Any]],
    name_table: dict[str, list[tuple[str, str, str]]],
    names_added: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """(resolvable miss rows, their edge targets as (symbol_id, qname) dicts,
    count left declined). Pure: rule 3 of the extractor applied to stored
    rows — an `unresolved` site of an ADDED name resolves iff its
    qualification-filtered candidate set has exactly one member."""
    resolvable: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    left = 0
    for m in misses:
        name = str(m.get("callee_name") or "")
        if name not in names_added or str(m.get("disposition") or "") != "unresolved":
            continue
        cands = candidates_for_call(name_table, str(m.get("call_text") or name), name)
        if len(cands) == 1:
            resolvable.append(m)
            targets.append({"symbol_id": cands[0][0], "qualified_name": cands[0][1]})
        elif cands:
            left += 1
    return resolvable, targets, left


def derive_dependents(
    delta: CommitDelta,
    *,
    miss_paths: list[str],
    tombstoned_edge_paths: list[str],
    ambiguous_edge_paths: list[str],
) -> tuple[list[str], dict[str, int]]:
    """Union of the three sources, sorted so a cap is deterministic;
    per-source counts for the log line.

    The commit's own paths are NOT excluded. Touched files are derived one at a time
    against a name table the same commit is still changing, so a caller ingested before
    its definer is declined at the commit that added the definition — excluding them
    would leave exactly those sites behind. A touched path appears here only when a live
    miss or edge of ITS OWN depends on the delta, which is precisely the case where
    re-deriving it changes a row."""
    src = {
        "a_declined_sites": set(miss_paths),
        "b_tombstoned_targets": set(tombstoned_edge_paths),
        "d_now_ambiguous": set(ambiguous_edge_paths),
    }
    union: set[str] = set()
    for s in src.values():
        union |= s
    return sorted(union), {k: len(v) for k, v in src.items()}


def split_by_cap(paths: list[str], cap: int) -> tuple[list[str], list[str]]:
    cap = max(0, int(cap))
    return paths[:cap], paths[cap:]


# ---------------------------------------------------------------------------
# pending queue (code_index_state key `dependents_pending`)
# ---------------------------------------------------------------------------


async def read_pending(repo: Any, org: str, ns: str) -> tuple[list[str], dict[str, Any]]:
    state = await repo.all_state(org, ns)
    row = state.get(PENDING_KEY) or {}
    try:
        detail = json.loads(str(row.get("detail") or "{}"))
    except ValueError:
        detail = {}
    paths = [str(p) for p in (detail.get("paths") or []) if p]
    return paths, detail


async def write_pending(
    repo: Any,
    org: str,
    ns: str,
    paths: list[str],
    *,
    head: str,
    dropped_total: int = 0,
) -> None:
    await repo.set_state(
        org,
        ns,
        PENDING_KEY,
        str(len(paths)),
        detail=json.dumps(
            {
                "at": int(time.time()),
                "head": head[:12],
                "paths": paths,
                "dropped_total": int(dropped_total),
            }
        ),
    )


async def enqueue_pending(
    repo: Any, org: str, ns: str, overflow: list[str], *, head: str, pending_max: int
) -> tuple[int, int, int]:
    """Merge `overflow` into the queue; (queued now, queue size, dropped now)."""
    existing, detail = await read_pending(repo, org, ns)
    merged = sorted(set(existing) | set(overflow))
    dropped = max(0, len(merged) - int(pending_max))
    merged = merged[: int(pending_max)]
    queued = len(set(merged) - set(existing))
    await write_pending(
        repo,
        org,
        ns,
        merged,
        head=head,
        dropped_total=int(detail.get("dropped_total") or 0) + dropped,
    )
    return queued, len(merged), dropped


# ---------------------------------------------------------------------------
# re-extraction
# ---------------------------------------------------------------------------


async def reextract_paths(
    repo: Any,
    org: str,
    ns: str,
    paths: list[str],
    *,
    commit_sha: str,
    git_dir: str,
    show_file: Any,
    ingest_file: Any,
    embedding_model: str,
) -> DependentsResult:
    """ingest_file(edges_only=True) for each path at `commit_sha`. A path
    absent at that commit (deleted later in the same window) is skipped;
    one failure never stops the batch."""
    out = DependentsResult()
    from server.settings import settings

    sem = asyncio.Semaphore(max(1, int(settings.code_ingest_dependents_concurrency)))
    from server.code.app.index.round_reads import RoundReads, round_size, rounds

    size = round_size()
    for batch in rounds(list(paths), size):
        # A batch's per-path reads (edges, declined sites, guards, the call names) from
        # one statement each, like a commit's round, and its rows one INSERT per table.
        # Dependents write no symbols, so the prefetched name table holds for the whole
        # batch.
        reads = RoundReads(repo, org, ns) if size > 0 else repo
        sources = await _batch_sources(batch, git_dir, commit_sha, show_file, out)
        if size > 0:
            await reads.prefetch(sources)
        try:
            await asyncio.gather(
                *(
                    _reextract_one(
                        reads,
                        org,
                        ns,
                        path,
                        sources[path],
                        sem,
                        out,
                        commit_sha=commit_sha,
                        embedding_model=embedding_model,
                        ingest_file=ingest_file,
                    )
                    for path in batch
                    if path in sources
                )
            )
        finally:
            if size > 0:
                await reads.flush()
    return out


async def _batch_sources(
    batch: list[str],
    git_dir: str,
    commit_sha: str,
    show_file: Any,
    out: DependentsResult,
) -> dict[str, bytes]:
    """The batch's sources at `commit_sha`; an absent path, or one over
    `code_ingest_max_file_bytes` (which re-derives nothing), is counted skipped."""
    from server.code.app.index.gitrepo import BlobTooLarge
    from server.settings import settings

    cap = settings.code_ingest_max_file_bytes

    async def _read(path: str) -> bytes | None:
        try:
            source: bytes | None = await show_file(git_dir, commit_sha, path, max_bytes=cap)
        except BlobTooLarge:
            return None
        return source

    got = await asyncio.gather(*(_read(p) for p in batch))
    sources = {}
    for path, source in zip(batch, got, strict=True):
        if source is None:
            out.skipped += 1
        else:
            sources[path] = source
    return sources


async def _reextract_one(
    repo: Any,
    org: str,
    ns: str,
    path: str,
    source: bytes,
    sem: asyncio.Semaphore,
    out: DependentsResult,
    *,
    commit_sha: str,
    embedding_model: str,
    ingest_file: Any,
) -> None:
    parsed = getattr(repo, "parsed", None)
    extra = {"parsed": parsed[path]} if isinstance(parsed, dict) and path in parsed else {}
    async with sem:
        try:
            stats = await ingest_file(
                repo,
                org,
                ns,
                path=path,
                source=source,
                commit_sha=commit_sha,
                embedding_model=embedding_model,
                embedder=None,
                edges_only=True,
                **extra,
            )
        except Exception as e:  # noqa: BLE001 — per path
            out.errors += 1
            logger.warning("dependents: %s failed: %s", path, e)
            return
        if getattr(stats, "skipped", False):
            out.skipped += 1
            return
        out.reingested += 1
        out.edges_new += int(getattr(stats, "edges_new", 0))
        out.edges_tombstoned += int(getattr(stats, "edges_tombstoned", 0))
        out.misses_new += int(getattr(stats, "misses_new", 0))
        out.misses_tombstoned += int(getattr(stats, "misses_tombstoned", 0))
        out.misses_refreshed += int(getattr(stats, "misses_refreshed", 0))


# ---------------------------------------------------------------------------
# per-commit entry point
# ---------------------------------------------------------------------------


async def run_for_commit(
    repo: Any,
    org: str,
    ns: str,
    *,
    delta: CommitDelta,
    commit_sha: str,
    git_dir: str,
    show_file: Any,
    ingest_file: Any,
    embedding_model: str,
    dependents_enabled: bool,
    closure_enabled: bool,
    max_files: int,
    pending_max: int,
    closure_max_names: int,
    closure_max_sites: int,
) -> tuple[ClosureRepairResult | None, DependentsResult | None]:
    """Closure repair first (it retires miss rows the dependents set would
    otherwise re-extract), then the bounded dependents re-extraction. Every
    read is a key read: misses by callee_name, edges by to_symbol_id,
    definitions by name."""
    if delta.empty or not (dependents_enabled or closure_enabled):
        return None, None

    # the same per-commit cap on each side: a generated file that adds
    # thousands of names is not an occasion to read thousands of key ranges
    truncated = (
        len(delta.names_added) > closure_max_names or len(delta.names_removed) > closure_max_names
    )
    added = set(sorted(delta.names_added)[:closure_max_names])
    removed = set(sorted(delta.names_removed)[:closure_max_names])
    names = sorted(added | removed)
    misses = (
        await repo.live_call_misses_for_names(
            org,
            ns,
            names,
            limit=closure_max_sites,
        )
        if names
        else []
    )
    name_table: dict[str, list[tuple[str, str, str]]] = (
        await repo.name_candidates(org, ns, sorted(added)) if added else {}
    )

    closure: ClosureRepairResult | None = None
    resolved_keys: set[tuple[str, str, int, str]] = set()
    if closure_enabled:
        t0 = time.monotonic()
        closure = ClosureRepairResult(
            names=len(added),
            sites_read=len(misses),
            truncated=truncated,
            names_unique=sum(1 for n in added if len(name_table.get(n, [])) == 1),
        )
        resolvable, targets, left = plan_closure_repair(misses, name_table, added)
        closure.left_declined = left
        edge_rows = []
        miss_tombstones = []
        for m, t in zip(resolvable, targets, strict=True):
            edge_rows.append(
                build_row(
                    "code_edges",
                    org=org,
                    namespace=ns,
                    tier="treesitter",
                    edge_kind="calls",
                    to_symbol_id=t["symbol_id"],
                    from_symbol_id=str(m["from_symbol_id"]),
                    path=str(m["path"]),
                    line=int(m["line"]),
                    from_qualified_name=str(m.get("from_qualified_name") or ""),
                    to_qualified_name=t["qualified_name"],
                    confidence=REPO_WIDE_CONFIDENCE,
                    indexed_commit=commit_sha,
                )
            )
            miss_tombstones.append(CodeRepo.tombstone_of("code_call_sites_unresolved", m))
            resolved_keys.add(
                (str(m["callee_name"]), str(m["path"]), int(m["line"]), str(m["from_symbol_id"]))
            )
        # one insert per table for the whole commit (insert volume, not row
        # count, is what a shared merge tree pays for)
        await repo.insert_rows("code_edges", edge_rows)
        await repo.insert_rows("code_call_sites_unresolved", miss_tombstones)
        closure.resolved = len(edge_rows)
        closure.ms = int((time.monotonic() - t0) * 1000)

    if not dependents_enabled:
        return closure, None

    t1 = time.monotonic()
    miss_paths = [
        str(m["path"])
        for m in misses
        if (str(m["callee_name"]), str(m["path"]), int(m["line"]), str(m["from_symbol_id"]))
        not in resolved_keys
    ]
    tomb_ids = sorted(delta.symbols_tombstoned)
    tomb_paths = (
        [
            str(r["path"])
            for r in await repo.live_edges_to_symbols(
                org,
                ns,
                tomb_ids,
                edge_kinds=["calls", "inherits", "fixture_use"],
            )
        ]
        if tomb_ids
        else []
    )
    ambiguous_ids = sorted(
        {c[0] for n in added if len(name_table.get(n, [])) > 1 for c in name_table[n]}
    )
    amb_paths = (
        [
            str(r["path"])
            for r in await repo.live_edges_to_symbols(
                org,
                ns,
                ambiguous_ids,
                edge_kinds=["calls"],
                max_confidence=0.65,
            )
        ]
        if ambiguous_ids
        else []
    )
    paths, by_source = derive_dependents(
        delta,
        miss_paths=miss_paths,
        tombstoned_edge_paths=tomb_paths,
        ambiguous_edge_paths=amb_paths,
    )
    now, overflow = split_by_cap(paths, max_files)
    deps = await reextract_paths(
        repo,
        org,
        ns,
        now,
        commit_sha=commit_sha,
        git_dir=git_dir,
        show_file=show_file,
        ingest_file=ingest_file,
        embedding_model=embedding_model,
    )
    deps.candidates = len(paths)
    deps.by_source = by_source
    if overflow:
        deps.queued, deps.pending_total, deps.dropped = await enqueue_pending(
            repo,
            org,
            ns,
            overflow,
            head=commit_sha,
            pending_max=pending_max,
        )
    deps.ms = int((time.monotonic() - t1) * 1000)
    return closure, deps


@dataclass
class DrainResult:
    files: int = 0
    skipped: int = 0
    errors: int = 0
    remaining: int = 0


async def drain_pending(
    repo: Any,
    org: str,
    ns: str,
    *,
    head: str,
    git_dir: str,
    budget: int,
    show_file: Any,
    ingest_file: Any,
    embedding_model: str,
) -> DrainResult:
    """Re-extract up to `budget` queued paths at `head` and write the rest
    back. Called by reconcile ahead of its stale-path heal, inside the
    heal's own per-pass budget."""
    paths, detail = await read_pending(repo, org, ns)
    out = DrainResult(remaining=len(paths))
    if not paths or budget <= 0:
        return out
    now, rest = paths[:budget], paths[budget:]
    res = await reextract_paths(
        repo,
        org,
        ns,
        now,
        commit_sha=head,
        git_dir=git_dir,
        show_file=show_file,
        ingest_file=ingest_file,
        embedding_model=embedding_model,
    )
    out.files, out.skipped, out.errors = res.reingested, res.skipped, res.errors
    out.remaining = len(rest)
    await write_pending(
        repo,
        org,
        ns,
        rest,
        head=head,
        dropped_total=int(detail.get("dropped_total") or 0),
    )
    return out
