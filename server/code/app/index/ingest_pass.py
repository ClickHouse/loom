"""Per-merge fast-tier ingest for the code plane.

Poll shape (house convention — no inbound webhook surface): each pass
fetches the bare clone, walks first-parent commits since the `git_head`
watermark (oldest first, bounded), and per commit re-indexes exactly the
changed files: tree-sitter symbols/edges, AST chunks with contextual
embeddings (content-hash memoized — unchanged chunks cost nothing), a
context pack for the merged PR, then advances the watermark. A crash mid-
commit re-runs idempotently from the last complete commit.

LLM descriptions (contextual-retrieval one-liners) are best-effort and
budget-gated; embedding failures degrade to dim=0 stubs the
code_embed_recovery worker retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from server.auth.context import current_caller
from server.code.app.index.pass_budget import (
    CodeIngestResult,
    PassBudget,
    RepoPass,
    cut_points,
    git_text_source,
    pass_budget,
    rotate_to,
)
from server.code.app.index.pass_fetch import _publish_clone_head, fetch_entry, prefetch_mirrors
from server.code.app.index.pass_stream import ingest_oldest_first
from server.core.errors import caller_text
from server.llm import customer
from server.settings import settings
from server.workers import pass_context
from server.workers.status import log_pass_errors, record_pass, set_worker_identity

logger = logging.getLogger("loom.workers.code_ingest")


async def run_pass(org: str = "", namespace: str = "") -> CodeIngestResult:
    """One ingest pass. With org+namespace, only that configured repo runs
    (the code.refresh trigger addresses a single namespace); without, every
    configured repo runs (the poller cadence)."""
    result = CodeIngestResult()
    with record_pass("code_ingest") as rec:
        await _run(result, org, namespace)
        # Only when the pass ran to its end: a cancelled pass may have lost its lease.
        await _record_tier_health(result)
        log_pass_errors(logger, "code_ingest", result.errors)
        rec.outcome = pass_outcome(result)
    return result


def pass_outcome(result: CodeIngestResult) -> dict[str, Any]:
    """The scalars the pass records: the row's three counts
    under their own names, the partial flag and the resume
    point, then the legs' counters. The first sixteen ride the pass row as
    `outcome_*`; the detail carries them all."""
    return {
        "items_planned": result.files_planned,
        "items_done": result.files_done,
        "rows_written": result.rows_written(),
        "partial": result.partial,
        "resume": result.resume,
        "commits_planned": result.commits_planned,
        "commits_done": result.commits_done,
        "files_planned": result.files_planned,
        "files_done": result.files_done,
        "rechunk_done": result.rechunked,
        "heal_done": result.healed,
        "bootstrap_files": result.bootstrap_files,
        "elapsed_s": result.elapsed_s,
        "budget_s": result.budget_s,
        "errors": len(result.errors),
        "head": result.head[:12],
        "commits": result.commits,
        "files_indexed": result.files_indexed,
        "files_errored": result.files_errored,
        "files_excluded": result.files_excluded,
        "chunks_new": result.chunks_new,
        "embedded": result.embedded,
        "packs_built": result.packs_built,
        "briefs_built": result.briefs_built,
        "briefs_failed": result.briefs_failed,
        "dependents": result.dependents_reingested,
        "closure_resolved": result.closure_resolved,
        "unit_risk_rows": result.unit_risk_rows,
        "namespaces_walked": result.namespaces_walked,
        "read_hits": result.read_hits,
        "read_misses": result.read_misses,
        "note": result.note,
    }


def _ingest_served(cfg: Any, result: CodeIngestResult) -> None:
    """The parser tier is ok on an entry the pass served in full: no budget cut or held
    model call stopped it (its record otherwise stays as it was)."""
    if (cfg.org, cfg.namespace) not in result.unserved:
        result.tier_ok(cfg, "ingest")


async def _record_tier_health(result: CodeIngestResult) -> None:
    """Each entry's fetch and ingest outcome of this pass as its tier-health record."""
    from server.code.app.index import tier_health
    from server.code.store.index import CodeRepo

    repo = CodeRepo()
    for (org, ns, tier), error in result.tier_outcomes.items():
        await tier_health.record(repo, org, ns, tier, error or None)


def selected_repos(org: str = "", namespace: str = "") -> list[Any]:
    """The repos a pass covers: all configured repos, or just the addressed
    one. An addressed identity that is NOT configured selects nothing — a
    namespace only gets indexed when an operator put it in the config."""
    from server.code.domain.index.repos import configured_repos

    repos = configured_repos()
    if org and namespace:
        repos = [c for c in repos if c.org == org and c.namespace == namespace]
    return repos


