"""First index of a self-service repository.

The ingest worker calls these from its per-repository pass for a registry
entry (``cfg.source == "registry"``): clone the bare mirror when it is
missing, then build the initial index a slice at a time (``code_bootstrap_
files_per_pass``) with a cursor in code_index_state, so a restart resumes
and one large repository never monopolises a pass. Progress is written as
``bootstrap_stage`` (queued → cloning → indexing → ready | error) with a
JSON detail the Repositories card renders.

A repository is ready when it is searchable. A slice embeds inline when a real embedder
is up (fake provider → stubs; a slice whose every embed failed opens a process-local
breaker for ten minutes so the bootstrap never waits on a dead provider); the last slice
moves the entry to ``embedding`` until the vector leg covers
``code_ready_embed_coverage`` of the chunks, and only then to ``ready``. The detail
carries an ETA while indexing and the coverage while embedding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import time
from pathlib import Path
from typing import Any

from server.settings import settings

logger = logging.getLogger("loom.code_index.bootstrap")

STAGE_KEY = "bootstrap_stage"
CURSOR_KEY = "backfill_cursor:index"
# Stages during which the entry is still being built (the recovery worker
# takes its larger batch, status reads shorten their count cache).
BUILDING_STAGES = frozenset({"queued", "cloning", "indexing", "embedding"})

# After a slice in which the provider answered nothing, the next slices store stubs (the
# recovery worker fills them) rather than pay a timeout per file; the breaker closes by
# itself after the hold.
_EMBED_BREAKER_HOLD_S = 600.0
_embed_breaker_until = 0.0


def embedding_coverage(embeddable: int, embedded: int) -> float:
    """Share of embeddable chunks that carry a vector, 0..1. A namespace with
    no embeddable chunk is fully covered (nothing is waiting)."""
    if embeddable <= 0:
        return 1.0
    return max(0.0, min(1.0, embedded / embeddable))


def is_searchable(coverage: float) -> bool:
    """`ready` means the vector leg answers for at least `code_ready_embed_coverage` of
    the chunks."""
    return coverage >= settings.code_ready_embed_coverage


def eta_text(remaining_files: int, files_per_s: float) -> str:
    """ "~38 min left" from the files still to index and the measured rate;
    "" when there is no rate to divide by (the first slice, a stalled one)."""
    if files_per_s <= 0 or remaining_files <= 0:
        return ""
    minutes = max(1, math.ceil(remaining_files / files_per_s / 60))
    if minutes < 60:
        return f"~{minutes} min left"
    hours, rest = divmod(minutes, 60)
    return f"~{hours} h {rest} min left" if rest else f"~{hours} h left"


def embed_breaker_open(now: float | None = None) -> bool:
    return (now if now is not None else time.monotonic()) < _embed_breaker_until


def open_embed_breaker(now: float | None = None) -> None:
    global _embed_breaker_until
    _embed_breaker_until = (now if now is not None else time.monotonic()) + _EMBED_BREAKER_HOLD_S


def reset_embed_breaker() -> None:
    global _embed_breaker_until
    _embed_breaker_until = 0.0


def bootstrap_embedder() -> Any | None:
    """The embedder a slice embeds with, or None to store stubs: None when
    the configured provider is the fake (a vector of n-gram hashes is worse
    than a stub the recovery worker will fill once a real provider is set)
    and while the breaker is open."""
    from server.embedding.embedder import get_embedder
    from server.embedding.fake import FakeNGramEmbedder

    if embed_breaker_open():
        return None
    embedder = get_embedder()
    if isinstance(embedder, FakeNGramEmbedder):
        return None
    return embedder


async def set_stage(repo: Any, org: str, ns: str, stage: str, **detail: Any) -> None:
    await repo.set_state(org, ns, STAGE_KEY, stage, detail=json.dumps(detail) if detail else "")


async def clone_if_missing(cfg: Any, repo: Any) -> bool:
    """True when the mirror exists after the call. A failed clone records
    stage=error with the reason and returns False; the next pass retries."""
    from server.code.app.index.gitrepo import auth_env, remote_access, upload_pack_args
    from server.code.app.index.sandbox import SandboxRequiredError, run_job

    git_dir = cfg.git_dir
    if await asyncio.to_thread(os.path.isdir, git_dir):
        return True
    await set_stage(repo, cfg.org, cfg.namespace, "cloning", remote=cfg.git_remote)
    await asyncio.to_thread(os.makedirs, os.path.dirname(git_dir) or ".", exist_ok=True)
    tmp = git_dir + ".partial"
    # The clone runs as a repository job: the empty target its one writable path.
    await asyncio.to_thread(shutil.rmtree, tmp, True)
    await asyncio.to_thread(os.makedirs, tmp)
    try:
        done = await run_job(
            ["git", "clone", "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
            + ["--bare", "--quiet", *upload_pack_args(cfg.git_remote), cfg.git_remote, tmp],
            env=auth_env(cfg.github_token, cfg.git_remote),
            timeout_s=settings.code_bootstrap_clone_timeout_s,
            writable=[Path(tmp)],
            **remote_access(cfg.git_remote),
        )
    except TimeoutError:
        await set_stage(
            repo, cfg.org, cfg.namespace, "error", reason="clone timed out; retrying next pass"
        )
        return False
    except SandboxRequiredError as e:
        await set_stage(repo, cfg.org, cfg.namespace, "error", reason=str(e)[:300])
        return False
    err = done.stderr
    if done.returncode != 0:
        reason = err.decode(errors="replace").strip().splitlines()[-1:] or ["git clone failed"]
        await set_stage(repo, cfg.org, cfg.namespace, "error", reason=reason[0][:300])
        logger.warning("bootstrap: clone of %s failed: %s", cfg.label, reason[0][:300])
        return False
    await asyncio.to_thread(os.rename, tmp, git_dir)
    if cfg.classification == "restricted":
        await asyncio.to_thread(os.chmod, git_dir, 0o700)
    await repo.set_state(cfg.org, cfg.namespace, "repo_remote", cfg.git_remote)
    await repo.set_state(cfg.org, cfg.namespace, "repo_branch", cfg.git_branch)
    logger.info("bootstrap: cloned %s -> %s", cfg.git_remote, git_dir)
    return True


async def index_slice(
    cfg: Any,
    repo: Any,
    head: str,
    *,
    max_files: int | None = None,
    stop_after: float | None = None,
) -> dict[str, Any]:
    """Index the next ``max_files`` paths of ``head``; on the last slice set the
    git_head watermark and the extractor stamps and mark the entry ready.
    Returns the progress it wrote.

    The slice goes in rounds of `code_ingest_round_files` (the per-file resolution reads
    served round-wide); `stop_after` (a monotonic instant) is checked before every round
    — past it no further round starts and the cursor advances by the paths the started
    rounds took on, so the next pass continues there (`partial` on the progress)."""
    from server.code.app.index import gitrepo
    from server.code.app.index.denylist import load_denylist
    from server.code.app.index.ingest import ingest_file
    from server.code.app.index.ingest_headers import headers_first
    from server.embedding.embedder import get_embedder

    org, ns = cfg.org, cfg.namespace
    limit = max_files or settings.code_bootstrap_files_per_pass
    paths = headers_first(await gitrepo.ls_tree(cfg.git_dir, head, cfg.path_prefixes))
    cursor = await repo.get_state(org, ns, CURSOR_KEY)
    start = int(cursor) if cursor.isdigit() else 0
    total = len(paths)
    if start >= total:
        await _finish(repo, org, ns, head, total)
        return {"done": total, "total": total, "stage": "ready"}

    model_name = get_embedder().model_name
    embedder = bootstrap_embedder()
    denylist = await load_denylist(cfg, head, gitrepo.show_file)
    sem = asyncio.Semaphore(4)
    counters = {"files": 0, "errors": 0, "embedded": 0, "embed_stubs": 0}
    slice_paths = paths[start : start + limit]

    async def _source(path: str) -> tuple[str, bytes | None]:
        async with sem:
            return path, await gitrepo.show_file(cfg.git_dir, head, path)

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
                    denylist=denylist,
                    parsed=reads.parsed.get(path),
                )
                counters["files"] += 1
                counters["embedded"] += int(getattr(stats, "embedded", 0) or 0)
                counters["embed_stubs"] += int(getattr(stats, "embed_stubs", 0) or 0)
            except Exception as e:  # noqa: BLE001 — per-file, keep going
                counters["errors"] += 1
                logger.warning("bootstrap: %s %s: %s", cfg.label, path, e)

    started = time.monotonic()
    attempted, partial = await _slice_rounds(
        repo,
        org,
        ns,
        slice_paths,
        source=_source,
        one=_one,
        denylist=denylist,
        stop_after=stop_after,
    )
    done = start + attempted
    await repo.set_state(org, ns, CURSOR_KEY, str(done))
    rate = counters["files"] / max(time.monotonic() - started, 1e-9)
    _note_embed_outcome(cfg, embedder, counters)
    if done >= total:
        stage = await _finish(repo, org, ns, head, total)
        return {"done": total, "total": total, "stage": stage}
    await set_stage(
        repo,
        org,
        ns,
        "indexing",
        done=done,
        total=total,
        files_per_s=round(rate, 1),
        eta=eta_text(total - done, rate),
        errors=counters["errors"],
        embedded=counters["embedded"],
        embed_stubs=counters["embed_stubs"],
    )
    progress: dict[str, Any] = {"done": done, "total": total, "stage": "indexing"}
    if partial:
        progress["partial"] = True
    return progress


async def _slice_rounds(
    repo: Any,
    org: str,
    ns: str,
    slice_paths: list[str],
    *,
    source: Any,
    one: Any,
    denylist: Any,
    stop_after: float | None,
) -> tuple[int, bool]:
    """The slice in rounds of `code_ingest_round_files`, the budget checked before each;
    returns (paths attempted, cut by the budget)."""
    from server.code.app.index.round_reads import RoundReads, round_size, rounds

    size = round_size()
    attempted, partial = 0, False
    for batch in rounds(slice_paths, size):
        if stop_after is not None and time.monotonic() >= stop_after:
            partial = True
            break
        attempted += len(batch)
        found = dict(await asyncio.gather(*(source(p) for p in batch)))
        sources = {p: src for p, src in found.items() if src is not None}
        async with RoundReads(repo, org, ns) as reads:
            if size > 0:
                await reads.prefetch(sources, denylist)
            await asyncio.gather(*(one(reads, p, sources[p]) for p in batch if p in sources))
    return attempted, partial


def _note_embed_outcome(cfg: Any, embedder: Any | None, counters: dict[str, int]) -> None:
    """A slice that embedded with a provider and got nothing back opens the
    breaker: the next slices store stubs instead of waiting on it."""
    if embedder is None or counters["embed_stubs"] == 0 or counters["embedded"] > 0:
        return
    open_embed_breaker()
    logger.warning(
        "bootstrap: %s: %d chunks stubbed, none embedded — storing stubs for the next "
        "%d s (the recovery worker fills them)",
        cfg.label,
        counters["embed_stubs"],
        int(_EMBED_BREAKER_HOLD_S),
    )


async def record_backfill_stamps(repo: Any, org: str, ns: str) -> None:
    """A namespace indexed outside the worker (the first-build slices, the
    `loom code backfill` CLI) has rows produced by THIS extractor; record the
    stamps the way the worker's first observation would, so a read that asks
    "does this namespace's parser tier carry constructor calls" has an answer
    without a worker pass. A record already present — a worker mid-heal — is
    left alone."""
    from server.code.domain.extract.extractor_stamp import STAGES, extractor_stamp, stage_stamps

    state = await repo.all_state(org, ns)
    if str((state.get("extractor_stamp") or {}).get("value") or ""):
        return
    now = int(time.time())
    await repo.set_state(
        org,
        ns,
        "extractor_stamp",
        extractor_stamp(),
        detail=json.dumps({"at": now, "backfill": True}),
    )
    current = stage_stamps()
    for s in STAGES:
        await repo.set_state(
            org,
            ns,
            f"extractor_stamp_{s}",
            current[s],
            detail=json.dumps({"at": now, "from": "", "backfill": True}),
        )


async def _finish(repo: Any, org: str, ns: str, head: str, total: int) -> str:
    """The last slice: watermark, stamps, then `ready` when the vector leg
    already covers the chunks (a slice embedded inline), else `embedding`
    with the coverage; `maybe_finish_embedding` moves it on later."""
    await repo.set_state(org, ns, "git_head", head, detail="bootstrap")
    await repo.set_state(org, ns, CURSOR_KEY, "")
    await record_backfill_stamps(repo, org, ns)
    stage = await _write_embed_stage(repo, org, ns, total=total, head=head[:12])
    logger.info(
        "bootstrap: %s/%s initial index complete at %s (%d files) → %s",
        org,
        ns,
        head[:12],
        total,
        stage,
    )
    return stage


async def _write_embed_stage(repo: Any, org: str, ns: str, **detail: Any) -> str:
    embeddable, embedded = await repo.chunk_embed_coverage(org, ns)
    coverage = embedding_coverage(embeddable, embedded)
    reached = "ready" if is_searchable(coverage) else "embedding"
    await set_stage(
        repo,
        org,
        ns,
        reached,
        chunks=embeddable,
        embedded=embedded,
        embedding_coverage=round(coverage, 4),
        **detail,
    )
    return reached


async def maybe_finish_embedding(cfg: Any, repo: Any) -> str:
    """Called by the ingest pass for a self-service entry whose first index is
    built: while the stage is `embedding`, re-read the coverage and either
    move to `ready` or refresh the percentage the card shows. Returns the
    stage after the call ("" when the entry is not in that stage)."""
    stage = await repo.get_state(cfg.org, cfg.namespace, STAGE_KEY)
    if stage != "embedding":
        return ""
    new_stage = await _write_embed_stage(repo, cfg.org, cfg.namespace)
    if new_stage == "ready":
        logger.info("bootstrap: %s is ready — embeddings cover the index", cfg.label)
    return new_stage
