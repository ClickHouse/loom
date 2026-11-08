"""One commit's ingest: its changed files in rounds, then the unit-risk refresh, the
merged PR's pack and brief, and the dependents the change re-resolves."""

from __future__ import annotations

import logging
import time
from typing import Any

from server.code.app.index.gitrepo import BlobTooLarge
from server.code.app.index.ingest import record_oversize
from server.code.app.index.pass_budget import _PR_NUMBER_RE, CodeIngestResult
from server.settings import settings

logger = logging.getLogger("loom.workers.code_ingest")


async def _ingest_commit(
    repo: Any,
    org: str,
    ns: str,
    git_dir: str,
    base_sha: str,
    sha: str,
    *,
    embedder: Any,
    llm: Any,
    describe: bool,
    prefixes: tuple[str, ...],
    result: CodeIngestResult,
    build_pack: Any,
    estimate_tokens: Any,
    build_row: Any,
    gitrepo: Any,
    ingest_file: Any,
    tombstone_file: Any,
    embed_reuse_from: str = "",
    denylist: Any = None,
    build_brief: Any = None,
    skip: int = 0,
    clock: Any = None,
    on_round: Any = None,
) -> int | None:
    """None when the commit landed whole; else how many of its files are
    durable: a commit too big for one pass is ingested in
    round slices, each flushed before the next starts, and the caller keeps
    the watermark until the last slice lands."""
    entries = await gitrepo.diff_files(git_dir, base_sha, sha)
    entries = [e for e in entries if e.path.startswith(prefixes)]
    # `Foo.h` before `Foo.cpp`, so the source's edge stage
    # reads the header's chunks as this commit leaves them
    from server.code.app.index.ingest_headers import HEADER_EXTS

    entries.sort(key=lambda e: 0 if e.path.endswith(HEADER_EXTS) else 1)
    if len(entries) > settings.code_ingest_max_files_per_commit:
        logger.warning(
            "code_ingest: commit %s touches %d files (cap %d) — indexing the cap, "
            "rest heal on next backfill",
            sha[:12],
            len(entries),
            settings.code_ingest_max_files_per_commit,
        )
        entries = entries[: settings.code_ingest_max_files_per_commit]

    changed_paths, changed_symbols, file_stats, done = await _ingest_commit_files(
        repo,
        org,
        ns,
        git_dir,
        sha,
        entries,
        embedder=embedder,
        llm=llm,
        describe=describe,
        embed_reuse_from=embed_reuse_from,
        denylist=denylist,
        result=result,
        gitrepo=gitrepo,
        ingest_file=ingest_file,
        tombstone_file=tombstone_file,
        skip=skip,
        clock=clock,
        on_round=on_round,
    )

    await _resolve_dependents(
        repo,
        org,
        ns,
        git_dir,
        sha,
        file_stats,
        set(changed_paths),
        embedding_model=embedder.model_name,
        gitrepo=gitrepo,
        ingest_file=ingest_file,
        result=result,
    )
    await _refresh_unit_risk(
        repo, org, ns, embedder.model_name, sha, changed_paths, file_stats, result
    )
    if done < len(entries):
        return done  # This slice's files and dependents are durable
    # The pack names every file of the commit, whichever slice ingested it.
    changed_paths = [e.path for e in entries]

    # Context pack when the squash-merge subject names a PR (#NNNNN).
    subject = await gitrepo.commit_subject(git_dir, sha)
    # Last #N in the subject wins: "Merge pull request #N" and "(#N)" both
    # put the PR number at the end.
    matches = _PR_NUMBER_RE.findall(subject)
    if matches and changed_paths:
        pr_number = int(matches[-1])
        pack = await build_pack(
            repo,
            org,
            ns,
            pr_number=pr_number,
            title=subject,
            commit_sha=sha,
            changed_paths=changed_paths,
            changed_symbols=changed_symbols,
        )
        await repo.insert_rows(
            "code_context_packs",
            [
                build_row(
                    "code_context_packs",
                    org=org,
                    namespace=ns,
                    pr_number=pr_number,
                    merge_commit_sha=sha,
                    pack=pack,
                    pack_tokens=estimate_tokens(pack),
                )
            ],
        )
        result.packs_built += 1
        await _brief_after_pack(
            build_brief,
            repo,
            org,
            ns,
            git_dir=git_dir,
            pr_number=pr_number,
            title=subject,
            merge_sha=sha,
            parent_sha=base_sha,
            changed_paths=changed_paths,
            result=result,
        )
    return None