async def _run(result: CodeIngestResult, org: str = "", namespace: str = "") -> None:
    """One pass over the selected entries, in this order: every entry's new commits (the
    watermark is the cursor, written per commit) → the bootstrap slices owed → the walk (the
    reconcile heal and the extractor re-chunk; their cursors are written per leg) → the
    cheap post-steps (the `embedding` → `ready` flip and the table counts). Each loop checks
    the pass budget before starting a unit; past it the pass records where it stopped and
    returns a success with `partial: true`."""
    # A repository a person added since the last pass is
    # picked up here — the registry snapshot is what configured_repos() reads.
    from server.code.app.index.repo_registry import refresh_db_repos

    budget = pass_budget()
    result.budget_s = round(budget.seconds, 1)
    await refresh_db_repos()
    inactive = await _inactive_namespaces()
    # A deleted directory row means retired — the worker must
    # not keep fetching and indexing it.
    repos = [c for c in selected_repos(org, namespace) if (c.org, c.namespace) not in inactive]
    # Before any entry can be skipped below (no sandbox, an org switched off, a clone).
    from server.code.app.index.maintainers import sync_restricted_page_branches

    await sync_restricted_page_branches(repos, exhausted=budget.exhausted)
    repos = _without_unsandboxed(repos, result)
    # Each phase starts at the entry the previous pass cut it
    # at (the cursor that pass left), so the entries after the cut are served
    # before the ones already served; the commit phase leaves the walk its
    # share of the budget instead of every release tree's commits taking all
    # of it, pass after pass.
    starts = cut_points(pass_context.resume_cursor())
    if not (org and namespace):
        repos = rotate_to(repos, starts.get("commits", ""))
    commit_budget = budget.phase(settings.code_ingest_walk_budget_share)
    await prefetch_mirrors(repos, result)  # One fetch per mirror
    plans: list[RepoPass] = []
    for i, cfg in enumerate(repos):
        if commit_budget.exhausted():
            result.cut(f"{cfg.namespace}:commits", cfg=cfg)
            logger.warning(
                "code_ingest: commit phase budget (%.0f s of %.0f) reached before %d entr%s "
                "fetched: %s — the next pass starts there",
                commit_budget.seconds,
                budget.seconds,
                len(repos) - i,
                "y" if len(repos) - i == 1 else "ies",
                ",".join(c.namespace for c in repos[i:]),
            )
            break
        pass_context.set_namespace(cfg.namespace)  # Where a cut pass stopped
        if await customer.switched_off(cfg.org):
            # Its chunks are embedded and described: the repository waits,
            # watermark unmoved, until the org's model calls are back on.
            continue
        try:
            # Its model calls are the namespace's, and when code.refresh runs the pass,
            # the caller's (its token's subject), as for any request.
            with customer.for_org(cfg.org, namespace=cfg.namespace, subject=current_caller()[0]):
                plan = await _run_repo(cfg, result, commit_budget)
        except customer.HOLDS:
            continue  # a held call is no failure: the repository waits, watermark unmoved
        except Exception as e:
            result.errors.append(f"{cfg.label}: {type(e).__name__}: {e}")
            logger.exception("code_ingest: repo %s failed", cfg.label)
            pass_context.note_failure(e, cfg.namespace)
            result.tier_failed(cfg, "ingest", caller_text(e))
            continue
        if plan is not None:
            plans.append(plan)
    await ingest_oldest_first(plans, result, commit_budget)
    await _run_leg(
        result,
        budget,
        rotate_to(
            [p for p in plans if p.bootstrap],
            starts.get("bootstrap", ""),
            key=lambda p: p.cfg.namespace,
        ),
        "bootstrap",
        _bootstrap_leg,
    )
    await _run_leg(
        result,
        budget,
        rotate_to(
            [p for p in plans if p.walk], starts.get("walk", ""), key=lambda p: p.cfg.namespace
        ),
        "walk",
        _walk_leg,
    )
    for plan in plans:
        pass_context.set_namespace(plan.cfg.namespace)
        await _post_steps(plan, result)
        _ingest_served(plan.cfg, result)
    result.elapsed_s = round(budget.elapsed(), 1)
    if result.partial:
        pass_context.set_cursor(result.resume)


