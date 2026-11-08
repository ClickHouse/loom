"""`loom code backfill`: the full-checkout index pass, its edge and dialect
folder passes, and the embed phase."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import time
from typing import TYPE_CHECKING, Any, Literal

from server.cli.code_maintenance import _compact_after_bulk
from server.cli.code_run import _identity, _note, _repo_cfg
from server.core.background import gather_or_cancel
from server.llm import customer

if TYPE_CHECKING:
    from server.code.app.index.ingest_stats import FileIngestStats

log = logging.getLogger("loom.cli.code")


def _count_record(counters: dict[str, int], recorded: FileIngestStats | Literal[False]) -> None:
    """Count an oversize file's record as `_one` counts an ingested file."""
    key = "errors" if recorded is False else "excluded" if recorded.excluded else "files"
    counters[key] = counters.get(key, 0) + 1


async def _backfill(args: argparse.Namespace) -> int:
    from server.code.app.index import gitrepo
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.settings import settings

    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()

    if args.phase == "embed":
        return await _backfill_embed(repo, org, ns, args)

    cfg = _repo_cfg(args)
    git_dir = cfg.git_dir
    if not await asyncio.to_thread(os.path.isdir, git_dir):
        print(f"git dir {git_dir} missing — run `loom code init`", file=sys.stderr)
        return 2

    head = await _backfill_head(git_dir, cfg, args)
    prefixes = cfg.path_prefixes
    paths_file = getattr(args, "paths_file", "")
    if paths_file:
        listed = await asyncio.to_thread(pathlib.Path(paths_file).read_text, "utf-8")
        wanted = [ln.strip() for ln in listed.splitlines() if ln.strip()]
        paths = [p for p in wanted if p.startswith(prefixes)]
        print(f"repair mode: {len(paths)} of {len(wanted)} listed paths are inside {prefixes}")
    else:
        paths = await _tree_paths_with_coverage(git_dir, head, prefixes)
    if args.limit:
        paths = paths[: args.limit]
    # A source file's edge stage reads its class's header from
    # the header's indexed chunks, so headers are indexed first
    from server.code.app.index.ingest_headers import headers_first

    paths = headers_first(paths)
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

    from server.code.app.index.denylist import load_denylist
    from server.code.app.index.ingest import ingest_file, read_capped

    suffix = str(getattr(args, "embedding_model_suffix", "") or "")
    model_name = get_embedder().model_name + suffix
    if suffix:
        print(f"twin label: rows written under embedding_model={model_name!r}")
    denylist = await load_denylist(cfg, head, gitrepo.show_file)
    cursor_key = f"backfill_cursor:index{suffix}"
    mode = "repair" if paths_file else "twin" if suffix else "backfill"
    done_upto = "" if paths_file else await repo.get_state(org, ns, cursor_key)
    start_idx = int(done_upto) if done_upto.isdigit() else 0
    started = time.monotonic()
    sem = asyncio.Semaphore(max(1, args.concurrency))
    counters = {"files": 0, "chunks": 0, "symbols": 0, "edges": 0, "errors": 0}
    oversize: set[str] = set()  # recorded once; the edge pass reads them again

    async def _source(path: str) -> tuple[str, bytes | None]:
        if path in oversize:
            return path, None
        async with sem:
            source, recorded = await read_capped(
                gitrepo.show_file,
                repo,
                org,
                ns,
                git_dir=cfg.git_dir,
                path=path,
                commit_sha=head,
                embedding_model=model_name,
                denylist=denylist,
            )
        if recorded is not None:
            oversize.add(path)
            _count_record(counters, recorded)
        return path, source

    async def _one(reads: Any, path: str, source: bytes) -> None:
        async with sem:
            try:
                stats = await ingest_file(
                    reads,
                    org,
                    ns,
                    path=path,
                    source=source,
                    commit_sha=head,
                    embedding_model=model_name,
                    embedder=embedder,
                    max_chunk_chars=settings.code_chunk_max_chars,
                    embed_reuse_from=cfg.embed_reuse_from,
                    denylist=denylist,
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

    batch = 200
    for b_start in range(start_idx, len(paths), batch):
        chunk_paths = paths[b_start : b_start + batch]
        held = await _held_batch(
            repo, org, ns, chunk_paths, source=_source, one=_one, denylist=denylist
        )
        if held:
            print(f"backfill stopped at {b_start}/{len(paths)}: {held}", file=sys.stderr)
            # The batches written before the hold are compacted and recorded.
            await _close_index(counters, org, ns, model_name, started, head, mode)
            return 1
        if not paths_file:
            await repo.set_state(org, ns, cursor_key, str(b_start + len(chunk_paths)))
        rate = counters["files"] / max(time.monotonic() - started, 1e-9)
        print(
            f"  [{b_start + len(chunk_paths)}/{len(paths)}] files={counters['files']} "
            f"chunks={counters['chunks']} symbols={counters['symbols']} "
            f"edges={counters['edges']} errors={counters['errors']} "
            f"({rate:.1f} files/s)"
        )

    if second_pass_wanted(
        explicit=bool(getattr(args, "edges_second_pass", False)),
        declined=bool(getattr(args, "no_edges_second_pass", False)),
        paths_file=bool(paths_file),
        done_upto=str(done_upto or ""),
    ):
        if not getattr(args, "edges_second_pass", False):
            print("first full backfill of this namespace: edge second pass implied")
        second = await _second_edge_pass(
            repo,
            org,
            ns,
            paths,
            source=_source,
            denylist=denylist,
            head=head,
            model_name=model_name,
            sem=sem,
        )
        counters["edges_second_pass"] = second["edges"]
        counters["errors"] += second["errors"]
    if getattr(args, "dialect_folder_pass", False):
        folder = await _dialect_folder_pass(repo, org, ns, head)
        counters["dialect_inherited"] = folder["inherited"]
    await _close_index(counters, org, ns, model_name, started, head, mode)
    if paths_file:
        # a targeted repair proves nothing about the rest of the tree: the
        # per-merge watermark must keep walking every commit it has not seen
        print(f"repair complete at {head[:12]} (watermark untouched): {counters}")
        return backfill_index_exit_code(counters["files"], counters["errors"])
    if suffix:
        # a twin label proves nothing about the live label's coverage: the
        # per-merge watermark stays where the live backfill left it
        await repo.set_state(org, ns, cursor_key, "")
        print(f"twin backfill complete at {head[:12]} (watermark untouched): {counters}")
        return backfill_index_exit_code(counters["files"], counters["errors"])
    await repo.set_state(org, ns, "git_head", head, detail="backfill")
    await repo.set_state(org, ns, cursor_key, "")  # done; clear the cursor
    from server.code.app.index.bootstrap import record_backfill_stamps

    await record_backfill_stamps(repo, org, ns)
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


def second_pass_wanted(*, explicit: bool, declined: bool, paths_file: bool, done_upto: str) -> bool:
    """A namespace's FIRST full backfill indexes files in
    tree order, so a call to a function whose file comes later binds
    nothing (a call in fastify.js to `buildRouting(…)` reads `unresolved,
    0 candidates` while lib/route.js is not yet indexed). The edges-only
    second pass is what repairs that, so a first full
    backfill implies it; `--edges-second-pass` asks for it on any run,
    `--no-edges-second-pass` declines the implied one. A resumed run
    (cursor set) or a `--paths-file` repair implies nothing."""
    if explicit:
        return True
    if declined or paths_file:
        return False
    return not done_upto


async def _second_edge_pass(
    repo: Any,
    org: str,
    ns: str,
    paths: list[str],
    *,
    source: Any,
    denylist: Any,
    head: str,
    model_name: str,
    sem: asyncio.Semaphore,
) -> dict[str, int]:
    """The edges-only pass over the same paths once the whole
    tree is indexed. The first pass resolved each file against the name table
    as it stood — a fresh namespace's dbt model indexed before the model it
    `ref()`s bound nothing (bench callers F1 0.286 → 1.0 on the second
    pass). Same rounds as the index phase; chunks, symbols and facets are
    left alone, edges are key-diffed against the live rows."""
    from server.code.app.index.ingest import ingest_file

    started = time.monotonic()
    counters = {"files": 0, "edges": 0, "errors": 0}

    async def _one(reads: Any, path: str, src: bytes) -> None:
        async with sem:
            try:
                stats = await ingest_file(
                    reads,
                    org,
                    ns,
                    path=path,
                    source=src,
                    commit_sha=head,
                    embedding_model=model_name,
                    edges_only=True,
                    denylist=denylist,
                    parsed=reads.parsed.get(path),
                )
            except Exception as e:  # noqa: BLE001 — per-file, keep going
                counters["errors"] += 1
                print(f"  ! second pass {path}: {type(e).__name__}: {e}", file=sys.stderr)
                return
            counters["files"] += 1
            counters["edges"] += stats.edges_new

    print(f"second edge pass: {len(paths)} files against the finished name table")
    for b_start in range(0, len(paths), 200):
        await _backfill_batch_rounds(
            repo,
            org,
            ns,
            paths[b_start : b_start + 200],
            source=source,
            one=_one,
            denylist=denylist,
        )
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
    counters: dict[str, int],
    org: str,
    ns: str,
    model_name: str,
    started: float,
    head: str,
    mode: str,
) -> None:
    """The index phase's end: compact what it wrote and record its pass."""
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