async def _ingest_commit_files(
    repo: Any,
    org: str,
    ns: str,
    git_dir: str,
    sha: str,
    entries: list[Any],
    *,
    embedder: Any,
    llm: Any,
    describe: bool,
    embed_reuse_from: str,
    denylist: Any,
    result: CodeIngestResult,
    gitrepo: Any,
    ingest_file: Any,
    tombstone_file: Any,
    skip: int = 0,
    clock: Any = None,
    on_round: Any = None,
) -> tuple[list[str], list[dict[str, Any]], list[Any], int]:
    """The commit's files, in rounds: each round's per-file
    resolution reads are answered from two round-wide statements; the order
    (headers first) and the per-file sequence are unchanged. Returns the
    changed paths, the changed symbols (for the pack) and the per-file stats
    (the commit's name-table delta) and how many of the
    commit's files are durable. The first `skip` files landed
    in an earlier pass; a round starts only if `clock` says it can finish
    before the pass deadline, so the pass stops between rounds (each flushed)
    and never dies holding unsaved work."""
    from server.code.app.index.round_reads import RoundReads, round_size, rounds

    changed_paths: list[str] = []
    changed_symbols: list[dict[str, Any]] = []
    file_stats: list[Any] = []
    done = min(max(0, skip), len(entries))
    result.files_planned += len(entries) - done
    size = round_size()
    for round_entries in rounds(entries[done:], size):
        if clock is not None and not clock.can_start(len(round_entries)):
            break
        round_started = time.monotonic()
        sources: dict[str, bytes] = {}
        oversize: dict[str, BlobTooLarge] = {}
        for entry in round_entries:
            if entry.status != "D":
                try:
                    src = await gitrepo.show_file(
                        git_dir, sha, entry.path, max_bytes=settings.code_ingest_max_file_bytes
                    )
                except BlobTooLarge as e:
                    oversize[entry.path] = e
                    continue
                if src is not None:
                    sources[entry.path] = src
        reads = RoundReads(repo, org, ns)
        if size > 0:
            await reads.prefetch(sources, denylist)
            await _prefetch_round_models(
                reads,
                org,
                ns,
                sources,
                embedder=embedder,
                llm=llm if describe else None,
                embed_reuse_from=embed_reuse_from,
            )
        try:
            await _ingest_round_files(
                reads,
                org,
                ns,
                sha,
                round_entries,
                sources,
                oversize=oversize,
                embedder=embedder,
                llm=llm,
                describe=describe,
                embed_reuse_from=embed_reuse_from,
                denylist=denylist,
                result=result,
                ingest_file=ingest_file,
                tombstone_file=tombstone_file,
                changed_paths=changed_paths,
                changed_symbols=changed_symbols,
                file_stats=file_stats,
            )
        finally:
            await reads.flush()  # The round's buffered rows
        result.read_hits += reads.hits
        result.read_misses += reads.misses
        done += len(round_entries)
        if clock is not None:
            clock.observe(len(round_entries), time.monotonic() - round_started)
        if on_round is not None and done < len(entries):
            await on_round(done)  # the round is flushed: a kill now loses nothing
    return changed_paths, changed_symbols, file_stats, done


async def _prefetch_round_models(
    reads: Any,
    org: str,
    ns: str,
    sources: dict[str, bytes],
    *,
    embedder: Any,
    llm: Any,
    embed_reuse_from: str,
) -> None:
    """The round's descriptions and embeddings, made together.
    Skipped where a file's embedding text is not the round's to know: a
    namespace that copies vectors from another (embed_reuse_from) or a callee
    outline read from the index at the file's turn."""
    from server.code.app.index.round_models import prefetch_models

    await prefetch_models(
        reads,
        org,
        ns,
        sources,
        llm=llm,
        embedder=embedder,
        embedding_model=embedder.model_name,
        max_chunk_chars=settings.code_chunk_max_chars,
        embed=not embed_reuse_from,
    )