async def _run_leg(
    result: CodeIngestResult,
    budget: PassBudget,
    plans: list[RepoPass],
    leg: str,
    run: Any,
) -> None:
    """One leg over the entries that owe it, the budget checked before each;
    an entry's failure is recorded, never fatal."""
    for i, plan in enumerate(plans):
        if budget.exhausted():
            for unserved in plans[i:]:
                result.cut(f"{unserved.cfg.namespace}:{leg}", cfg=unserved.cfg)
            logger.warning(
                "code_ingest: pass budget (%.0f s) reached before the %s of %d entr%s: %s — "
                "the next pass continues there",
                budget.seconds,
                leg,
                len(plans) - i,
                "y" if len(plans) - i == 1 else "ies",
                ",".join(p.cfg.namespace for p in plans[i:]),
            )
            return
        pass_context.set_namespace(plan.cfg.namespace)
        set_worker_identity("code_ingest", org=plan.cfg.org, namespace=plan.cfg.namespace)
        try:
            with customer.for_org(
                plan.cfg.org, namespace=plan.cfg.namespace, subject=current_caller()[0]
            ):
                await run(plan, result, budget)
        except customer.HOLDS:
            # a held call is no failure: the leg runs once the org is back
            result.held(plan.cfg)
        except Exception as e:
            result.errors.append(f"{plan.cfg.label}: {leg}: {type(e).__name__}: {e}")
            logger.exception("code_ingest: %s of %s failed", leg, plan.cfg.label)
            pass_context.note_failure(e, f"{plan.cfg.namespace}:{leg}")
            result.tier_failed(plan.cfg, "ingest", f"{leg}: {caller_text(e)}")


async def _bootstrap_leg(plan: RepoPass, result: CodeIngestResult, budget: PassBudget) -> None:
    """The first index is built a slice per pass with a cursor; the slice's
    rounds stop starting at the budget."""
    from server.code.app.index.bootstrap import CURSOR_KEY, index_slice

    cursor = await plan.repo.get_state(plan.cfg.org, plan.cfg.namespace, CURSOR_KEY)
    before = int(cursor) if str(cursor).isdigit() else 0
    with git_text_source(plan.cfg.git_dir, plan.head):
        progress = await index_slice(plan.cfg, plan.repo, plan.head, stop_after=budget.stop_after)
    done = int(progress.get("done") or 0)
    total = int(progress.get("total") or 0)
    planned = min(int(settings.code_bootstrap_files_per_pass), max(0, total - before))
    result.files_planned += planned
    result.files_done += max(0, done - before)
    result.bootstrap_files += max(0, done - before)
    if progress.get("partial"):
        result.cut(f"{plan.cfg.namespace}:bootstrap@{done}/{progress.get('total')}", cfg=plan.cfg)
    result.note = f"bootstrap {progress['stage']}: {progress['done']}/{progress['total']}"


async def _walk_leg(plan: RepoPass, result: CodeIngestResult, budget: PassBudget) -> None:
    result.namespaces_walked += 1
    with git_text_source(plan.cfg.git_dir, plan.head):
        await _steady_state(plan.repo, plan.cfg, plan.head, result, budget=budget)


async def _post_steps(plan: RepoPass, result: CodeIngestResult) -> None:
    """Seconds per entry, after every cursor of the pass is written so a cut
    pass never loses them: the change-driven table counts for an entry whose
    commits moved rows this pass, and the `embedding` → `ready` flip for a
    self-service entry whose first index exists."""
    if plan.commits:
        # The pass that moved the rows publishes their counts,
        # so the Repositories page reads a point instead of seven FINAL scans.
        try:
            await plan.repo.refresh_table_counts(plan.cfg.org, plan.cfg.namespace)
        except Exception as e:  # noqa: BLE001 — never fail a pass over a cache row
            result.errors.append(f"{plan.cfg.label}: table_counts: {type(e).__name__}: {e}")
        # The same pass publishes the coverage the
        # Repositories page shows, so a health open reads a point instead of
        # FINAL aggregates over the index while it builds.
        try:
            await plan.repo.refresh_coverage(plan.cfg.org, plan.cfg.namespace)
        except Exception as e:  # noqa: BLE001 — never fail a pass over a cache row
            result.errors.append(f"{plan.cfg.label}: coverage: {type(e).__name__}: {e}")
    if not (plan.self_service and plan.walk):
        return
    from server.code.app.index.bootstrap import maybe_finish_embedding

    try:
        stage = await maybe_finish_embedding(plan.cfg, plan.repo)
    except Exception as e:  # noqa: BLE001 — the card keeps its last stage
        result.errors.append(f"{plan.cfg.label}: ready flip: {type(e).__name__}: {e}")
        return
    if stage:
        result.note = f"bootstrap {stage}"


def _without_unsandboxed(repos: list[Any], result: CodeIngestResult) -> list[Any]:
    """`repos` less the self-registered entries a host without the sandbox must
    not clone or fetch (`compiler_tier.unsandboxed_skip`); each one skipped is
    named on the pass note."""
    from server.code.app.index.compiler_tier import unsandboxed_skip

    kept: list[Any] = []
    for cfg in repos:
        if reason := unsandboxed_skip(cfg, "code_ingest"):
            result.note = f"{cfg.namespace}: {reason}"
        else:
            kept.append(cfg)
    return kept


