"""`loom code` maintenance subcommands: init, reconcile, compaction,
re-embedding, the prefix twin, crossref, the answer cache, descriptions,
stats, exports, instruction lint and repack."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import time
from typing import Any

from server.cli.code_run import _identity, _note, _repo_cfg

log = logging.getLogger("loom.cli.code")


async def _register_after_init(cfg: Any) -> None:
    """`init` registers the namespace (loom.namespaces kind=code,
    maintainers synced) the way an ingest pass would, so a branch added to the
    registry is readable by its maintainers before its first pass. The
    self-service registry is read first: the sync needs it to tell the
    operator's entries from the branches added from the page. Fail-quiet:
    the next ingest pass registers it anyway."""
    try:
        from server.code.app.index.registry import ensure_registered
        from server.code.app.index.repo_registry import refresh_db_repos

        await refresh_db_repos(force=True)
        name = f"{cfg.org}/{cfg.namespace}"
        if await ensure_registered(cfg):
            print(f"registered code namespace {name}")
        else:
            print(
                f"code namespace {name} not registered now "
                "(already registered, or left to the next ingest pass)"
            )
    except Exception as e:  # noqa: BLE001 — registration is also the ingest pass's job
        print(f"namespace registration deferred to the next ingest pass ({type(e).__name__}: {e})")


async def _init(args: argparse.Namespace) -> int:
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    cfg = _repo_cfg(args)
    git_dir = cfg.git_dir
    if not await asyncio.to_thread(os.path.isdir, git_dir):
        from server.code.app.index.bootstrap import clone_mirror
        from server.code.domain.index.state import CLONE_TIMED_OUT
        from server.settings import settings

        print(
            f"cloning {cfg.git_remote} -> {git_dir} (bare; waits for a clone already running, "
            f"then at most {settings.code_bootstrap_clone_timeout_s:.0f}s, "
            "code_bootstrap_clone_timeout_s)…"
        )
        # The worker's clone (bootstrap.clone_mirror): a failed or stopped one leaves no mirror.
        if reason := await clone_mirror(cfg):
            if reason == CLONE_TIMED_OUT:
                reason = "clone timed out (code_bootstrap_clone_timeout_s)"
            print(reason, file=sys.stderr)
            return 1
    else:
        print(f"bare clone already at {git_dir}")

    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()
    await repo.set_state(org, ns, "repo_remote", cfg.git_remote)
    await repo.set_state(org, ns, "repo_branch", cfg.git_branch)
    print(f"seeded code_index_state for ({org}, {ns})")
    await _register_after_init(cfg)
    print("next: loom code backfill --embed   (or --phase index, then --phase embed)")
    return 0


async def _export_pairs(args: argparse.Namespace) -> int:
    from server.code.app.index.retrieval_pairs import export_pairs
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    await get_client().connect()
    org, ns = _identity(args)
    res = await export_pairs(CodeRepo(get_client()), org, ns, days=args.days, out_path=args.out)
    print(
        f"export-pairs: scanned={res.traces_scanned} pairs={res.pairs} "
        f"negative_only={res.negative_only} unmatched={res.skipped_no_match} "
        f"by_route={res.by_route} -> {args.out}"
    )
    return 0


async def _reconcile(args: argparse.Namespace) -> int:
    from server.code.app.index import gitrepo
    from server.code.app.index.reconcile import find_stale_paths, heal
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    await get_client().connect()
    cfg = _repo_cfg(args)
    repo = CodeRepo()
    if not args.no_fetch:
        await gitrepo.fetch(cfg.git_dir, cfg.git_remote, cfg.git_branch, token=cfg.github_token)
    head = await gitrepo.head_sha(cfg.git_dir, cfg.git_branch)
    found = await find_stale_paths(
        repo,
        cfg.git_dir,
        cfg.org,
        cfg.namespace,
        head,
        max_commits=args.max_commits,
    )
    print(
        f"reconcile {cfg.org}/{cfg.namespace} @ {head[:12]}: {found.stale_count} of "
        f"{found.paths_total} paths stale ({found.commits_distinct} distinct chunk commits, "
        f"{found.commits_checked} checked, {found.commits_failed} unknown to the clone"
        + (", TRUNCATED — count is a floor" if found.truncated else "")
        + f", {found.took_ms} ms)"
    )
    for path in found.stale[: args.show]:
        print(f"  {path}")
    if found.stale_count > args.show:
        print(f"  … {found.stale_count - args.show} more")
    if not args.heal or not found.stale:
        return 0
    from server.embedding.embedder import get_embedder

    embedder = get_embedder()
    if "fake" in embedder.model_name:
        print("refusing to heal with the fallback embedder; set OPENAI_API_KEY", file=sys.stderr)
        return 2
    todo = found.stale[: args.limit] if args.limit else found.stale
    print(f"healing {len(todo)} paths with {embedder.model_name} …")
    res = await heal(repo, cfg, head, todo, embedder=embedder)
    print(
        f"healed files={res.files} chunks_new={res.chunks_new} chunks_moved={res.chunks_moved} "
        f"symbols_moved={res.symbols_moved} errors={res.errors}"
    )
    return 0 if res.errors == 0 else 1


async def _reembed(args: argparse.Namespace) -> int:
    from server.code.app.index.reembed_model import reembed_namespace
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.embedding.embedder import get_embedder, get_secondary_embedder

    await get_client().connect()
    org, ns = _identity(args)
    source = args.source_model or get_embedder().model_name
    embedder = get_secondary_embedder(args.embedding_model)
    print(
        f"re-embedding {org}/{ns} {source!r} -> {embedder.model_name!r} ({embedder.dim}-d)"
        + (f" under {args.prefix!r}" if args.prefix else "")
    )

    def progress(stats: Any, after: str) -> None:
        print(
            f"  written={stats.rows_written} skipped={stats.rows_skipped} "
            f"paths={stats.paths_seen} embed={stats.embed_seconds:.0f}s "
            f"insert={stats.insert_seconds:.0f}s @ {after[:60]}",
            flush=True,
        )

    stats = await reembed_namespace(
        CodeRepo(),
        org,
        ns,
        source_model=source,
        embedder=embedder,
        path_prefix=args.prefix,
        batch_rows=args.batch,
        progress=progress,
    )
    print(
        f"done: rows_written={stats.rows_written} rows_skipped={stats.rows_skipped} "
        f"paths={stats.paths_seen} embed={stats.embed_seconds:.1f}s "
        f"insert={stats.insert_seconds:.1f}s"
    )
    await _compact_after_bulk(stats.rows_written, embedder.model_name)
    return 0


async def _vector_twin(args: argparse.Namespace) -> int:
    from server.code.domain.index.prefix_twin import prefix_twins_for
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index.prefix_twin import fill_twin
    from server.core.ch import get_client
    from server.embedding.embedder import get_embedder

    await get_client().connect()
    ch = get_client()
    org, ns = _identity(args)
    label = args.embedding_model
    if not label:
        twins = prefix_twins_for(get_embedder().model_name)
        if not twins:
            print("no prefix twin of the served model in LOOM_CODE_VECTOR_TABLES")
            return 2
        label = twins[0][0]
    targets = [(org, ns)]
    if args.all_namespaces:
        targets = [(c.org, c.namespace) for c in configured_repos() if c.org == org] or targets
    live_total = 0
    for o, n in targets:
        t0 = time.perf_counter()
        live = await fill_twin(ch, o, n, label)
        live_total += live
        print(
            f"{o}/{n}: twin {label!r} live rows={live} ({time.perf_counter() - t0:.1f}s)",
            flush=True,
        )
    # the fill reports live rows, not rows written: an upper bound
    await _compact_after_bulk(live_total, label)
    return 0


def compact_targets(table: str) -> list[str]:
    """`--table` → the tables to compact, in order."""
    from server.code.app.index.compaction import CHUNKS_TABLE, PREFIX_TABLE, resolve_table

    if table == "all":
        return [PREFIX_TABLE, CHUNKS_TABLE]
    return [resolve_table(table)]


async def _compact(args: argparse.Namespace) -> int:
    from server.code.app.index.compaction import describe, maybe_compact
    from server.core.ch import get_client
    from server.settings import settings

    try:
        tables = compact_targets(str(args.table))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    await get_client().connect()
    ch = get_client()
    min_ratio = args.min_ratio if args.min_ratio is not None else settings.code_compact_min_ratio
    rc = 0
    for t in tables:
        res = await maybe_compact(
            ch,
            t,
            min_ratio=min_ratio,
            dry_run=bool(args.dry_run),
            timeout_s=args.timeout_s,
        )
        print(describe(res), flush=True)
        _note(**{f"{t.split('.')[-1]}_ratio": res.ratio, f"{t.split('.')[-1]}_ran": res.ran})
        if res.error or res.timed_out:
            rc = 1
    return rc


async def _compact_after_bulk(rows_written: int, model: str) -> None:
    """A bulk pass that wrote many chunk rows ends by
    compacting the prefix twin (and the model's vector tables). Never
    raises; prints what it did."""
    try:
        from server.code.app.index.compaction import compact_after_bulk, describe
        from server.core.ch import get_client

        for res in await compact_after_bulk(get_client(), rows_written, model):
            print(f"compact: {describe(res)}", flush=True)
    except Exception as e:  # noqa: BLE001 — the pass itself already succeeded
        log.warning("compact after bulk pass skipped: %s", e)


async def _crossref(args: argparse.Namespace) -> int:
    from server.code.domain.extract.crossref import run_crossref_pass
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    await get_client().connect()
    org, ns = _identity(args)
    res = await run_crossref_pass(CodeRepo(), org, ns)
    print(
        f"crossref: registrations={res.registrations} "
        f"test_files_linked={res.test_files_linked} rows={res.rows_written} "
        f"high_df_skipped={len(res.skipped_high_df)}"
    )
    return 0


async def _coverage_import(args: argparse.Namespace) -> int:
    from server.code.app.index.coverage_import import import_from_settings
    from server.core.ch import get_client

    await get_client().connect()
    org, ns = _identity(args)
    res = await import_from_settings(org, ns, dry_run=bool(args.dry_run))
    print(
        f"coverage-import{' (dry run)' if res.dry_run else ''}: namespace={res.namespace} "
        f"commit={res.commit[:12] or '-'} jobs={res.jobs} shards_read={res.shards_read} "
        f"pairs={res.pairs} files={res.files} retired_pairs={res.retired_pairs} "
        f"retired_files={res.retired_files} unmapped_tests={res.unmapped_tests} "
        f"unindexed_files={res.unindexed_files}"
        + (f" skipped={res.skipped}" if res.skipped else "")
    )
    for e in res.errors:
        print(f"  error: {e}", file=sys.stderr)
    return 1 if res.errors else 0


async def _warm(args: argparse.Namespace) -> int:
    import json as _json

    from server.code.app.answers import ask
    from server.code.app.common import resolve_code_thresholds
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.embedding.embedder import get_embedder
    from server.llm.client import get_llm_client, get_llm_client_for_model
    from server.settings import settings

    def _load() -> list[str]:
        out: list[str] = []
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("{"):
                    q = str(_json.loads(line).get("question") or "").strip()
                    if q:
                        out.append(q)
                else:
                    out.append(line)
        return out

    questions = await asyncio.to_thread(_load)
    if not questions:
        print("no questions in file", file=sys.stderr)
        return 2

    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()
    embedder = get_embedder()
    override = settings.code_ask_llm_model
    llm = get_llm_client_for_model(override) if override else get_llm_client()
    abstain_threshold, noise_floor = await resolve_code_thresholds(org, ns)

    hits = misses = abstains = 0
    for i, q in enumerate(questions, 1):
        res = await ask(
            repo,
            org,
            ns,
            question=q,
            embedder=embedder,
            llm=llm,
            search_kwargs={
                "top_k": 10,
                "token_budget": 8000,
                "abstain_threshold": abstain_threshold,
                "noise_floor": noise_floor,
            },
        )
        if res.source == "cached":
            hits += 1
        elif res.abstain:
            abstains += 1
            print(f"  [{i}] ABSTAINED (won't cache): {q[:70]}")
        else:
            misses += 1
        if i % 10 == 0:
            print(f"  {i}/{len(questions)} (new: {misses}, cached: {hits})")
    print(
        f"warm complete: {len(questions)} questions — "
        f"{misses} newly synthesized+cached, {hits} already cached, "
        f"{abstains} abstained (not cached; likely off-index — check wording)"
    )
    _note(questions=len(questions), synthesized=misses, cached=hits, abstained=abstains)
    return 0


_PURGE_PREVIEW = 10  # matched answers a dry run prints


async def _answers_purge(args: argparse.Namespace) -> int:
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()
    where = "" if getattr(args, "all", False) else "AND llm_model = {m:String}"
    rows = await repo._ch.query_all(
        f"""
        SELECT * FROM loom.code_answers FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND tombstoned_at IS NULL {where}
        """,
        {"org": org, "ns": ns, "m": args.llm_model},
    )
    if not rows:
        print("no live cached answers matched")
        return 0
    if args.dry_run:
        print(f"would tombstone {len(rows)} cached answers")
        for r in rows[:_PURGE_PREVIEW]:
            question = " ".join(str(r.get("question") or "").split())[:100]
            print(f"  [{r.get('llm_model') or '-'}] {question}")
        if len(rows) > _PURGE_PREVIEW:
            print(f"  … and {len(rows) - _PURGE_PREVIEW} more")
        return 0
    await repo.insert_rows("code_answers", [CodeRepo.tombstone_of("code_answers", r) for r in rows])
    print(
        f"tombstoned {len(rows)} cached answers"
        + (f" (llm_model={args.llm_model})" if args.llm_model else " (all)")
    )
    return 0


async def _describe(args: argparse.Namespace) -> int:
    """Generate contextual one-liners for chunks in the top PageRank files
    and re-embed them (context_text changes with the description).
    Parallel across chunks (semaphore-bounded); resumable — described
    chunks are skipped by context_source."""
    from server.code.app.index.describe import _card_for, _describe_chunk
    from server.code.domain.extract.chunker import Chunk
    from server.code.domain.extract.context_text import build_context_text, with_description
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.embedding.embedder import get_embedder
    from server.llm.client import get_llm_client

    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()
    embedder = get_embedder()
    llm = get_llm_client()

    ranked = await repo.top_summaries(org, ns, embedder.model_name, limit=100000)
    n_files = max(1, int(len(ranked) * args.top_pct / 100.0))
    target_paths = [str(r["scope_path"]) for r in ranked[:n_files]]
    if not ranked:
        # a first index has no file summaries yet (their worker runs later): every
        # indexed file, in path order, rather than nothing
        everything = await repo.live_paths_under(org, ns, "", limit=1_000_000)
        target_paths = everything[: max(1, int(len(everything) * args.top_pct / 100.0))]
    if args.suffix:
        target_paths = [p for p in target_paths if p.endswith(args.suffix)]
    print(
        f"describing chunks in {len(target_paths)} files "
        f"(top {args.top_pct}% by PageRank"
        + (f", suffix {args.suffix}" if args.suffix else "")
        + ")"
    )

    sem = asyncio.Semaphore(max(1, args.concurrency))
    done = 0
    stop = False

    async def _one(path: str, live: dict[str, Any], card: str) -> None:
        nonlocal done, stop
        if stop:
            return
        async with sem:
            chunk = Chunk(
                path=path,
                scope=str(live.get("scope") or ""),
                kind=str(live.get("kind")),
                start_line=int(live.get("start_line") or 0),
                end_line=int(live.get("end_line") or 0),
                content=str(live.get("content") or ""),
            )
            desc = await _describe_chunk(llm, chunk, card=card)
            if not desc:
                return
            # the ingest's own head lines (signature, calls, callers) stay
            ctx = (
                with_description(
                    str(live.get("context_text") or ""), scope=chunk.scope, description=desc
                )
                if live.get("context_text")
                else build_context_text(
                    path=path, scope=chunk.scope, description=desc, content=chunk.content
                )
            )
            try:
                vec = await embedder.embed(ctx)
            except Exception as e:  # noqa: BLE001 — one chunk's embed failure skips that chunk
                print(f"  ! embed failed for {path}#{chunk.scope}: {e}", file=sys.stderr)
                return
            row = CodeRepo.moved_of(
                "code_chunks",
                live,
                context_description=desc,
                context_source="llm",
                context_text=ctx,
                embedding=vec,
                embedding_dim=len(vec),
            )
            await repo.insert_rows("code_chunks", [row])
            done += 1
            if done % 200 == 0:
                print(f"  {done} chunks described", flush=True)
            if args.limit and done >= args.limit:
                stop = True

    # files are read and described several at a time: one file's reads are a
    # round trip each, so a serial walk of a large repository is read-bound
    files = asyncio.Semaphore(max(1, args.concurrency))

    async def _file(path: str) -> None:
        if stop:
            return
        async with files:
            rows = await repo.live_chunks_for_path(org, ns, path, embedder.model_name)
            pending = [
                r
                for r in rows
                if str(r.get("context_source")) != "llm"
                and str(r.get("kind")) in ("function", "class_skeleton")
            ]
            if not pending:
                return
            card = await _card_for(repo, org, ns, embedder.model_name, path, True)
        await asyncio.gather(*(_one(path, r, card) for r in pending))

    await asyncio.gather(*(_file(p) for p in target_paths))
    print(f"describe complete: {done} chunks")
    await _compact_after_bulk(done, embedder.model_name)
    _note(described=done, files=len(target_paths))
    return 0


async def _export_matrix(args: argparse.Namespace) -> int:
    import dataclasses
    import json

    from server.code.app.index.matrix import co_change_pairs, render_matrix
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()
    state = await repo.all_state(org, ns)
    head = str((state.get("git_head") or {}).get("value") or "")
    pairs = await co_change_pairs(
        repo,
        org,
        ns,
        prs=args.prs,
        min_together=args.min_together,
        min_conf=args.min_conf,
    )
    if args.json:
        text = json.dumps([dataclasses.asdict(p) for p in pairs], indent=2)
    else:
        text = render_matrix(
            pairs,
            prs=args.prs,
            head=head,
            max_partners=args.max_partners,
            max_rows=args.max_rows,
        )
    if args.out:
        await asyncio.to_thread(pathlib.Path(args.out).write_text, text, encoding="utf-8")
        print(f"wrote {len(pairs)} pairs to {args.out}", file=sys.stderr)
    else:
        print(text, end="")
    return 0


async def _lint_instructions(args: argparse.Namespace) -> int:
    import json

    from server.code.app.index.instructions_lint import lint_repo, render_report

    cfg = _repo_cfg(args)
    repo = None
    if args.symbols:
        from server.code.store.index import CodeRepo
        from server.core.ch import get_client

        await get_client().connect()
        repo = CodeRepo()
    report = await lint_repo(
        cfg,
        files=list(args.file),
        symbols=args.symbols,
        repo=repo,
        globs=list(args.glob),
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_report(report, stale_only=not args.all))
    return 1 if (args.fail_on_stale and report.stale_count) else 0


async def _stats(args: argparse.Namespace) -> int:
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()
    state = await repo.all_state(org, ns)
    counts = await repo.table_counts(org, ns)
    print(f"code index ({org}, {ns})")
    for k, v in sorted(state.items()):
        print(f"  {k:24} {str(v['value'])[:60]}  ({v['updated_at']})")
    for t, n in counts.items():
        print(f"  {t:24} {n:,} live rows")
    return 0


async def _merge_diff(git_dir: str, sha: str) -> str | None:
    """`git diff <sha>^1 <sha>` in the bare clone; None when git fails or
    times out (the commit is not in the clone, a shallow fetch), and for a
    stored sha that is not a hex object name: it goes on git's argv, where
    a leading `-` would be read as an option. Runs through the code index's
    git runner, which kills and reaps git when the command is cancelled."""
    import re

    from server.code.app.index import gitrepo

    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        print(f"  not a commit sha: {sha[:20]!r}; PR skipped", file=sys.stderr)
        return None
    try:
        return await gitrepo.run(git_dir, "diff", f"{sha}^1", sha, timeout_s=120)
    except gitrepo.GitError:
        print(f"  git diff failed for {sha[:12]}; PR skipped", file=sys.stderr)
        return None


async def _repack(args: argparse.Namespace) -> int:
    """Rebuild the precomputed pack for the last N merged PRs so every pack a
    bot fetches has the current shape (touched symbols + callers, co-change
    gaps, files last). Changed symbols come from the functions git names in
    each PR's hunk headers (drift-proof) plus span overlap, like code.impact."""
    from server.code.app.index.packs import build_pr_pack, estimate_tokens
    from server.code.app.symbol import _name_matches, _parse_unified_diff_full
    from server.code.store.index import CodeRepo, build_row
    from server.core.ch import get_client

    cfg = _repo_cfg(args)
    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()
    prs = await repo.recent_prs(org, ns, days=args.days, limit=args.limit)
    print(f"repack: {len(prs)} merged PRs in the last {args.days} days", file=sys.stderr)
    done = skipped = 0
    for pr in prs:
        raw = pr["merge_commit_sha"]
        sha = raw.decode().rstrip("\x00") if isinstance(raw, bytes) else str(raw)
        files = [str(f) for f in (pr.get("files") or [])]
        files = [f for f in files if any(f.startswith(p) for p in cfg.path_prefixes)]
        if not files or not sha:
            skipped += 1
            continue
        diff = await _merge_diff(cfg.git_dir, sha)
        if diff is None:
            # No hunks would read as "every function in these files changed".
            skipped += 1
            continue
        hunks, names = _parse_unified_diff_full(diff)
        rows = await repo.functions_for_paths(org, ns, files[:60], limit=400)
        touched: list[dict[str, Any]] = []
        for r in rows:
            p = str(r["path"])
            spans, hn = hunks.get(p), names.get(p) or set()
            overlaps = any(
                s <= int(r["end_line"] or 0) and e >= int(r["start_line"] or 0)
                for s, e in (spans or [])
            )
            named = any(_name_matches(str(r["qualified_name"]), n) for n in hn)
            if spans and not (named or overlaps):
                continue
            touched.append(r)
        seen: set[tuple[str, str]] = set()
        uniq: list[dict[str, Any]] = []
        for r in sorted(touched, key=lambda r: 0 if str(r["tier"]) == "scip" else 1):
            k = (str(r["path"]), str(r["qualified_name"]))
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        pack = await build_pr_pack(
            repo,
            org,
            ns,
            pr_number=int(pr["pr_number"]),
            title=str(pr["title"]),
            commit_sha=sha,
            changed_paths=files,
            changed_symbols=uniq[:30],
        )
        if args.dry_run:
            print(f"  #{pr['pr_number']}: {len(uniq)} symbols, {estimate_tokens(pack)} tokens")
        else:
            await repo.insert_rows(
                "code_context_packs",
                [
                    build_row(
                        "code_context_packs",
                        org=org,
                        namespace=ns,
                        pr_number=int(pr["pr_number"]),
                        merge_commit_sha=sha,
                        pack=pack,
                        pack_tokens=estimate_tokens(pack),
                    )
                ],
            )
        done += 1
        if done % 25 == 0:
            print(f"  … {done} packs", file=sys.stderr)
    verb = "rendered" if args.dry_run else "written"
    print(f"repack: {done} packs {verb}, {skipped} PRs had no indexed files")
    _note(packs=done, skipped=skipped, prs=len(prs), dry_run=bool(args.dry_run))
    return 0
