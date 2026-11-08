"""Content-based reconciliation for the fast tier.

Watermarks (`git_head`, `scip_commit`) say which commits were WALKED; they say nothing
about whether every row those commits should have refreshed actually was: rows can carry
wrong line numbers while every lag gauge reads 0. This module measures drift directly: for
each indexed path, the commit at which the file was last fully parsed
(`code_files.indexed_commit`, rewritten on every successful ingest of the path) versus the
branch head — if the file changed in between, the path is stale. Chunk-row commits are NOT
the marker: an unmoved chunk keeps its original indexed_commit forever, and any min()/max()
over commit hashes orders hex strings, not history. One `git diff --name-only` per distinct
commit (a few dozen after a healthy week, bounded by `max_commits`), never one per file.

`find_stale_paths` is pure measurement; `heal` re-ingests a bounded batch at
head through the same `ingest_file` the worker uses (moved chunks are
re-versioned for free, only genuinely new content is embedded).

Per-file identity is compared against the tree rather than replaying history: a
`code_files` row carries `blob_sha`, git's own id of the
bytes that were parsed. Those rows are judged by ONE `git ls-tree -r <head>` — stale iff
the blob at head differs, gone iff the path is absent — with no per-commit diff and no
`max_commits` ceiling; the cost is the same whether the namespace is one commit or a
thousand behind. Rows without a blob keep the diff walk. Both halves land in
the same `stale` list.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.extractor_stamp import STAGES
from server.core.background import gather_or_cancel
from server.llm import customer

logger = logging.getLogger("loom.code_index.reconcile")


@dataclass
class ReconcileResult:
    head: str
    paths_total: int = 0
    commits_distinct: int = 0
    commits_checked: int = 0
    commits_failed: int = 0  # unknown to the clone (force-push, GC): unverifiable, not stale
    stale: list[str] = field(default_factory=list)
    truncated: bool = False  # more distinct commits than max_commits — stale is a floor
    took_ms: int = 0
    # Paths judged by blob identity against one ls-tree of head (no diff, no ceiling),
    # and indexed paths the head tree no longer has.
    by_tree: int = 0
    gone: list[str] = field(default_factory=list)
    tree_ms: int = 0
    # Paths at head inside the entry's scope that have no row at all (a widened prefix
    # list, a file the commit walk never reached), with the blob each one has at head.
    # Only measured when the caller passes `in_scope`.
    missing: dict[str, str] = field(default_factory=dict)

    @property
    def stale_count(self) -> int:
        return len(self.stale)


async def _judge_by_tree(
    rows3: list[tuple[str, str, str]],
    git_dir: str,
    head: str,
    ls_tree_blobs: Any,
    res: ReconcileResult,
    *,
    need_tree: bool = False,
) -> tuple[set[str], bool, Mapping[str, str]]:
    """The tree pass: rows that recorded the blob they parsed are judged against ONE
    ls-tree of head — stale iff the blob differs, gone iff head lacks the path. Returns
    (stale, judged, tree); judged=False means the head could not be listed and every row
    takes the diff walk. `need_tree` lists head even when no row has a blob, for the
    missing-path measure."""
    stale: set[str] = set()
    with_blob = [(p, b) for p, _c, b in rows3 if b]
    if not with_blob and not need_tree:
        return stale, False, {}
    t_tree = time.monotonic()
    try:
        tree = await ls_tree_blobs(git_dir, head)
    except Exception as e:  # noqa: BLE001 — unreadable head: those rows take the diff walk
        logger.info("reconcile: ls-tree %s failed, falling back to diffs: %s", head[:12], e)
        return stale, False, {}
    finally:
        res.tree_ms = int((time.monotonic() - t_tree) * 1000)
    gone: list[str] = []
    for path, blob in with_blob:
        at_head = tree.get(path)
        if at_head is None:
            gone.append(path)
        elif at_head != blob:
            stale.add(path)
    res.by_tree = len(with_blob)
    res.gone = sorted(gone)
    return stale, bool(with_blob), tree


async def find_stale_paths(
    repo: Any,
    git_dir: str,
    org: str,
    ns: str,
    head: str,
    *,
    diff_files: Any = None,
    ls_tree_blobs: Any = None,
    max_commits: int = 1000,
    concurrency: int = 8,
    in_scope: Callable[[str], bool] | None = None,
    unindexable: Mapping[str, str] | None = None,
) -> ReconcileResult:
    """Paths whose last full parse predates a change to the file at `head`.

    With `in_scope`, also the paths at head the index has no row for (`missing`),
    leaving out each path `unindexable` records at the same blob — a file an earlier
    backfill read and ingest refused (binary, generated, no parser) stays out until
    its bytes change."""
    if diff_files is None:
        from server.code.app.index.gitrepo import diff_files as _df

        diff_files = _df
    if ls_tree_blobs is None:
        from server.code.app.index.gitrepo import ls_tree_blobs as _lt

        ls_tree_blobs = _lt
    started = time.monotonic()
    res = ReconcileResult(head=head)
    rows3 = [
        (str(p), str(c or ""), str(b or "")) for p, c, b in await repo.file_identities(org, ns)
    ]
    res.paths_total = len(rows3)
    stale, judged, tree = await _judge_by_tree(
        rows3, git_dir, head, ls_tree_blobs, res, need_tree=in_scope is not None
    )
    if in_scope is not None:
        indexed = {p for p, _c, _b in rows3}
        refused = unindexable or {}
        res.missing = {
            p: b
            for p, b in sorted(tree.items())
            if p not in indexed and refused.get(p) != b and in_scope(p)
        }
    # -- diff walk for the rest (rows without a blob, or a head the mirror could not list)
    groups: dict[str, set[str]] = defaultdict(set)
    for path, commit, blob in rows3:
        if judged and blob:
            continue
        c = str(commit or "")
        if not c or c == head:
            continue
        groups[c].add(str(path))
    res.commits_distinct = len(groups)
    # most-populated commits first, so a truncated pass still covers the bulk
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    if len(ordered) > max_commits:
        res.truncated = True
        ordered = ordered[:max_commits]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(commit: str, paths: set[str]) -> None:
        async with sem:
            try:
                changed = {e.path for e in await diff_files(git_dir, commit, head)}
            except Exception as e:  # noqa: BLE001 — unknown commit: unverifiable
                res.commits_failed += 1
                logger.info("reconcile: diff %s..%s failed: %s", commit[:12], head[:12], e)
                return
            res.commits_checked += 1
            stale.update(paths & changed)

    await asyncio.gather(*(_one(c, p) for c, p in ordered))
    res.stale = sorted(stale)
    res.took_ms = int((time.monotonic() - started) * 1000)
    return res


async def drain_dependents_pending(
    repo: Any,
    cfg: Any,
    head: str,
    *,
    budget: int,
    embedding_model: str,
    ingest_file: Any = None,
    show_file: Any = None,
) -> Any:
    """Re-extract up to `budget` paths a commit's dependents cap left in
    code_index_state `dependents_pending`, at `head` and edges-only, before the
    stale-path heal spends the rest of the budget. Never touches the git_head watermark."""
    if ingest_file is None:
        from server.code.app.index.ingest import ingest_file as _if

        ingest_file = _if
    if show_file is None:
        from server.code.app.index.gitrepo import show_file as _sf

        show_file = _sf
    from server.code.app.index.dependents import drain_pending

    return await drain_pending(
        repo,
        cfg.org,
        cfg.namespace,
        head=head,
        git_dir=cfg.git_dir,
        budget=budget,
        show_file=show_file,
        ingest_file=ingest_file,
        embedding_model=embedding_model,
    )


@dataclass
class HealResult:
    files: int = 0
    chunks_new: int = 0
    chunks_moved: int = 0
    symbols_moved: int = 0
    errors: int = 0
    # Scoped heals only: files ingested under the scope (their content matched the
    # recorded hash, the other stages were skipped) and, of those, files where the
    # scoped stages found nothing to write.
    scoped: int = 0
    identical: int = 0
    # Paths the heal took on (every path of a started round, whether it was found,
    # ingested or failed) — what a cursor advances by; `partial` when the budget stopped
    # it before the last round.
    attempted: int = 0
    partial: bool = False
    rounds: int = 0
    # Per-file resolution reads the rounds answered from memory / sent on.
    read_hits: int = 0
    read_misses: int = 0
    # Paths ingest took on and indexed nothing for (absent at head, refused by the
    # denylist or its content rules, or no parser claims them).
    skipped_paths: list[str] = field(default_factory=list)
    # Oversize paths read by their size alone: counted, or left for the next reconcile
    oversize: set[str] = field(default_factory=set)

    def record(self, path: str, recorded: Any) -> None:
        """An oversize file's record (`read_capped`): its stats count as an ingested
        file's; a failed one is an error with no row, which the next reconcile asks for
        again, so it is not a skipped path either."""
        if recorded is None:
            return
        self.oversize.add(path)
        if recorded is False:
            self.errors += 1
        else:
            self.count(path, recorded, scoped=False)

    def count(self, path: str, stats: Any, *, scoped: bool) -> None:
        """One ingested file's counts (`scoped`: the heal ran under a stage scope)."""
        self.files += 1
        if getattr(stats, "skipped", False):
            self.skipped_paths.append(path)
        self.chunks_new += stats.chunks_new
        self.chunks_moved += stats.chunks_moved
        self.symbols_moved += stats.symbols_moved
        if scoped and len(getattr(stats, "stages_run", ())) < len(STAGES):
            self.scoped += 1
        if getattr(stats, "identical", False):
            self.identical += 1