async def _inactive_namespaces() -> set[tuple[str, str]]:
    """(org, namespace) of configured code entries whose directory row says
    active = false. Fail-open (empty) when the directory cannot be read."""
    from server.code.domain.index.repos import configured_repos
    from server.core.ch import get_client

    keys = [(c.org, c.namespace) for c in configured_repos()]
    if not keys:
        return set()
    try:
        rows = await get_client().query_all(
            """
            SELECT org, namespace FROM loom.namespaces FINAL
            WHERE (org, namespace) IN {keys:Array(Tuple(String, String))} AND active = false
            """,
            params={"keys": keys},
        )
    except Exception:  # noqa: BLE001 — a directory outage must not stop indexing
        return set()
    return {(str(r["org"]), str(r["namespace"])) for r in rows}


async def _run_repo(
    cfg: Any,
    result: CodeIngestResult,
    budget: PassBudget | None = None,
) -> RepoPass | None:
    """Loop one for one entry: the mirror, the head, the watermark and the
    new commits (each written to the watermark as it lands). Returns what the
    later legs need, or None when nothing more is owed this pass."""
    import os

    from server.code.app.index import gitrepo
    from server.code.store.index import CodeRepo

    org, ns = cfg.org, cfg.namespace
    git_dir = cfg.git_dir
    # Provider spans (llm.code_describe, embed.*) created below read this to
    # carry loom.org/namespace/operation — without it they would be nameless
    # root traces in Langfuse and unattributable in ClickStack.
    set_worker_identity("code_ingest", org=org, namespace=ns)

    self_service = cfg.source == "registry"
    repo = CodeRepo()
    if not await asyncio.to_thread(os.path.isdir, git_dir):
        if not cfg.git_remote:
            result.note = f"git dir {git_dir} missing and the entry names no remote"
            return None
        # The mirror is a cache of the remote: a new host (or a moved deployment)
        # clones it again, in the background, so the pass goes on to the next entry.
        from server.code.app.index.bootstrap import clone_in_background

        present, failed = await clone_in_background(cfg, repo)
        if failed:
            result.errors.append(f"{cfg.label}: clone failed (retrying): {failed}")
            result.tier_failed(cfg, "fetch", f"clone {cfg.git_remote}: {failed}")
        if not present:
            result.note = f"{cfg.label}: cloning into {git_dir}"
            return None

    # Directory row so the namespace shows up (kind=code) in the dashboard. Once per
    # process per repo; failures are logged, never fatal.
    from server.code.app.index.bootstrap import restrict_if_restricted
    from server.code.app.index.registry import ensure_registered

    await ensure_registered(cfg)
    # Every pass, not once per process: a mirror an older build cloned, or one whose
    # mode was changed by hand, is tightened once the entry is restricted.
    await restrict_if_restricted(cfg)

    if not await fetch_entry(cfg, git_dir, repo, result):
        return None  # watermark unmoved; supervisor backoff handles cadence

    head = await gitrepo.head_sha(git_dir, cfg.git_branch)
    # The clone's tip is published beside the
    # watermark, so code.repos / code.index_status read a row instead of
    # running `git rev-parse` per repository on every cold open. Soft: a pass
    # never fails on this row.
    await _publish_clone_head(repo, org, ns, head, cfg.git_branch)
    watermark = await repo.get_state(org, ns, "git_head")
    result.head = head
    plan = RepoPass(cfg, repo, head, watermark, self_service, walk=bool(watermark))
    if not watermark:
        if self_service:
            # The first index is built a slice per pass, with a
            # cursor so a restart resumes where it stopped — in its own leg
            # after every entry's commits.
            plan.bootstrap = True
            return plan
        result.note = "no git_head watermark — run `loom code backfill` first"
        return None
    if watermark == head:
        return plan  # nothing new; the walk is still owed

    commits = await gitrepo.commits_between(
        git_dir, watermark, head, settings.code_ingest_max_commits_per_pass
    )
    if not commits:
        # Watermark not an ancestor (force-push / branch swap): reset to head
        # and let operators re-backfill if needed, loudly.
        logger.warning(
            "code_ingest: watermark %s not an ancestor of %s — advancing "
            "without indexing; re-run backfill to heal",
            watermark[:12],
            head[:12],
        )
        await repo.set_state(org, ns, "git_head", head, detail="watermark reset")
        result.note = "watermark reset (non-ancestor)"
        return plan
    plan.pending = commits  # Ingested oldest-first across entries
    return plan


