"""`loom code backfill`: the full-checkout index pass, its edge and dialect
folder passes, and the embed phase."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import logging
import os
import pathlib
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from server.cli import code_backfill_workers
from server.cli._ch import connect_code_plane
from server.cli.code_maintenance import _compact_after_bulk
from server.cli.code_run import _identity, _note, _repo_cfg
from server.core.background import gather_or_cancel
from server.llm import customer

if TYPE_CHECKING:
    from server.code.app.index.ingest_stats import FileIngestStats

log = logging.getLogger("loom.cli.code")


def _count_record(counters: dict[str, int], recorded: FileIngestStats | Literal[False]) -> None:
    """Count an oversize file's record as `_Run.one` counts an ingested file."""
    key = "errors" if recorded is False else "excluded" if recorded.excluded else "files"
    counters[key] = counters.get(key, 0) + 1


def _edge_source(
    git_dir: str, head: str, sem: asyncio.Semaphore
) -> Callable[[str], Awaitable[tuple[str, bytes | None]]]:
    """The edges-only pass's reader: a file over `code_ingest_max_file_bytes` reads as
    absent, its row left as the index phase (this run's or an earlier one's) wrote it."""
    from server.code.app.index import gitrepo
    from server.code.app.index.gitrepo import BlobTooLarge
    from server.settings import settings

    async def _source(path: str) -> tuple[str, bytes | None]:
        cap = int(settings.code_ingest_max_file_bytes)
        async with sem:
            try:
                return path, await gitrepo.show_file(git_dir, head, path, max_bytes=cap)
            except BlobTooLarge:
                return path, None

    return _source


def _cursor_batches(paths: list[str], start: int = 0) -> list[tuple[int, list[str]]]:
    """The paths from `start` in cursor batches of 200, each with its first index. The
    headers `headers_first` put first end their own batch, so no batch holds both a
    header and a source: every header can be written before any source batch starts."""
    from server.code.app.index.ingest_headers import HEADER_EXTS

    batch = 200
    split = next((i for i, p in enumerate(paths) if not p.endswith(HEADER_EXTS)), len(paths))
    cuts = {*range(0, split, batch), *range(split, len(paths), batch), start, len(paths)}
    return [(a, paths[a:b]) for a, b in itertools.pairwise(sorted(c for c in cuts if c >= start))]


def index_counters() -> dict[str, int]:
    return {"files": 0, "chunks": 0, "symbols": 0, "edges": 0, "errors": 0}


def edge_counters() -> dict[str, int]:
    return {"files": 0, "edges": 0, "errors": 0}


def add_counters(total: dict[str, int], part: dict[str, int]) -> None:
    for key, n in part.items():
        total[key] = total.get(key, 0) + n