async def _ingest_round_files(
    reads: Any,
    org: str,
    ns: str,
    sha: str,
    round_entries: list[Any],
    sources: dict[str, bytes],
    *,
    oversize: dict[str, BlobTooLarge],
    embedder: Any,
    llm: Any,
    describe: bool,
    embed_reuse_from: str,
    denylist: Any,
    result: CodeIngestResult,
    ingest_file: Any,
    tombstone_file: Any,
    changed_paths: list[str],
    changed_symbols: list[dict[str, Any]],
    file_stats: list[Any],
) -> None:
    """One round's files in order (headers first), each through `ingest_file`; a file
    over the byte cap gets its verdict from its size, unread."""
    for entry in round_entries:
        result.files_done += 1
        changed_paths.append(entry.path)
        if entry.status == "D":
            file_stats.append(
                await tombstone_file(
                    reads,
                    org,
                    ns,
                    path=entry.path,
                    embedding_model=embedder.model_name,
                )
            )
            continue
        source = sources.get(entry.path)
        if entry.path in oversize:
            stats = await record_oversize(
                reads,
                org,
                ns,
                path=entry.path,
                too_large=oversize[entry.path],
                commit_sha=sha,
                embedding_model=embedder.model_name,
                denylist=denylist,
            )
        elif source is None:
            continue
        else:
            stats = await ingest_file(
                reads,
                org,
                ns,
                path=entry.path,
                source=source,
                commit_sha=sha,
                embedding_model=embedder.model_name,
                embedder=embedder,
                llm=llm,
                max_chunk_chars=settings.code_chunk_max_chars,
                describe=describe,
                embed_reuse_from=embed_reuse_from,
                denylist=denylist,
                parsed=reads.parsed.get(entry.path),
            )
        if stats.excluded:
            result.files_excluded += 1
            continue
        if stats.skipped:
            continue
        _count_file(result, stats, changed_symbols)
        file_stats.append(stats)


def _count_file(
    result: CodeIngestResult,
    stats: Any,
    changed_symbols: list[dict[str, Any]],
) -> None:
    result.files_indexed += 1
    if stats.error:
        result.files_errored += 1
    result.chunks_new += stats.chunks_new
    result.chunks_kept += stats.chunks_kept
    result.embedded += stats.embedded
    result.embed_stubs += stats.embed_stubs
    result.symbols_new += stats.symbols_new
    result.edges_new += stats.edges_new
    result.described += stats.described
    # only symbols in files that actually changed matter for the pack
    changed_symbols.extend(stats.symbol_summaries[:20])


async def _refresh_unit_risk(
    repo: Any,
    org: str,
    ns: str,
    model: str,
    sha: str,
    changed_paths: list[str],
    file_stats: list[Any],
    result: CodeIngestResult,
) -> None:
    """After a commit's files and their dependents are
    durable, rewrite the precomputed risk rows of the changed files and of
    the files their calls reach (their amp moved). Never fatal to the
    commit — a unit without a fresh row is annotated live."""
    from server.code.app import unit_risk as code_unit_risk

    if not changed_paths:
        return
    callee_ids = sorted(
        {sid for fs in file_stats for sid in (getattr(fs, "callee_symbol_ids", None) or [])}
    )
    try:
        result.unit_risk_rows += await code_unit_risk.refresh_for_commit(
            repo, org, ns, model, changed_paths, callee_ids
        )
    except Exception as e:  # noqa: BLE001 — the live reads serve until the weekly pass
        result.unit_risk_failed += 1
        result.errors.append(f"{sha[:12]} unit_risk: {type(e).__name__}: {e}")
        logger.warning("code_ingest: unit risk refresh for %s failed: %s", sha[:12], e)