async def heal(
    repo: Any,
    cfg: Any,
    head: str,
    paths: list[str],
    *,
    embedder: Any,
    ingest_file: Any = None,
    show_file: Any = None,
    concurrency: int = 4,
    stages: frozenset[str] | None = None,
    live_hashes: Mapping[str, str] | None = None,
    stop_after: float | None = None,
    round_files: int | None = None,
) -> HealResult:
    """Re-ingest `paths` at `head` — the repair half. Never touches the
    git_head watermark: a targeted repair proves nothing about the rest.

    `stages` scopes the heal to the extractor stages that moved; `live_hashes` (path ->
    `code_files.content_hash`) is what lets `ingest_file` prove a file's content is
    unchanged before it skips the other stages. Without a scope every stage runs, as a
    content repair must.

    The paths go in rounds of `round_files` (default `code_ingest_round_files`; 0 = one
    round), each round's per-file resolution reads answered from two round-wide
    statements (`RoundReads`). `stop_after` (a monotonic instant) is checked before
    every round: past it no further round starts, the in-flight one finishes, and the
    result says how many paths were attempted and that it is partial — the caller's
    cursor advances by `attempted`. A held model call (`customer.HOLDS`) raises
    instead, its round's peers cancelled, so the caller's cursor stays."""
    if ingest_file is None:
        from server.code.app.index.ingest import ingest_file as _if

        ingest_file = _if
    if show_file is None:
        from server.code.app.index.gitrepo import show_file as _sf

        show_file = _sf
    from server.code.app.index.denylist import load_denylist
    from server.code.app.index.ingest import read_capped
    from server.code.app.index.round_reads import RoundReads, round_size, rounds
    from server.settings import settings

    out = HealResult()
    denylist = await load_denylist(cfg, head, show_file)
    sem = asyncio.Semaphore(max(1, concurrency))
    size = round_size() if round_files is None else max(0, int(round_files))

    async def _source(path: str) -> tuple[str, bytes | None]:
        async with sem:
            source, recorded = await read_capped(
                show_file,
                repo,
                cfg.org,
                cfg.namespace,
                git_dir=cfg.git_dir,
                path=path,
                commit_sha=head,
                embedding_model=embedder.model_name,
                denylist=denylist,
            )
        out.record(path, recorded)
        return path, source

    async def _one(reads: Any, path: str, source: bytes) -> None:
        async with sem:
            scoped: dict[str, Any] = {}
            if stages is not None:
                scoped = {
                    "stages": stages,
                    "live_content_hash": str((live_hashes or {}).get(path) or ""),
                }
            try:
                stats = await ingest_file(
                    reads,
                    cfg.org,
                    cfg.namespace,
                    path=path,
                    source=source,
                    commit_sha=head,
                    embedding_model=embedder.model_name,
                    embedder=embedder,
                    max_chunk_chars=settings.code_chunk_max_chars,
                    embed_reuse_from=cfg.embed_reuse_from,
                    denylist=denylist,
                    parsed=reads.parsed.get(path),
                    **scoped,
                )
            except customer.HOLDS:
                raise  # the org's model calls are held: the caller keeps its cursor
            except Exception as e:  # noqa: BLE001 — per-file
                out.errors += 1
                logger.warning("reconcile heal: %s failed: %s", path, e)
                return
            out.count(path, stats, scoped=stages is not None)

    for batch in rounds(list(paths), size):
        if stop_after is not None and time.monotonic() >= stop_after:
            out.partial = True
            break
        out.rounds += 1
        out.attempted += len(batch)
        found = dict(await asyncio.gather(*(_source(p) for p in batch)))
        sources = {p: src for p, src in found.items() if src is not None}
        out.skipped_paths.extend(p for p in batch if p not in sources and p not in out.oversize)
        async with RoundReads(repo, cfg.org, cfg.namespace) as reads:
            if size > 0:
                await reads.prefetch(sources, denylist, embedding_model=embedder.model_name)
            # A held file ends the heal, its peers cancelled before the reads close.
            await gather_or_cancel(*(_one(reads, p, sources[p]) for p in batch if p in sources))
        out.read_hits += reads.hits
        out.read_misses += reads.misses
    return out


async def backfill_missing(
    repo: Any,
    cfg: Any,
    head: str,
    missing: Mapping[str, str],
    *,
    limit: int,
    embedder: Any,
    stop_after: float | None = None,
    heal_fn: Any = None,
) -> tuple[HealResult | None, dict[str, str]]:
    """Ingest up to `limit` of the `missing` paths at `head` through `heal`, and
    return (the heal, {path: blob} of the attempted paths ingest indexed nothing for).
    The caller keeps that map so the next measure leaves those files out until their
    bytes change, instead of re-reading a binary every interval."""
    paths = list(missing)[: max(0, limit)]
    if not paths:
        return None, {}
    healed = await (heal_fn or heal)(
        repo, cfg, head, paths, embedder=embedder, stop_after=stop_after
    )
    tried = set(paths[: healed.attempted])
    refused = {p: missing[p] for p in healed.skipped_paths if p in tried}
    return healed, refused