@dataclass
class _Run:
    """What the index phase's cursor batches and its edge pass run with: one in the CLI
    process, and one in each `--workers` process (`code_backfill_workers`)."""

    repo: Any
    cfg: Any
    args: argparse.Namespace
    org: str
    ns: str
    head: str
    model_name: str
    embedder: Any
    denylist: Any
    sem: asyncio.Semaphore
    counters: dict[str, int] = field(default_factory=index_counters)
    edges: dict[str, int] = field(default_factory=edge_counters)

    @classmethod
    async def open(
        cls,
        repo: Any,
        cfg: Any,
        args: argparse.Namespace,
        head: str,
        *,
        org: str,
        ns: str,
        embedder: Any,
    ) -> _Run:
        from server.code.app.index import gitrepo
        from server.code.app.index.denylist import load_denylist
        from server.embedding.embedder import get_embedder

        return cls(
            repo=repo,
            cfg=cfg,
            args=args,
            org=org,
            ns=ns,
            head=head,
            model_name=get_embedder().model_name
            + str(getattr(args, "embedding_model_suffix", "") or ""),
            embedder=embedder,
            denylist=await load_denylist(cfg, head, gitrepo.show_file),
            sem=asyncio.Semaphore(max(1, args.concurrency)),
        )

    async def source(self, path: str) -> tuple[str, bytes | None]:
        from server.code.app.index import gitrepo
        from server.code.app.index.ingest import read_capped

        async with self.sem:
            source, recorded = await read_capped(
                gitrepo.show_file,
                self.repo,
                self.org,
                self.ns,
                git_dir=self.cfg.git_dir,
                path=path,
                commit_sha=self.head,
                embedding_model=self.model_name,
                denylist=self.denylist,
            )
        if recorded is not None:
            _count_record(self.counters, recorded)
        return path, source

    async def one(self, reads: Any, path: str, source: bytes) -> None:
        from server.code.app.index.ingest import ingest_file
        from server.settings import settings

        counters, args = self.counters, self.args
        async with self.sem:
            try:
                stats = await ingest_file(
                    reads,
                    self.org,
                    self.ns,
                    path=path,
                    source=source,
                    commit_sha=self.head,
                    embedding_model=self.model_name,
                    embedder=self.embedder,
                    max_chunk_chars=settings.code_chunk_max_chars,
                    embed_reuse_from=self.cfg.embed_reuse_from,
                    denylist=self.denylist,
                    context_callees=str(getattr(args, "context_callees", "") or "off"),
                    callers_namespace=str(getattr(args, "callers_from", "") or ""),
                    parsed=reads.parsed.get(path),
                )
            except customer.HOLDS:
                raise  # the org's model calls are held: the run stops, its cursor kept
            except Exception as e:  # noqa: BLE001 — per-file, keep going
                counters["errors"] += 1
                print(f"  ! {path}: {type(e).__name__}: {e}", file=sys.stderr)
                return
            if stats.excluded:
                counters["excluded"] = counters.get("excluded", 0) + 1
                return
            counters["files"] += 1
            counters["chunks"] += stats.chunks_new
            counters["symbols"] += stats.symbols_new
            counters["moved"] = counters.get("moved", 0) + stats.chunks_moved + stats.symbols_moved
            counters["edges"] += stats.edges_new
            counters["embed_reused"] = counters.get("embed_reused", 0) + stats.embed_reused

    async def batch(self, paths: list[str]) -> str:
        """One cursor batch of the index phase: "" once written, else why it was held."""
        return await _held_batch(
            self.repo,
            self.org,
            self.ns,
            paths,
            source=self.source,
            one=self.one,
            denylist=self.denylist,
            git_dir=self.cfg.git_dir,
            head=self.head,
        )

    async def edge_one(self, reads: Any, path: str, src: bytes) -> None:
        from server.code.app.index.ingest import ingest_file

        async with self.sem:
            try:
                stats = await ingest_file(
                    reads,
                    self.org,
                    self.ns,
                    path=path,
                    source=src,
                    commit_sha=self.head,
                    embedding_model=self.model_name,
                    edges_only=True,
                    denylist=self.denylist,
                    parsed=reads.parsed.get(path),
                )
            except Exception as e:  # noqa: BLE001 — per-file, keep going
                self.edges["errors"] += 1
                print(f"  ! second pass {path}: {type(e).__name__}: {e}", file=sys.stderr)
                return
            self.edges["files"] += 1
            self.edges["edges"] += stats.edges_new

    async def edge_batch(self, paths: list[str]) -> None:
        """One batch of the edge pass."""
        await _backfill_batch_rounds(
            self.repo,
            self.org,
            self.ns,
            paths,
            source=_edge_source(self.cfg.git_dir, self.head, self.sem),
            one=self.edge_one,
            denylist=self.denylist,
            git_dir=self.cfg.git_dir,
            head=self.head,
        )


async def worker_run(args: argparse.Namespace, head: str) -> _Run:
    """A `--workers` process's run: its own connection, the entry's config and denylist
    at the parent's `head`. The worker is the command itself (`code_backfill_workers`),
    so this runs once the registry is read and the active configuration loaded."""
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.embedding.embedder import get_embedder

    cfg = _repo_cfg(args)
    await connect_code_plane(get_client(), args)
    org, ns = _identity(args)
    embedder = get_embedder() if args.embed else None
    return await _Run.open(CodeRepo(), cfg, args, head, org=org, ns=ns, embedder=embedder)


def _workers_for(
    args: argparse.Namespace, head: str, batches: int, edge_batches: int
) -> contextlib.AbstractAsyncContextManager[code_backfill_workers.Workers | None]:
    """The run's `--workers` processes, for both passes and no more than either has
    batches; None runs them in this one."""
    n = min(args.workers, max(batches, edge_batches))
    if n > 1:
        return code_backfill_workers.opened(n, args, head)
    return contextlib.nullcontext(None)