async def _steady_state(
    repo: Any,
    cfg: Any,
    head: str,
    result: CodeIngestResult,
    *,
    budget: PassBudget | None = None,
) -> None:
    """The walk every pass owes an entry whose first index exists: the
    reconcile heal, then the extractor re-chunk, each in rounds that stop
    starting at the pass budget. The `embedding` → `ready` flip runs after the
    cursors, in `_post_steps`."""
    await _maybe_reconcile(repo, cfg, head, result, budget=budget)
    if budget is not None and budget.exhausted():
        result.cut(f"{cfg.namespace}:rechunk", cfg=cfg)
        return
    await _maybe_rechunk(repo, cfg, head, result, budget=budget)


# (path, blob) pairs a backfill found unindexable, kept in the reconcile state so the
# next measure skips them; the newest win when a namespace has more.
_UNINDEXABLE_KEPT = 2000


async def _scope_of(cfg: Any, head: str) -> Any:
    """The entry's scope as a path predicate: under a configured prefix and not
    refused by the path layers of its denylist (content rules need the bytes)."""
    from server.code.app.index import gitrepo
    from server.code.app.index.denylist import load_denylist

    prefixes = tuple(cfg.path_prefixes) or ("",)
    denylist = await load_denylist(cfg, head, gitrepo.show_file)
    return lambda path: path.startswith(prefixes) and not denylist.reason(path)


async def _tombstone_gone(repo: Any, cfg: Any, paths: list[str]) -> int:
    """Tombstone indexed paths the head tree no longer has; returns how many."""
    if not paths:
        return 0
    from server.code.app.index.ingest import tombstone_file
    from server.embedding.embedder import get_embedder

    done = 0
    for path in paths:
        try:
            await tombstone_file(
                repo,
                cfg.org,
                cfg.namespace,
                path=path,
                embedding_model=get_embedder().model_name,
            )
            done += 1
        except Exception as e:  # noqa: BLE001 — one bad path must not stop the pass
            logger.warning("code_ingest: reconcile tombstone %s failed: %s", path, e)
    return done


async def _backfill_missing(
    repo: Any,
    cfg: Any,
    head: str,
    found: Any,
    unindexable: dict[str, str],
    budget: PassBudget | None,
    *,
    left: int,
) -> tuple[Any, dict[str, str], dict[str, str]]:
    """A path in scope the index has no row for (the prefix list widened, or the walk
    never reached it) is ingested at head inside what is `left` of the heal budget.
    Returns the heal, the paths ingest refused, and the refusals to keep."""
    from server.code.app.index.reconcile import backfill_missing
    from server.embedding.embedder import get_embedder

    backfilled, refused = await backfill_missing(
        repo,
        cfg,
        head,
        found.missing,
        limit=max(0, left),
        embedder=get_embedder(),
        stop_after=budget.stop_after if budget is not None else None,
    )
    kept = dict(list({**unindexable, **refused}.items())[-_UNINDEXABLE_KEPT:])
    return backfilled, refused, kept