async def _held_batch(
    repo: Any, org: str, ns: str, batch_paths: list[str], *, source: Any, one: Any, denylist: Any
) -> str:
    """One cursor batch as the org's model calls, so its switch applies to them;
    "" once written, else why the switch held it (the cursor stays)."""
    try:
        with customer.for_org(org, namespace=ns):
            await _backfill_batch_rounds(
                repo, org, ns, batch_paths, source=source, one=one, denylist=denylist
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
    from server.code.app.index.round_reads import RoundReads, round_size, rounds

    size = round_size()
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
        await gitrepo.fetch(git_dir, cfg.git_remote, cfg.git_branch, token=cfg.github_token)
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

    async def _finish(code: int) -> int:
        _note(phase="embed", embedded=total, failed=result.failed, poisoned=result.poisoned)
        await _compact_after_bulk(total, embedder.model_name)
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

    while True:
        # --include-poisoned: rows that exhausted their attempts get one more
        # pass (inputs are bounded now).
        rows = await repo.pending_embed_chunks(
            org,
            ns,
            embedder.model_name,
            settings.code_embed_max_attempts,
            500,
            include_poisoned=bool(getattr(args, "include_poisoned", False)),
        )
        if not rows:
            break
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
    print(f"embed phase complete: {total} chunks embedded")
    return await _finish(0)