async def _index_batches(
    run: _Run, batches: list[tuple[int, list[str]]], workers: code_backfill_workers.Workers | None
) -> AsyncGenerator[tuple[int, list[str], dict[str, int], str], None]:
    """The index phase's cursor batches in order, each with its own counters and why the
    org's switch held it ("" once written): in this process, or across `workers`."""
    if workers is not None:
        async for done in workers.index_batches(batches):
            yield done
        return
    for b, chunk in batches:
        run.counters = index_counters()
        held = await run.batch(chunk)
        yield b, chunk, run.counters, held
        if held:
            return


async def _index_phase(
    run: _Run,
    paths: list[str],
    batches: list[tuple[int, list[str]]],
    workers: code_backfill_workers.Workers | None,
    *,
    counters: dict[str, int],
    cursor_key: str,
    started: float,
) -> str:
    """Run the batches, moving the cursor past each one once every earlier one is
    written; why the org's switch held one, "" once all are written. A batch written
    beside the held one is counted and the cursor stays."""
    held = ""
    paths_file = bool(getattr(run.args, "paths_file", ""))
    async with contextlib.aclosing(_index_batches(run, batches, workers)) as done:
        async for b_start, chunk_paths, part, why in done:
            add_counters(counters, part)
            if held:
                continue
            if why:
                held = why
                print(f"backfill stopped at {b_start}/{len(paths)}: {held}", file=sys.stderr)
                continue
            if not paths_file:
                await run.repo.set_state(
                    run.org, run.ns, cursor_key, str(b_start + len(chunk_paths))
                )
            rate = counters["files"] / max(time.monotonic() - started, 1e-9)
            print(
                f"  [{b_start + len(chunk_paths)}/{len(paths)}] files={counters['files']} "
                f"chunks={counters['chunks']} symbols={counters['symbols']} "
                f"edges={counters['edges']} errors={counters['errors']} "
                f"({rate:.1f} files/s)"
            )
    return held


async def _backfill(args: argparse.Namespace) -> int:
    # The run's `--workers` processes close on this stack once the run returns, its
    # records written, as one process retires after its command returns.
    async with contextlib.AsyncExitStack() as stack:
        return await _backfill_run(args, stack)