async def _maybe_reconcile(
    repo: Any,
    cfg: Any,
    head: str,
    result: CodeIngestResult,
    *,
    budget: PassBudget | None = None,
) -> None:
    """Once per `code_reconcile_interval_seconds` per namespace: measure the
    paths whose chunks predate their last change and heal a bounded batch.
    The count lands in code_index_state (key `reconcile`, value = stale
    count, detail = JSON) so the `loom.code.stale_files` gauge reads it
    without re-running git. Failures are logged, never fatal to the pass."""
    import json

    if not settings.code_reconcile_enabled:
        return
    try:
        prev = await repo.all_state(cfg.org, cfg.namespace)
        last = prev.get("reconcile") or {}
        detail = {}
        try:
            detail = json.loads(str(last.get("detail") or "{}"))
        except ValueError:
            detail = {}
        if time.time() - float(detail.get("at") or 0) < settings.code_reconcile_interval_seconds:
            return
        from server.code.app.index.reconcile import find_stale_paths, heal
        from server.embedding.embedder import get_embedder

        heal_budget = settings.code_reconcile_heal_per_pass
        drained = None
        if settings.code_ingest_dependents_enabled:
            from server.code.app.index.reconcile import drain_dependents_pending

            drained = await drain_dependents_pending(
                repo,
                cfg,
                head,
                budget=heal_budget,
                embedding_model=get_embedder().model_name,
            )
            heal_budget = max(0, heal_budget - drained.files)
            result.dependents_drained += drained.files
        unindexable = detail.get("unindexable") or {}
        found = await find_stale_paths(
            repo,
            cfg.git_dir,
            cfg.org,
            cfg.namespace,
            head,
            max_commits=settings.code_reconcile_max_commits,
            in_scope=await _scope_of(cfg, head),
            unindexable=unindexable,
        )
        healed = None
        if found.stale and heal_budget:
            result.files_planned += len(found.stale[:heal_budget])
            healed = await heal(
                repo,
                cfg,
                head,
                found.stale[:heal_budget],
                embedder=get_embedder(),
                stop_after=budget.stop_after if budget is not None else None,
            )
            result.files_done += int(getattr(healed, "attempted", healed.files) or 0)
            result.read_hits += int(getattr(healed, "read_hits", 0) or 0)
            result.read_misses += int(getattr(healed, "read_misses", 0) or 0)
            if getattr(healed, "partial", False):
                result.cut(
                    f"{cfg.namespace}:heal@{healed.attempted}/{len(found.stale[:heal_budget])}",
                    cfg=cfg,
                )
        backfilled, refused, unindexable = await _backfill_missing(
            repo,
            cfg,
            head,
            found,
            unindexable,
            budget,
            left=heal_budget - (healed.files if healed else 0),
        )
        # An indexed path the head tree no longer has can never
        # be healed (show_file returns None on every pass); tombstone it like the commit walk
        # would have, inside the same budget.
        gone_paths = list(getattr(found, "gone", ()) or ())
        gone_budget = max(
            0,
            heal_budget - (healed.files if healed else 0) - (backfilled.files if backfilled else 0),
        )
        gone_tombstoned = await _tombstone_gone(repo, cfg, gone_paths[:gone_budget])
        remaining = found.stale_count - (healed.files if healed else 0)
        await repo.set_state(
            cfg.org,
            cfg.namespace,
            "reconcile",
            str(max(0, remaining)),
            detail=json.dumps(
                {
                    "at": int(time.time()),
                    "head": head[:12],
                    "paths": found.paths_total,
                    "commits": found.commits_distinct,
                    "checked": found.commits_checked,
                    "failed": found.commits_failed,
                    "stale": found.stale_count,
                    "truncated": found.truncated,
                    "took_ms": found.took_ms,
                    "by_tree": getattr(found, "by_tree", 0),
                    "gone": len(gone_paths),
                    "gone_tombstoned": gone_tombstoned,
                    "tree_ms": getattr(found, "tree_ms", 0),
                    "healed": healed.files if healed else 0,
                    "chunks_moved": healed.chunks_moved if healed else 0,
                    "symbols_moved": healed.symbols_moved if healed else 0,
                    "heal_errors": healed.errors if healed else 0,
                    "missing": len(found.missing),
                    "backfilled": backfilled.files - len(refused) if backfilled else 0,
                    "unindexable": unindexable,
                    "dependents_drained": drained.files if drained else 0,
                    "dependents_pending": drained.remaining if drained else 0,
                }
            ),
        )
        result.reconciled = found.stale_count
        result.healed = healed.files if healed else 0
        if found.stale_count:
            logger.warning(
                "code_ingest: reconcile %s: %d of %d paths stale (%d distinct commits, "
                "truncated=%s), healed %d",
                cfg.label,
                found.stale_count,
                found.paths_total,
                found.commits_distinct,
                found.truncated,
                healed.files if healed else 0,
            )
    except customer.HOLDS:
        raise  # the reconcile re-ingests through the embedder: the repository waits
    except Exception as e:
        result.errors.append(f"reconcile: {type(e).__name__}: {e}")
        logger.exception("code_ingest: reconcile failed for %s", cfg.label)
        result.tier_failed(cfg, "ingest", f"reconcile: {caller_text(e)}")


def _stage_plan(
    value: str,
    detail: dict[str, Any],
    recorded: dict[str, str],
    current: str,
    current_stages: dict[str, str],
) -> tuple[str, frozenset[str], dict[str, Any]]:
    """Pure: what this pass does about the extractor stamps.

    Returns (action, stages, detail): action is `first` (no record yet:
    record, do not heal), `converged` (nothing), or `heal` with the stages
    to run and the progress detail to continue from — reset to cursor 0 when
    `target` is not the running stamp. A stage with no recorded key (one
    added after the keys were recorded, or the crash window between the two
    completion writes) counts as moved, so the heal is scoped to it plus
    whatever else moved; a combined move that no stage explains heals every
    stage."""
    from server.code.domain.extract.extractor_stamp import STAGES, moved_stages

    if not value:
        return "first", frozenset(), {}
    target = str(detail.get("target") or "")
    if value == current and not target:
        stale = moved_stages(recorded, current_stages)
        if not stale:
            return "converged", frozenset(), {}
        # the combined value agrees but a stage key does not (a stage added
        # after the keys were recorded, or the crash window between the two
        # completion writes): heal exactly those stages
        return "heal", stale, {"target": current, "cursor": 0, "stages": sorted(stale)}
    if target != current:
        # first divergence, or the extractor moved again mid-migration
        moved = moved_stages(recorded, current_stages)
        detail = {"target": current, "cursor": 0, "stages": sorted(moved or STAGES)}
    stages = frozenset(str(s) for s in (detail.get("stages") or STAGES) if s in STAGES)
    return "heal", (stages or frozenset(STAGES)), detail