async def _brief_after_pack(
    build_brief: Any,
    repo: Any,
    org: str,
    ns: str,
    *,
    git_dir: str,
    pr_number: int,
    title: str,
    merge_sha: str,
    parent_sha: str,
    changed_paths: list[str],
    result: CodeIngestResult,
) -> None:
    """The review brief rides the pack's trigger and inputs.
    Never fatal to the commit — the pack and the index are durable, and a
    brief the ingest did not write is built on its first read."""
    if build_brief is None:
        return
    try:
        await build_brief(
            repo,
            org,
            ns,
            git_dir=git_dir,
            pr_number=pr_number,
            title=title,
            merge_sha=merge_sha,
            parent_sha=parent_sha,
            changed_paths=changed_paths,
        )
        result.briefs_built += 1
    except Exception as e:  # noqa: BLE001 — a missing brief is a miss, built on read
        result.briefs_failed += 1
        logger.warning("code_ingest: review brief for PR %s failed: %s", pr_number, e)


async def _resolve_dependents(
    repo: Any,
    org: str,
    ns: str,
    git_dir: str,
    sha: str,
    file_stats: list[Any],
    touched: set[str],
    *,
    embedding_model: str,
    gitrepo: Any,
    ingest_file: Any,
    result: CodeIngestResult,
) -> None:
    """After a commit's touched files: bind declined sites
    whose name just gained its one definition, then re-extract edges for the
    untouched files whose edges or declined sites depend on what this commit
    added, removed or retired. Bounded per commit; failures are recorded,
    never fatal to the commit (its own files are already durable)."""
    if not (settings.code_ingest_dependents_enabled or settings.code_closure_repair_enabled):
        return
    from server.code.app.index.dependents import CommitDelta, run_for_commit

    delta = CommitDelta.from_stats(file_stats, touched)
    if delta.empty:
        return
    try:
        closure, deps = await run_for_commit(
            repo,
            org,
            ns,
            delta=delta,
            commit_sha=sha,
            git_dir=git_dir,
            show_file=gitrepo.show_file,
            ingest_file=ingest_file,
            embedding_model=embedding_model,
            dependents_enabled=settings.code_ingest_dependents_enabled,
            closure_enabled=settings.code_closure_repair_enabled,
            max_files=settings.code_ingest_dependents_max_files,
            pending_max=settings.code_ingest_dependents_pending_max,
            closure_max_names=settings.code_closure_repair_max_names,
            closure_max_sites=settings.code_closure_repair_max_sites,
        )
    except Exception as e:
        result.errors.append(f"{sha[:12]} dependents: {type(e).__name__}: {e}")
        logger.exception("code_ingest: dependents for %s failed", sha[:12])
        return
    if closure is not None:
        result.closure_resolved += closure.resolved
        result.closure_ms += closure.ms
    if deps is not None:
        result.dependents_reingested += deps.reingested
        result.dependents_queued += deps.queued
        result.dependents_ms += deps.ms
        result.edges_new += deps.edges_new
        if deps.errors:
            result.errors.append(f"{sha[:12]} dependents: {deps.errors} path(s) failed")
    if (closure and closure.resolved) or (deps and (deps.candidates or deps.queued)):
        logger.info(
            "code_ingest: %s dependents: %d candidate paths %s, re-extracted %d "
            "(edges +%d/-%d, misses +%d/-%d/~%d), queued %d (pending %d, dropped %d) in %d ms; "
            "closure: %d/%d names unique, %d sites bound, %d left declined in %d ms",
            sha[:12],
            deps.candidates if deps else 0,
            deps.by_source if deps else {},
            deps.reingested if deps else 0,
            deps.edges_new if deps else 0,
            deps.edges_tombstoned if deps else 0,
            deps.misses_new if deps else 0,
            deps.misses_tombstoned if deps else 0,
            deps.misses_refreshed if deps else 0,
            deps.queued if deps else 0,
            deps.pending_total if deps else 0,
            deps.dropped if deps else 0,
            deps.ms if deps else 0,
            closure.names_unique if closure else 0,
            closure.names if closure else 0,
            closure.resolved if closure else 0,
            closure.left_declined if closure else 0,
            closure.ms if closure else 0,
        )