async def _backfill_run(args: argparse.Namespace, stack: contextlib.AsyncExitStack) -> int:
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    if args.phase == "embed":
        if args.workers > 1:
            print("--workers applies to --phase index", file=sys.stderr)
            return 2
        return await _backfill_embed(CodeRepo(), *_identity(args), args)
    if (worker := getattr(args, "backfill_worker", None)) is not None:
        return await code_backfill_workers.serve(await worker_run(args, worker.head), worker)
    # Before declaring the org: an unconfigured entry records nothing.
    cfg = _repo_cfg(args)
    await connect_code_plane(get_client(), args)
    org, ns = _identity(args)
    repo = CodeRepo()

    git_dir = cfg.git_dir
    if not await asyncio.to_thread(os.path.isdir, git_dir):
        print(f"git dir {git_dir} missing — run `loom code init`", file=sys.stderr)
        return 2

    head = await _backfill_head(git_dir, cfg, args)
    prefixes = cfg.path_prefixes
    paths_file = getattr(args, "paths_file", "")
    paths = await _backfill_paths(cfg, args, head, paths_file)
    print(
        f"backfill: {cfg.org}/{cfg.namespace} branch={cfg.git_branch} "
        f"{len(paths)} files at {head[:12]} (prefixes {prefixes}"
        + (f", vectors reused from {cfg.embed_reuse_from}" if cfg.embed_reuse_from else "")
        + ")"
    )

    from server.embedding.embedder import get_embedder

    embedder = None
    if args.embed:
        embedder = get_embedder()
        if "fake" in embedder.model_name and not getattr(args, "allow_fake_embedder", False):
            print(
                f"refusing to backfill with the fallback embedder {embedder.model_name!r}: "
                "the vectors would be unusable. Set OPENAI_API_KEY (or a local model) "
                "or pass --allow-fake-embedder for a dev run.",
                file=sys.stderr,
            )
            return 2
        print(f"embedding inline with {embedder.model_name}")
    else:
        model = get_embedder().model_name
        print(f"chunks stored as dim=0 stubs for model {model}; run --phase embed after")

    suffix = str(getattr(args, "embedding_model_suffix", "") or "")
    run = await _Run.open(repo, cfg, args, head, org=org, ns=ns, embedder=embedder)
    model_name = run.model_name
    if suffix:
        print(f"twin label: rows written under embedding_model={model_name!r}")
    cursor_key = f"backfill_cursor:index{suffix}"
    owed_key = f"backfill_edges_owed:index{suffix}"
    mode = "repair" if paths_file else "twin" if suffix else "backfill"
    done_upto = "" if paths_file else await repo.get_state(org, ns, cursor_key)
    start_idx = int(done_upto) if done_upto.isdigit() else 0
    batches = _cursor_batches(paths, start_idx)
    owed, recorded = await _record_edge_pass(
        repo,
        org,
        ns,
        args,
        owed_key,
        str(done_upto or ""),
        side_by_side=args.workers > 1 and code_backfill_workers.side_by_side(batches),
    )
    started = time.monotonic()
    counters = index_counters()
    second: dict[str, int] | None = None
    edge_batches = len(_cursor_batches(paths)) if owed else 0
    workers = await stack.enter_async_context(_workers_for(args, head, len(batches), edge_batches))
    held = await _index_phase(
        run, paths, batches, workers, counters=counters, cursor_key=cursor_key, started=started
    )
    if owed and not held:
        if recorded:
            print(f"edge second pass owed by an earlier run of this backfill ({recorded})")
        elif owed == "first-backfill":
            print("first full backfill of this namespace: edge second pass implied")
        elif owed == "workers":
            print("--workers: batches were written side by side; the edge pass binds them")
        second = await _second_edge_pass(run, paths, workers)
    if held:
        # The batches written before the hold are compacted, republished and recorded.
        await _close_index(repo, counters, org, ns, model_name, started, head, mode)
        return 1
    if second is not None:
        counters["edges_second_pass"] = second["edges"]
        counters["errors"] += second["errors"]
        if not paths_file and not second["errors"]:
            await repo.set_state(org, ns, owed_key, "")
    if getattr(args, "dialect_folder_pass", False):
        folder = await _dialect_folder_pass(repo, org, ns, head)
        counters["dialect_inherited"] = folder["inherited"]
    # A targeted repair proves nothing about the rest of the tree, and a twin label
    # nothing about the live label's coverage: the per-merge watermark keeps walking
    # every commit it has not seen, from where the live backfill left it.
    if not paths_file and not suffix:
        await repo.set_state(org, ns, "git_head", head, detail="backfill")
        from server.code.app.index.bootstrap import record_backfill_stamps

        await record_backfill_stamps(repo, org, ns)
    await _close_index(repo, counters, org, ns, model_name, started, head, mode)
    if not paths_file:
        # Cleared once the phase has published: an interrupted run resumes at the end.
        await repo.set_state(org, ns, cursor_key, "")
    if paths_file:
        print(f"repair complete at {head[:12]} (watermark untouched): {counters}")
        return backfill_index_exit_code(counters["files"], counters["errors"])
    if suffix:
        print(f"twin backfill complete at {head[:12]} (watermark untouched): {counters}")
        return backfill_index_exit_code(counters["files"], counters["errors"])
    print(f"backfill index phase complete at {head[:12]}: {counters}")
    _note(phase="index", head=head[:12], total_paths=len(paths), **counters)
    if not args.embed:
        print("run: loom code backfill --phase embed")
    return backfill_index_exit_code(counters["files"], counters["errors"])