async def _record_stage_stamps(
    repo: Any,
    cfg: Any,
    current_stages: dict[str, str],
    recorded: dict[str, str],
    reason: str,
) -> None:
    """Write every stage key at the running stamps, `detail[reason] = True`."""
    import json

    from server.code.domain.extract.extractor_stamp import STAGES

    now = int(time.time())
    for s in STAGES:
        await repo.set_state(
            cfg.org,
            cfg.namespace,
            f"extractor_stamp_{s}",
            current_stages[s],
            detail=json.dumps({"at": now, "from": recorded.get(s, ""), reason: True}),
        )


def _heal_languages(
    detail: dict[str, Any],
    recorded: Mapping[str, str],
    current: Mapping[str, str],
) -> frozenset[str]:
    """The language families this heal re-derives. A heal in
    progress keeps the families it started with (`detail["languages"]`); a
    fresh one takes the families whose stamp moved; a namespace with no
    family record heals every family, as does a move no family explains."""
    from server.code.domain.extract.extractor_stamp import LANGUAGE_FAMILIES, moved_languages

    kept = detail.get("languages")
    if isinstance(kept, list) and kept:
        return frozenset(str(x) for x in kept if x in LANGUAGE_FAMILIES) or frozenset(
            LANGUAGE_FAMILIES
        )
    if not any(recorded.get(lang) for lang in LANGUAGE_FAMILIES):
        return frozenset(LANGUAGE_FAMILIES)
    moved = moved_languages(recorded, current)
    return moved or frozenset(LANGUAGE_FAMILIES)


def _scope_paths(paths: list[str], langs: frozenset[str]) -> list[str]:
    """The live paths whose family is in `langs` (every path when all are)."""
    from server.code.domain.extract.extractor_stamp import (
        LANGUAGE_FAMILIES,
        language_family_of_path,
    )

    if langs >= frozenset(LANGUAGE_FAMILIES):
        return paths
    return [p for p in paths if language_family_of_path(p) in langs]


async def _record_language_stamps(
    repo: Any,
    cfg: Any,
    current: Mapping[str, str],
    recorded: Mapping[str, str],
    reason: str,
) -> None:
    """Write every family key at the running stamps."""
    import json

    from server.code.domain.extract.extractor_stamp import LANGUAGE_FAMILIES

    now = int(time.time())
    for lang in LANGUAGE_FAMILIES:
        await repo.set_state(
            cfg.org,
            cfg.namespace,
            f"extractor_stamp_lang_{lang}",
            current[lang],
            detail=json.dumps({"at": now, "from": recorded.get(lang, ""), reason: True}),
        )


async def _rechunk_batch(
    repo: Any,
    cfg: Any,
    head: str,
    batch: list[str],
    stages: frozenset[str],
    hashes: dict[str, str],
    result: CodeIngestResult,
    budget: PassBudget | None,
    *,
    cursor: int,
    total: int,
) -> tuple[int, int]:
    """Heal one re-chunk batch under the pass budget; returns (paths
    attempted, files identical). A heal the budget cut
    advances the cursor by the rounds it started, so the next pass continues
    at the next path."""
    if not batch:
        return 0, 0
    from server.code.app.index.reconcile import heal
    from server.embedding.embedder import get_embedder

    result.files_planned += len(batch)
    healed = await heal(
        repo,
        cfg,
        head,
        batch,
        embedder=get_embedder(),
        stages=stages,
        live_hashes=hashes,
        stop_after=budget.stop_after if budget is not None else None,
        concurrency=settings.code_extractor_rechunk_concurrency,
    )
    identical = int(getattr(healed, "identical", 0) or 0)
    result.rechunked += healed.files
    result.rechunk_identical += identical
    attempted = int(getattr(healed, "attempted", len(batch)) or 0)
    result.files_done += attempted
    result.read_hits += int(getattr(healed, "read_hits", 0) or 0)
    result.read_misses += int(getattr(healed, "read_misses", 0) or 0)
    if getattr(healed, "partial", False):
        result.cut(f"{cfg.namespace}:rechunk@{cursor + attempted}/{total}", cfg=cfg)
    return attempted, identical