def prefix_coverage_note(all_paths: list[str], prefixes: tuple[str, ...], *, top: int = 6) -> str:
    """What the entry's path prefixes leave outside the
    index, by top-level directory — '' when nothing is left out. The default
    prefixes are ClickHouse-shaped (src/, base/, programs/, tests/, ci/); on
    another layout they can silently drop whole trees (docs/, include/) and
    root files. The operator reads this line at the start of the backfill,
    not in a census afterwards."""
    if not prefixes:
        return ""
    outside = [p for p in all_paths if not p.startswith(prefixes)]
    if not outside:
        return ""
    counts: dict[str, int] = {}
    for path in outside:
        key = path.split("/", 1)[0] + "/" if "/" in path else "(root files)"
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    listed = ", ".join(f"{k} {n}" for k, n in ranked)
    more = len(counts) - len(ranked)
    tail = f", +{more} more" if more > 0 else ""
    return (
        f"prefixes leave {len(outside)} of {len(all_paths)} files outside the index: "
        f"{listed}{tail} — set the entry's path_prefixes if any belong in it"
    )


async def _tree_paths_with_coverage(
    git_dir: str, head: str, prefixes: tuple[str, ...]
) -> list[str]:
    """The paths a full backfill indexes (`ls_tree` under the prefixes), from
    one listing that also feeds the coverage line."""
    from server.code.app.index import gitrepo

    everything = await gitrepo.ls_tree(git_dir, head)
    paths = [p for p in everything if p.startswith(prefixes)] if prefixes else everything
    note = prefix_coverage_note(everything, prefixes)
    if note:
        print(f"backfill: {note}")
    return paths


async def _backfill_paths(
    cfg: Any, args: argparse.Namespace, head: str, paths_file: str
) -> list[str]:
    from server.code.app.index.ingest_headers import headers_first

    prefixes = cfg.path_prefixes
    if paths_file:
        listed = await asyncio.to_thread(pathlib.Path(paths_file).read_text, "utf-8")
        wanted = [ln.strip() for ln in listed.splitlines() if ln.strip()]
        paths = [p for p in wanted if p.startswith(prefixes)]
        print(f"repair mode: {len(paths)} of {len(wanted)} listed paths are inside {prefixes}")
    else:
        paths = await _tree_paths_with_coverage(cfg.git_dir, head, prefixes)
    if args.limit:
        paths = paths[: args.limit]
    return headers_first(paths)


async def _record_edge_pass(
    repo: Any,
    org: str,
    ns: str,
    args: argparse.Namespace,
    owed_key: str,
    done_upto: str,
    *,
    side_by_side: bool,
) -> tuple[str, str]:
    """(why this run owes the edge pass, why an earlier run recorded it owed). A cursor
    run records its reason before its first batch, so a run interrupted from there on
    leaves the pass owed; a `--paths-file` repair records nothing."""
    paths_file = bool(getattr(args, "paths_file", ""))
    recorded = "" if paths_file else await repo.get_state(org, ns, owed_key)
    owed = edge_pass_owed(
        explicit=bool(getattr(args, "edges_second_pass", False)),
        declined=bool(getattr(args, "no_edges_second_pass", False)),
        paths_file=paths_file,
        done_upto=done_upto,
        recorded=recorded,
        side_by_side=side_by_side,
    )
    if not paths_file and owed != recorded:
        await repo.set_state(org, ns, owed_key, owed)
    return owed, recorded


def edge_pass_owed(
    *,
    explicit: bool,
    declined: bool,
    paths_file: bool,
    done_upto: str,
    recorded: str,
    side_by_side: bool,
) -> str:
    """Why this run owes the edges-only second pass, '' when it does not. A
    namespace's FIRST full backfill indexes files in tree order, so a call to a
    function whose file comes later binds nothing (a call in fastify.js to
    `buildRouting(…)` reads `unresolved, 0 candidates` while lib/route.js is not
    yet indexed); the second pass repairs that, so a first full backfill implies
    it. `--edges-second-pass` asks for it on any run, `--no-edges-second-pass`
    declines it. A cursor run records the reason before its first batch and
    clears it once the pass is done (`recorded`), so a resumed run, or a rerun
    after an interrupted pass, still runs it. A resumed run with nothing
    recorded, and a `--paths-file` repair, imply nothing.

    A run whose `--workers` wrote batches side by side (`side_by_side`) owes it
    whatever the flags say: a file there resolved against a name table the other
    workers were still filling, where one process would have bound it, so a
    `--paths-file` repair runs it too and `--no-edges-second-pass` does not
    decline it."""
    if side_by_side or recorded == "workers":
        return "workers"
    if explicit:
        return "requested"
    if declined or paths_file:
        return ""
    if recorded:
        return recorded
    return "" if done_upto else "first-backfill"


async def _second_edge_pass(
    run: _Run, paths: list[str], workers: code_backfill_workers.Workers | None
) -> dict[str, int]:
    """The edges-only pass over the same paths once the whole
    tree is indexed. The first pass resolved each file against the name table
    as it stood — a fresh namespace's dbt model indexed before the model it
    `ref()`s bound nothing (bench callers F1 0.286 → 1.0 on the second
    pass). Same rounds as the index phase; chunks, symbols and facets are
    left alone, edges are key-diffed against the live rows. It starts from the
    index as it now stands (`forget_index_reads`), not what the index phase cached
    of it. Across `workers` its batches run in any order: names resolve against the
    finished symbols, but a Python class's inherited members are read from the edges
    the pass has rewritten so far, which one process reads in tree order."""
    from server.code.app.index.ingest_resolve import forget_index_reads

    forget_index_reads()
    started = time.monotonic()
    counters = run.edges
    batches = _cursor_batches(paths)
    print(f"second edge pass: {len(paths)} files against the finished name table")
    if workers is None:
        for _b, chunk in batches:
            await run.edge_batch(chunk)
    else:
        for part in await workers.edge_batches(batches):
            add_counters(counters, part)
    print(
        f"second edge pass complete: files={counters['files']} edges=+{counters['edges']} "
        f"errors={counters['errors']} in {time.monotonic() - started:.0f}s"
    )
    return counters


async def _dialect_folder_pass(repo: Any, org: str, ns: str, head: str) -> dict[str, int]:
    """Every live SQL file without a `dialect` facet takes
    the dialect its own directory's text-decided files agree on, written as
    a file-level facet at confidence 0.5 (`CONF_FOLDER`) — a reader filtering
    by dialect sees the whole folder, and the confidence says which rows
    were the folder's word rather than the file's. Two reads and one insert
    for the namespace."""
    from server.code.domain.extract.lang_sql_dialect import (
        CONF_FOLDER,
        FACET_DIALECT,
        folder_dialects,
    )
    from server.code.domain.extract.lang_sql_model import SQL_LANG
    from server.code.store.index import build_row

    facets = await repo.live_dialect_facets(org, ns)
    decided = [(str(r["path"]), str(r["name"]), float(r["confidence"])) for r in facets]
    have = {p for p, _d, _c in decided}
    sql_paths = await repo.live_paths_for_lang(org, ns, SQL_LANG)
    undecided = sorted(p for p in sql_paths if p not in have)
    assigned = folder_dialects(decided, undecided)
    rows = [
        build_row(
            "code_test_facets",
            org=org,
            namespace=ns,
            facet_kind=FACET_DIALECT,
            name=d,
            path=p,
            ordinal=0,
            line=1,
            confidence=CONF_FOLDER,
            indexed_commit=head,
        )
        for p, d in assigned.items()
    ]
    if rows:
        await repo.insert_rows("code_test_facets", rows)
    by_dialect: dict[str, int] = {}
    for d in assigned.values():
        by_dialect[d] = by_dialect.get(d, 0) + 1
    print(
        f"dialect folder pass: {len(sql_paths)} SQL files, {len(have & set(sql_paths))} decided by "
        f"text, {len(undecided)} undecided, {len(assigned)} inherit their folder's {by_dialect}"
    )
    return {"undecided": len(undecided), "inherited": len(assigned)}


async def _close_index(
    repo: Any,
    counters: dict[str, int],
    org: str,
    ns: str,
    model_name: str,
    started: float,
    head: str,
    mode: str,
) -> None:
    """The index phase's end, after its last watermark write: compact what it wrote,
    record its pass and republish its counts."""
    await _compact_after_bulk(counters["chunks"] + counters.get("moved", 0), model_name)
    await _record_manual_pass(
        "code_ingest",
        org,
        ns,
        started=started,
        done=counters["files"],
        rows_written=counters["chunks"] + counters["symbols"] + counters["edges"],
        errors=counters["errors"],
        counters=counters,
        phase="index",
        head=head[:12],
        mode=mode,
    )
    await _republish_counts(repo, org, ns)