async def _maybe_rechunk(
    repo: Any,
    cfg: Any,
    head: str,
    result: CodeIngestResult,
    *,
    budget: PassBudget | None = None,
) -> None:
    """Converge the namespace onto the running extractor stamps.

    code_index_state key `extractor_stamp`: value = the combined stamp the
    namespace is fully at; detail = JSON. Keys `extractor_stamp_chunk` /
    `_symbol` / `_edge`: the per-stage stamps it is fully
    at. While migrating, `extractor_stamp`'s detail carries {target, cursor,
    total, stages} and each pass re-ingests the next
    `code_extractor_rechunk_per_pass` live paths (sorted, so the cursor is
    stable across passes) through the reconcile heal path — same code as a
    content repair, and it never touches the git_head watermark — scoped to
    the stages whose stamp moved, so an unchanged file skips the blocks whose
    extractor did not. When the cursor reaches the end the value becomes the
    target and the stage keys are rewritten. A new stamp appearing
    mid-migration restarts the cursor at 0 against the new target with the
    stages re-derived from the recorded (pre-migration) stage stamps, so the
    scope is the union of everything that moved since the namespace last
    converged. An empty recorded value is a first observation: recorded, not
    acted on (`_stage_plan`). Failures are logged, never fatal to the pass."""
    import json

    if not settings.code_extractor_rechunk_enabled:
        return
    try:
        from server.code.domain.extract.extractor_stamp import (
            LANGUAGE_FAMILIES,
            STAGES,
            extractor_stamp,
            language_stamps,
            stage_stamps,
        )

        current, current_stages = extractor_stamp(), stage_stamps()
        current_langs = language_stamps()
        prev = await repo.all_state(cfg.org, cfg.namespace)
        row = prev.get("extractor_stamp") or {}
        value = str(row.get("value") or row.get("state_value") or "")
        try:
            detail = json.loads(str(row.get("detail") or "{}"))
        except ValueError:
            detail = {}
        recorded = {
            s: str((prev.get(f"extractor_stamp_{s}") or {}).get("value") or "") for s in STAGES
        }
        recorded_langs = {
            lang: str((prev.get(f"extractor_stamp_lang_{lang}") or {}).get("value") or "")
            for lang in LANGUAGE_FAMILIES
        }
        action, stages, detail = _stage_plan(value, detail, recorded, current, current_stages)
        if action == "first":
            await repo.set_state(
                cfg.org,
                cfg.namespace,
                "extractor_stamp",
                current,
                detail=json.dumps({"at": int(time.time()), "first_observation": True}),
            )
            await _record_stage_stamps(repo, cfg, current_stages, recorded, "first_observation")
            await _record_language_stamps(
                repo,
                cfg,
                current_langs,
                recorded_langs,
                "first_observation",
            )
            return
        if action != "heal":
            return  # converged
        cursor = int(detail.get("cursor") or 0)
        # Only the files whose language family's modules moved
        langs = _heal_languages(detail, recorded_langs, current_langs)
        detail["languages"] = sorted(langs)
        hashes = await repo.live_file_hashes(cfg.org, cfg.namespace)
        paths = _scope_paths(sorted(hashes), langs)
        batch = paths[cursor : cursor + settings.code_extractor_rechunk_per_pass]
        identical = int(detail.get("identical") or 0)
        attempted, identical_now = await _rechunk_batch(
            repo,
            cfg,
            head,
            batch,
            stages,
            hashes,
            result,
            budget,
            cursor=cursor,
            total=len(paths),
        )
        identical += identical_now
        cursor += attempted
        progress = {"at": int(time.time()), "stages": sorted(stages), "identical": identical}
        if cursor >= len(paths):
            await repo.set_state(
                cfg.org,
                cfg.namespace,
                "extractor_stamp",
                current,
                detail=json.dumps(
                    {**progress, "from": value, "files": len(paths), "completed": True}
                ),
            )
            await _record_stage_stamps(repo, cfg, current_stages, recorded, "completed")
            await _record_language_stamps(repo, cfg, current_langs, recorded_langs, "completed")
            logger.info(
                "code_ingest: %s re-chunked onto extractor stamp %s (%d files, "
                "stages %s, languages %s, %d identical)",
                cfg.label,
                current,
                len(paths),
                ",".join(sorted(stages)),
                ",".join(sorted(langs)),
                identical,
            )
        else:
            await repo.set_state(
                cfg.org,
                cfg.namespace,
                "extractor_stamp",
                value,
                detail=json.dumps(
                    {**progress, "target": current, "cursor": cursor, "total": len(paths)}
                ),
            )
            logger.warning(
                "code_ingest: %s extractor stamp %s -> %s (stages %s): "
                "re-chunked %d/%d, %d identical so far",
                cfg.label,
                value,
                current,
                ",".join(sorted(stages)),
                cursor,
                len(paths),
                identical,
            )
    except customer.HOLDS:
        raise  # the re-chunk embeds: the repository waits
    except Exception as e:
        result.errors.append(f"rechunk: {type(e).__name__}: {e}")
        logger.exception("code_ingest: extractor re-chunk failed for %s", cfg.label)
        result.tier_failed(cfg, "ingest", f"rechunk: {caller_text(e)}")