async def _republish_counts(repo: Any, org: str, ns: str) -> None:
    """The shared coverage and counts rows the ingest pass publishes after it moves
    rows. They are read with sequential consistency, because the run's own writes,
    the `git_head` the fingerprint carries included, may not have reached the
    replica that serves the read. The counts row is written last and only once the
    coverage row is: while it trails the chunk table, the next embed phase
    republishes both."""
    from server.core.ch import sequential_reads

    with sequential_reads():
        try:
            await repo.refresh_coverage(org, ns, must_land=True)
        except Exception as e:  # noqa: BLE001 — never fail a finished phase over a cache row
            print(
                f"  coverage and counts not republished: {type(e).__name__}: {e}", file=sys.stderr
            )
            return
        try:
            await repo.refresh_table_counts(org, ns)
        except Exception as e:  # noqa: BLE001 — never fail a finished phase over a cache row
            print(f"  table counts not republished: {type(e).__name__}: {e}", file=sys.stderr)


async def _held_batch(
    repo: Any,
    org: str,
    ns: str,
    batch_paths: list[str],
    *,
    source: Any,
    one: Any,
    denylist: Any,
    git_dir: str,
    head: str,
) -> str:
    """One cursor batch as the org's model calls, so its switch applies to them;
    "" once written, else why the switch held it (the cursor stays)."""
    try:
        with customer.for_org(org, namespace=ns):
            await _backfill_batch_rounds(
                repo,
                org,
                ns,
                batch_paths,
                source=source,
                one=one,
                denylist=denylist,
                git_dir=git_dir,
                head=head,
            )
    except customer.HOLDS as e:
        return str(e)
    return ""


async def _backfill_batch_rounds(
    repo: Any,
    org: str,
    ns: str,
    batch_paths: list[str],
    *,
    source: Any,
    one: Any,
    denylist: Any,
    git_dir: str,
    head: str,
) -> None:
    """One cursor batch of the CLI backfill in rounds of
    `code_ingest_round_files`: the round's sources
    are read first, `RoundReads.prefetch` parses them and answers every
    file's live-edge / live-symbol / declined-site / caller-guard / name reads
    from one statement each, and `ingest_file` runs against the wrapper with
    the parse it already holds. A per-file `live_symbols_for_path` read is one
    whole-namespace-slice scan per file (~1 s, growing with the branch), which
    caps a 50k-file release branch at ~2 files/s whatever `--concurrency`.
    Round size 0 keeps the per-file reads (the wrapper then only delegates)."""
    from server.code.app.index.pass_budget import git_text_source
    from server.code.app.index.round_reads import RoundReads, round_size, rounds

    size = round_size()
    with git_text_source(git_dir, head):
        for rnd in rounds(batch_paths, size):
            found = dict(await asyncio.gather(*(source(p) for p in rnd)))
            sources = {p: src for p, src in found.items() if src is not None}
            async with RoundReads(repo, org, ns) as reads:
                if size > 0:
                    await reads.prefetch(sources, denylist)
                await gather_or_cancel(*(one(reads, p, sources[p]) for p in rnd if p in sources))


async def _backfill_head(git_dir: str, cfg: Any, args: argparse.Namespace) -> str:
    """The commit a backfill indexes. `--commit` pins it (resolved in the
    clone, never fetched); otherwise the branch tip after the usual fetch."""
    from server.code.app.index import gitrepo

    pinned = str(getattr(args, "commit", "") or "").strip()
    if pinned:
        return await gitrepo.resolve_commit(git_dir, pinned)
    if not getattr(args, "no_fetch", False):
        await gitrepo.fetch(
            git_dir,
            cfg.git_remote,
            cfg.git_branch,
            token=cfg.github_token,
            proxy_identity=cfg.git_proxy_identity,
        )
    return await gitrepo.head_sha(git_dir, cfg.git_branch)


def backfill_index_exit_code(files: int, errors: int) -> int:
    """Per-file parse errors are skipped and reported inline, and the
    watermark advances regardless — so the exit code must describe the
    PHASE, not demand a perfect corpus. All-or-nothing here would abort a
    `set -e` runbook AFTER the phase completed, before embeddings,
    over one pathological generated file in 10K. Tolerate up
    to 1% file errors on runs large enough for a rate to mean something;
    small delta passes stay strict, where one error IS the signal."""
    tolerated = files // 100 if files >= 100 else 0
    return 0 if errors <= tolerated else 1


def manual_pass_status(done: int, errors: int) -> str:
    """The census vocabulary for an operator-run pass: `idle` did nothing,
    `partial` did some with per-item errors, `failed` only errored, else `ok`."""
    if done and errors:
        return "partial"
    if errors:
        return "failed"
    return "ok" if done else "idle"


async def _record_manual_pass(
    worker: str,
    org: str,
    ns: str,
    *,
    started: float,
    done: int,
    rows_written: int,
    errors: int,
    counters: dict[str, Any],
    **extra: Any,
) -> None:
    """A pass an operator runs outside
    the supervisor leaves a `loom.worker_status` row too, `trigger='manual'`,
    under the worker whose work it did — so the census can tell an operator's
    backfill from the worker's own passes and the row says which namespace
    it ran on. Written now (the process exits); fail-quiet."""
    try:
        from server.workers import status_store

        pass_started, pass_finished = status_store.wall_from_monotonic(started)
        await status_store.record_manual_pass(
            worker,
            namespace=ns,
            status=manual_pass_status(done, errors),
            started=pass_started,
            finished=pass_finished,
            items_done=done,
            rows_written=rows_written,
            partial_failures=errors,
            detail=status_store.detail_text(
                counters,
                {"command": "loom code", **({"org": org} if org else {}), **extra},
            ),
        )
    except Exception as e:  # noqa: BLE001 — health reporting never fails the command
        log.debug("worker_status manual row not written: %s", e)


async def _backfill_embed(repo: Any, org: str, ns: str, args: argparse.Namespace) -> int:
    from server.code.workers.embed_recovery import CodeEmbedRecoveryResult, _embed_rows
    from server.embedding.embedder import get_embedder
    from server.settings import settings

    embedder = get_embedder()
    total = 0
    started = time.monotonic()
    result = CodeEmbedRecoveryResult()
    declared = False

    async def _finish(code: int) -> int:
        _note(phase="embed", embedded=total, failed=result.failed, poisoned=result.poisoned)
        await _compact_after_bulk(total, embedder.model_name)
        if declared:
            await _republish_counts(repo, org, ns)
        await _record_manual_pass(
            "code_embed_recovery",
            org,
            ns,
            started=started,
            done=total,
            rows_written=total,
            errors=result.failed,
            counters={"embedded": total, "failed": result.failed, "poisoned": result.poisoned},
            phase="embed",
        )
        return code

    async def _pending() -> list[Any]:
        # --include-poisoned: rows that exhausted their attempts get one more
        # pass (inputs are bounded now).
        rows: list[Any] = await repo.pending_embed_chunks(
            org,
            ns,
            embedder.model_name,
            settings.code_embed_max_attempts,
            500,
            include_poisoned=bool(getattr(args, "include_poisoned", False)),
        )
        return rows

    # The first page and the published chunk counts are read before the org is
    # declared (on the client `code_run` connected): a mistyped --org has neither
    # pending rows nor chunks, so a `recording` cell records nothing, and the pass's
    # status row, keyed by namespace, needs no org.
    rows = await _pending()
    declared = bool(rows) or await repo.published_chunks_behind(org, ns)
    if declared:
        from server.core.ch import get_client

        await connect_code_plane(get_client(), args)  # declares the org it now writes
    while rows:
        n = await _embed_rows(
            repo,
            "code_chunks",
            rows,
            embedder,
            text_of=lambda r: str(r.get("context_text") or r.get("content") or ""),
            max_attempts=settings.code_embed_max_attempts,
            result=result,
        )
        total += n
        print(f"  embedded {total} (+{n}); failed={result.failed} poisoned={result.poisoned}")
        if result.switched_off:
            # Its rows stay as they are, so the next page would be the same one.
            print(f"  {org}'s model calls are switched off — stopping", file=sys.stderr)
            return await _finish(1)
        if n == 0 and result.failed:
            print("  stalling on failures — stopping; re-run to retry", file=sys.stderr)
            return await _finish(1)
        rows = await _pending()
    print(f"embed phase complete: {total} chunks embedded")
    return await _finish(0)
