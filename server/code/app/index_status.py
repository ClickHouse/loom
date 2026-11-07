"""code.index_status, code.index_errors, code.recent_commits and code.coverage: how
fresh and complete a namespace's index is, with the caches and the published status
snapshot that keep those reads cheap."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

from server.code.api.models_status import (
    CodeCommitCoverage,
    CodeCompilerTier,
    CodeCoverageGaps,
    CodeCoverageInput,
    CodeCoverageOutput,
    CodeIndexErrorRow,
    CodeIndexErrorsInput,
    CodeIndexErrorsOutput,
    CodeIndexStatusInput,
    CodeIndexStatusOutput,
    CodeLanguageCoverage,
    CodeRecentCommitRow,
    CodeRecentCommitsInput,
    CodeRecentCommitsOutput,
    CodeTierCoverage,
    CodeTierHealth,
    CodeWorkerStatus,
)
from server.code.app.common import require_code_intel, resolve_code_identity
from server.code.app.spans import set_code_span_attributes
from server.code.app.ttl_cache import TtlCache
from server.code.domain.index.compiler_tiers import state_value
from server.code.domain.index.state import IndexState
from server.code.store.index import CodeRepo
from server.core.background import create_deferred_task
from server.settings import settings

logger = logging.getLogger(__name__)


# The status is caller-independent and costly per repository (a state read plus four git
# subprocesses; a serial listing of ~20 repositories took over a second), so one answer per
# (org, namespace) serves every caller for `code_status_ttl_s`, and a cold listing computes
# the misses concurrently under a small semaphore instead of in series.
_status_cache: dict[tuple[str, str], tuple[float, CodeIndexStatusOutput]] = {}
_status_sem = asyncio.Semaphore(8)


async def _repo_status_cached(
    cfg: Any,
    *,
    max_age_s: float | None = None,
    state: IndexState | None = None,
    listing: bool = False,
) -> CodeIndexStatusOutput:
    """Fresh within `max_age_s`: the cached answer. Stale: the cached answer NOW, and one
    background refresh so the next caller gets a fresh one — a listing is never slower than a
    dict read once the cache is warm (a lapsed 30 s window would otherwise make a page load pay
    a cold `code.repos`). Missing: compute."""
    if max_age_s is None:
        max_age_s = settings.code_status_ttl_s
    key = (cfg.org, cfg.namespace)
    hit = _status_cache.get(key)
    now = time.monotonic()
    if hit is not None:
        if now - hit[0] < max_age_s:
            return hit[1]
        if key not in _status_refreshing:
            _status_refreshing.add(key)
            create_deferred_task(_refresh_repo_status(cfg, state=state))
        return hit[1]
    # Another process may have computed this status within the TTL and published it; with a
    # per-process memo alone, a multi-process web tier answers most dashboard calls cold. The
    # published row is listing-grade (no registry, files or workers) — only the listing reads
    # it, and it is never memoized, so code.index_status always gets a whole status.
    if listing:
        published = published_status_snapshot(state, max_age_s=max_age_s)
        if published is not None:
            return published
    return await _compute_repo_status(cfg, state=state)


STATUS_SNAPSHOT_KEY = "status_snapshot"


def published_status_snapshot(
    state: IndexState | None, *, max_age_s: float | None = None
) -> CodeIndexStatusOutput | None:
    """The `status_snapshot` state row another
    process published within `max_age_s`, as the status it holds; None for
    no row, a stale one, or one that does not parse (the caller computes)."""
    if max_age_s is None:
        max_age_s = settings.code_status_ttl_s
    if not state:
        return None
    row = state.get(STATUS_SNAPSHOT_KEY)
    if not isinstance(row, dict) or not row.get("value"):
        return None
    at = row.get("updated_at")
    if not isinstance(at, datetime):
        return None
    at_utc = at if at.tzinfo else at.replace(tzinfo=UTC)
    if (datetime.now(UTC) - at_utc).total_seconds() > max_age_s:
        return None

    try:
        return CodeIndexStatusOutput.model_validate_json(str(row["value"]))
    except Exception:  # noqa: BLE001 — an unparsable row is a missing row
        return None


# The status fields a listing row never reads — the operation registry, the file and
# language lists, the worker table — and the state map's blobs. A whole status is up to
# 235 KiB; a published row that carried it (and, through `state`, the previous snapshot)
# would grow on every publish and make every point read of `code_index_state` a 2 MB read
# (the table is one granule). The slim row is a few KB.
_SNAPSHOT_EXCLUDE = frozenset(
    {"ops", "request_fields", "op_maturity", "op_reach", "op_toolsets", "files", "lang", "workers"}
)
_SNAPSHOT_STATE_VALUE_MAX = 1024
# The state keys a listing row reads (`indexed_at` from git_head, the bootstrap stage); the
# rest of the map is code.index_status's, which never reads a published row. Slim rows that
# kept the whole map were still ~10 KiB each.
_SNAPSHOT_STATE_KEYS = frozenset({"git_head", "bootstrap_stage", "clone_head"})


def slim_status_snapshot_json(st: CodeIndexStatusOutput) -> str:
    """The listing-grade status as JSON."""
    data = st.model_dump(mode="json", exclude=set(_SNAPSHOT_EXCLUDE))
    data["state"] = {
        k: v
        for k, v in (data.get("state") or {}).items()
        if k in _SNAPSHOT_STATE_KEYS
        and len(str((v or {}).get("value", ""))) <= _SNAPSHOT_STATE_VALUE_MAX
    }
    return json.dumps(data, default=str)


async def _publish_status_snapshot(cfg: Any, st: CodeIndexStatusOutput) -> None:
    """The computed status, published for the other
    processes' listings; a failed publish keeps this process's memo and
    nothing else."""
    try:
        await CodeRepo().set_state(
            cfg.org,
            cfg.namespace,
            STATUS_SNAPSHOT_KEY,
            slim_status_snapshot_json(st),
            detail="status snapshot shared across processes",
        )
    except Exception as e:  # noqa: BLE001 — the snapshot is a cache
        logger.debug(
            "code.repos: status snapshot publish failed for %s/%s: %s", cfg.org, cfg.namespace, e
        )


_status_refreshing: set[tuple[str, str]] = set()


async def _compute_repo_status(
    cfg: Any, *, state: IndexState | None = None
) -> CodeIndexStatusOutput:
    key = (cfg.org, cfg.namespace)
    async with _status_sem:
        hit = _status_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < settings.code_status_ttl_s:
            return hit[1]
        # a caller without a prefetched state (code.index_status, tests' fakes)
        # keeps the one-argument call
        st = await (_repo_status(cfg, state=state) if state is not None else _repo_status(cfg))
        _status_cache[key] = (time.monotonic(), st)
        create_deferred_task(_publish_status_snapshot(cfg, st))
        return st


async def _refresh_repo_status(cfg: Any, *, state: IndexState | None = None) -> None:
    key = (cfg.org, cfg.namespace)
    try:
        await _compute_repo_status(cfg, state=state)
    except Exception as e:  # noqa: BLE001 — a failed refresh keeps the stale answer
        logger.debug("code.repos: status refresh failed for %s: %s", key, e)
    finally:
        _status_refreshing.discard(key)


def reset_repo_status_cache() -> None:
    """Tests, and a refresh that must be seen at once."""
    _status_cache.clear()
    _status_refreshing.clear()
    _lag_cache.clear()
    _ledger_passes_cache.clear()
    _coverage_cache.clear()
    _recent_commits_cache.clear()


# A cold `code.repos` refresh is mostly Python and git, not ClickHouse — per repository a
# state read, a counts read, four git subprocesses and the same 14-day worker-ledger query.
# The count of first-parent commits between two fixed shas never changes, so it is
# remembered per (clone, since, until); the ledger answer is one per process per
# `code_status_ledger_passes_ttl_s`.
_lag_cache: dict[tuple[str, str, str], int] = {}
_LAG_CACHE_MAX = 4096
_ledger_passes_cache: TtlCache[str, dict[str, dict[str, Any]]] = TtlCache(
    lambda: settings.code_status_ledger_passes_ttl_s, maxsize=16
)


async def _lag_between(gitrepo: Any, git_dir: str, since: str, until: str) -> int:
    """`commit_count_between`, remembered: the pair is immutable history."""
    key = (git_dir, since, until)
    hit = _lag_cache.get(key)
    if hit is not None:
        return hit
    try:
        n = int(await gitrepo.commit_count_between(git_dir, since, until))
    except Exception:  # noqa: BLE001 — non-ancestor / unknown sha
        return -1
    if len(_lag_cache) >= _LAG_CACHE_MAX:
        _lag_cache.clear()
    _lag_cache[key] = n
    return n


async def _clone_lags(
    cfg: Any, state: IndexState, lags: dict[str, int], val: Any
) -> tuple[str, dict[str, int]]:
    """(clone tip, lags): each watermark's distance to the bare clone's tip,
    counted with git; an unreadable clone leaves the lags at -1."""
    from server.code.app.index import gitrepo

    clone_head = ""
    lags = dict(lags)
    try:
        # The ingest pass publishes the clone's tip after every fetch (`clone_head`, detail =
        # branch); the row is the answer, and `git rev-parse` runs only when there is no row for
        # this branch (per repository, it dominates a cold listing).
        clone_head = published_clone_head(state, cfg.git_branch) or await gitrepo.head_sha(
            cfg.git_dir, cfg.git_branch
        )
        # The lag counts run together and are
        # remembered per sha pair — a warm status costs one git call.
        marks = [(k, val(k)) for k in lags if val(k)]
        lag_counts = await asyncio.gather(
            *(_lag_between(gitrepo, cfg.git_dir, wm, clone_head) for _, wm in marks)
        )
        for (name, _), n in zip(marks, lag_counts, strict=True):
            lags[name] = n
    except Exception as e:  # noqa: BLE001 — missing clone must not break status
        logger.debug("code status: clone tip unavailable for %s: %s", cfg.namespace, e)
    return clone_head, lags


def tier_health_rows(state: IndexState, lags: dict[str, int]) -> list[CodeTierHealth]:
    """Each recorded tier's health, with the lag of the watermark the tier advances
    (`lags` is keyed by state key: `git_head` for ingest, each compiler tier's own)."""
    from dataclasses import asdict

    from server.code.domain.index import tier_health

    return [
        CodeTierHealth(
            **asdict(h), lag_commits=lags.get(tier_health.WATERMARK_FOR_TIER.get(tier, ""), -1)
        )
        for tier, h in tier_health.from_state(state).items()
    ]


def published_lags(state: IndexState, marks: dict[str, str], clone_head: str) -> dict[str, int]:
    """The lags another process published in its status snapshot, for each watermark
    whose commit (and the clone tip) are still the ones it counted from: what a host
    without the mirror reports. -1 for the rest."""
    out = dict.fromkeys(marks, -1)
    row = state.get(STATUS_SNAPSHOT_KEY)
    if not isinstance(row, dict) or not row.get("value"):
        return out
    try:
        snap = json.loads(str(row["value"]))
    except ValueError:
        return out
    if not clone_head or str(snap.get("clone_head") or "") != clone_head:
        return out
    if marks.get("git_head") and snap.get("git_head") == marks["git_head"]:
        out["git_head"] = int(snap.get("fast_tier_lag", -1))
    for tier in snap.get("compiler_tiers") or []:
        key = str(tier.get("watermark_key") or "")
        if marks.get(key) and tier.get("commit") == marks[key]:
            out[key] = int(tier.get("lag", -1))
    return out


async def _repo_status(cfg: Any, *, state: IndexState | None = None) -> CodeIndexStatusOutput:
    """One repo entry's health: watermarks, lag vs the bare-clone tip, row counts, the
    CI-refresh breadcrumb, and this replica's code_* worker snapshot. Shared by
    code.index_status (one repo, full detail) and code.repos (every readable repo, summary).
    `state`: the namespace's state when the caller read every namespace's in one statement; the
    shared counts row rides in it."""
    from server.code.domain.index.compiler_tiers import WATERMARK_KEYS, compiler_tiers

    org, ns = cfg.org, cfg.namespace
    repo = CodeRepo()
    if state is None:
        state = await repo.all_state(org, ns)
    # counts are change-driven: cached while the watermarks in `state` match
    from server.code.store.index import counts_fingerprint

    counts = await repo.table_counts(
        org,
        ns,
        fingerprint=counts_fingerprint(state),
        max_age_s=_counts_max_age_s(state),
        shared_state=state.get("table_counts"),
    )

    def val(key: str) -> str:
        return _s((state.get(key) or {}).get("value"))

    def detail(key: str) -> str:
        return _s((state.get(key) or {}).get("detail"))

    git_head = val("git_head")
    # The mirror's last fetch rides the status, so a client
    # can tell "0 behind a mirror nobody has fetched" from fresh.
    from server.code.app.common import fetch_freshness

    clone_fetched_at, fetch_overdue = fetch_freshness(
        (state.get("clone_head") or {}).get("updated_at")
    )

    # keyed by state key; -1 = unset watermark or unreadable clone
    lags: dict[str, int] = {"git_head": -1, **dict.fromkeys(WATERMARK_KEYS, -1)}
    from server.code.app.index.mirrors import present_dir

    if not await present_dir(cfg):
        # No mirror here (yet): the counts another process published, for the same
        # commits, so every replica answers alike.
        clone_head = published_clone_head(state, cfg.git_branch)
        lags.update(published_lags(state, {k: val(k) for k in lags}, clone_head))
    else:
        clone_head, lags = await _clone_lags(cfg, state, lags, val)
    # The entry's own tier is the headline; every tier with a watermark is
    # listed. An entry naming no producer defers to whichever watermark is set.
    tiers = compiler_tiers(cfg.scip_producer, state, lags)
    primary = next((t for t in tiers if t["primary"]), None)
    scip_commit = str(primary["commit"]) if primary else ""
    scip_lag = int(primary["lag"]) if primary else -1

    workers: dict[str, CodeWorkerStatus] = {}
    try:
        from server.workers.registry import worker_meta
        from server.workers.status import get_registry

        meta = worker_meta()
        snap = get_registry().snapshot()
        # The registry is process-local: with the web and workers containers split (blue/green),
        # the web process never runs a worker and its snapshot says "never" for every one. The
        # ledger (`loom.worker_status`) is what every process writes — it wins for the pass facts;
        # the registry keeps enabled / interval and fills in when the ledger has no row.
        ledger = await _worker_passes_from_ledger()
        for name, m in meta.items():
            if not name.startswith("code_"):
                continue
            w = ledger.get(name) or snap.get(name) or {}
            err = w.get("last_error") or ""
            workers[name] = CodeWorkerStatus(
                enabled=bool(m.get("enabled", True)),
                interval_seconds=int(m.get("interval_seconds") or 0),
                last_run_at=str(w.get("last_run_at") or ""),
                last_duration_ms=int(w.get("last_duration_ms") or 0),
                consecutive_failures=int(w.get("consecutive_failures") or 0),
                total_runs=int(w.get("total_runs") or 0),
                last_error=str(err)[:200],
            )
    except Exception as e:  # noqa: BLE001 — worker registry is best-effort
        logger.debug("code status: worker snapshot unavailable: %s", e)

    coverage = coverage_fields(counts)
    candidates, warnings = await _index_quality(
        repo, cfg, state, workers, git_head=git_head, fetch_overdue=fetch_overdue
    )

    return CodeIndexStatusOutput(
        repo=cfg.repo,
        branch=cfg.git_branch,
        org=org,
        namespace=ns,
        provider=cfg.provider,
        profile=cfg.profile,
        compiler_candidates=candidates,
        quality_warnings=warnings,
        git_head=git_head,
        scip_commit=scip_commit,
        scip_commit_py=val("scip_commit_py"),
        scip_producer=primary["producer"] if primary else "",
        compiler_tiers=[CodeCompilerTier(**t) for t in tiers],
        tier_health=tier_health_rows(state, lags),
        pr_cursor=val("pr_cursor"),
        clone_head=clone_head,
        clone_fetched_at=clone_fetched_at,
        fetch_overdue=fetch_overdue,
        fast_tier_lag=lags["git_head"],
        scip_lag=scip_lag,
        scip_py_lag=lags["scip_commit_py"],
        last_refresh_at=val("last_refresh_at"),
        last_refresh_detail=detail("last_refresh_at"),
        org_readable=cfg.org_readable,
        path_prefixes=list(cfg.path_prefixes),
        counts=counts,
        state={
            k: {
                "value": _s(v.get("value")),
                "detail": _s(v.get("detail")),
                "updated_at": str(v.get("updated_at") or ""),
            }
            for k, v in state.items()
            # The published snapshot is not part of
            # the status it snapshots.
            if k != STATUS_SNAPSHOT_KEY
        },
        workers=workers,
        **coverage,
    )


async def _index_quality(
    repo: CodeRepo,
    cfg: Any,
    state: IndexState,
    workers: dict[str, CodeWorkerStatus],
    *,
    git_head: str,
    fetch_overdue: bool,
) -> tuple[list[str], list[str]]:
    from server.code.app.index.producer_inference import tree_producers
    from server.code.domain.index.compiler_tiers import TIERS

    try:
        candidates = await tree_producers(repo, cfg.org, cfg.namespace)
    except Exception as e:  # noqa: BLE001 — diagnostics must not break status
        logger.debug("code status: compiler inference unavailable for %s: %s", cfg.namespace, e)
        candidates = []

    def value(key: str) -> str:
        return _s((state.get(key) or {}).get("value"))

    warnings: list[str] = []
    if not git_head:
        warnings.append("Fast index has not completed its first pass.")
    if fetch_overdue:
        warnings.append("The repository mirror is overdue for a fetch.")
    for producer in candidates:
        spec = next((tier for tier in TIERS if tier.producer == producer), None)
        if spec and not value(spec.watermark_key):
            names = ", ".join(spec.workers)
            warnings.append(f"{spec.tool} is available for this tree but has not run ({names}).")
    failed = sorted(name for name, worker in workers.items() if worker.last_error)
    if failed:
        warnings.append("Index workers reporting errors: " + ", ".join(failed) + ".")
    return candidates, warnings


def published_clone_head(state: IndexState, branch: str) -> str:
    """The `clone_head` row's sha when the pass published one for `branch`; '' when there is
    no row or it names another branch, and the caller asks git."""
    row = state.get("clone_head") or {}
    sha = _s(row.get("value"))
    if len(sha) != 40 or _s(row.get("detail")) != branch:
        return ""
    return sha


def _counts_max_age_s(state: IndexState) -> float:
    from server.code.app.index.bootstrap import BUILDING_STAGES, STAGE_KEY

    stage = _s((state.get(STAGE_KEY) or {}).get("value"))
    return (
        settings.code_status_building_counts_ttl_s
        if stage in BUILDING_STAGES
        else settings.code_status_steady_counts_ttl_s
    )


def coverage_fields(counts: dict[str, int]) -> dict[str, Any]:
    """`chunks_total` / `chunks_embedded` / `embedding_coverage`
    from the counts `table_counts` already reads (no second scan)."""
    from server.code.app.index.bootstrap import embedding_coverage

    total = int(counts.get("code_chunks") or 0)
    if "code_chunks_embedded" not in counts:
        # a counts read without the coverage columns: unknown is not "lexical only"
        return {"chunks_total": total, "chunks_embedded": 0, "embedding_coverage": 1.0}
    embedded = int(counts.get("code_chunks_embedded") or 0)
    embeddable = int(counts.get("code_chunks_embeddable", total) or 0)
    return {
        "chunks_total": total,
        "chunks_embedded": embedded,
        "embedding_coverage": round(embedding_coverage(embeddable, embedded), 4),
    }


def _adhoc_repo(org: str, ns: str) -> Any:
    """A RepoConfig-shaped stand-in for a namespace that has index rows but
    no repo entry (e.g. after LOOM_CODE_REPOS_JSON dropped it). Status still
    renders; lag reads as unknown because there is no clone to compare."""
    from server.code.domain.index.repos import RepoConfig

    return RepoConfig(
        org=org,
        namespace=ns,
        repo=settings.code_repo,
        git_dir="",
        git_remote="",
        git_branch=settings.code_git_branch,
        path_prefixes=(),
        github_token="",
    )


async def _worker_passes_from_ledger(days: int = 14) -> dict[str, dict[str, Any]]:
    """The newest recorded pass per `code_*` worker from `loom.worker_status`, in the registry
    snapshot's shape, so `code.index_status` reads the same facts whichever container answers.
    Empty on any failure (the caller falls back to the process-local registry). One answer per
    process per `code_status_ledger_passes_ttl_s`: every repository's status asks this identical
    question."""
    from server.core.ch import get_client

    cache_key = str(int(days))
    hit = _ledger_passes_cache.get(cache_key)
    if hit is not None:
        return hit
    # Single-flight. A cold listing starts every repository's status together, and each would
    # ask this question before the first answer had landed (several identical 1.6 MB reads).
    inflight = _ledger_passes_inflight.get(cache_key)
    if inflight is not None:
        return await asyncio.shield(inflight)
    fut: asyncio.Future[dict[str, dict[str, Any]]] = asyncio.get_running_loop().create_future()
    _ledger_passes_inflight[cache_key] = fut
    try:
        out = await _read_worker_passes(get_client(), days)
        fut.set_result(out)
        return out
    except BaseException as e:
        fut.set_exception(e)
        raise
    finally:
        _ledger_passes_inflight.pop(cache_key, None)


_ledger_passes_inflight: dict[str, asyncio.Future[dict[str, dict[str, Any]]]] = {}


async def _read_worker_passes(client: Any, days: int) -> dict[str, dict[str, Any]]:
    """The ledger read itself; `_worker_passes_from_ledger` remembers and
    single-flights it."""
    from server.workers import status_store

    cache_key = str(int(days))
    try:
        rows = await status_store.pass_summaries("code_", days, ch=client)
    except Exception as e:  # noqa: BLE001 — the ledger is best-effort here
        logger.debug("code status: worker ledger unavailable: %s", e)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        started, finished = r.get("last_started"), r.get("last_finished")
        duration_ms = 0
        if isinstance(started, datetime) and isinstance(finished, datetime) and finished >= started:
            duration_ms = int((finished - started).total_seconds() * 1000)
        err = str(r.get("last_error") or "")
        if not err and str(r.get("last_status") or "") not in ("", "ok", "idle"):
            err = str(r.get("last_status"))
        last_run_at = ""
        if isinstance(started, datetime):
            last_run_at = started.replace(tzinfo=UTC).isoformat()
        out[str(r["worker"])] = {
            "last_run_at": last_run_at,
            "last_duration_ms": duration_ms,
            "consecutive_failures": int(r.get("consecutive_failures") or 0),
            "total_runs": int(r.get("total_runs") or 0),
            "last_error": err,
        }
    _ledger_passes_cache.put(cache_key, out)
    return out


async def handle_code_index_status(input: CodeIndexStatusInput) -> CodeIndexStatusOutput:
    from server.code.domain.index.repos import repo_for_namespace

    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    cfg = repo_for_namespace(org, ns) or _adhoc_repo(org, ns)

    # The same 30 s status `code.repos` serves, on
    # a copy — the fields below are this call's, not the cached row's.
    st = (await _repo_status_cached(cfg)).model_copy()
    # The two stats fields come from the rows the
    # graph-stats pass published (a few kB of state), not from two FINAL
    # counts over the large tables; a namespace without a row counts once
    # and publishes for everyone.
    state = await _state_for_status(org, ns)
    st.symbol_stats_rows, st.symbol_stats_at = await symbol_stats_summary_cached(
        org, ns, published=state.get("symbol_stats_summary")
    )
    st.unit_risk_rows, st.unit_risk_at = await unit_risk_summary_cached(
        org, ns, published=state.get("unit_risk_summary")
    )
    st.build_commit = settings.build_commit
    cat = code_op_catalogue()
    st.ops, st.request_fields = cat.ops, cat.request_fields
    st.op_maturity, st.op_reach = cat.maturity, cat.reach
    st.op_toolsets = cat.toolsets
    if input.commit:
        # Per-commit coverage, after the cached status (it is
        # this call's question, not the repository's).
        _fresh_heads(st, state)  # Never a cached head
        from server.code.app.index.commit_coverage import commit_coverage
        from server.code.app.index.mirrors import history_dir

        git_dir, gap = await history_dir(cfg)
        cov = await commit_coverage(
            git_dir,
            input.commit,
            st.git_head,
            [t.model_dump() for t in st.compiler_tiers],
            paths=list(input.paths or []),
            gap=gap,
        )
        st.commit_coverage = CodeCommitCoverage(**cov)
    brief = input.brief if input.brief is not None else bool(input.commit)
    if brief:
        # What a poller needs; the `state` map is most of a whole status.
        st.state, st.workers, st.counts = {}, {}, {}
        st.ops, st.request_fields = [], {}
        st.op_maturity, st.op_reach, st.op_toolsets = {}, {}, {}
    set_code_span_attributes(
        lag_commits=st.fast_tier_lag,
        stale=st.fast_tier_lag > settings.code_stale_after_commits,
        tier="scip" if st.scip_commit else "treesitter",
        consumer=input.consumer,
    )
    return st


_SYMBOL_STATS_CACHE: dict[tuple[str, str], tuple[float, int, str]] = {}
_UNIT_RISK_CACHE: dict[tuple[str, str], tuple[float, int, str]] = {}
# "the caller never looked for a published row" — distinct from None, "looked, none
# there": only the latter computes AND publishes.
_NOT_LOOKED_UP: Any = object()


def _fresh_heads(st: CodeIndexStatusOutput, state: IndexState) -> None:
    """The heads a coverage answer is judged against, from this call's state read. The cached
    status is stale-while-revalidate: a web process not asked for an hour would answer its
    hour-old `git_head`, so one call could say a commit was covered and the next that it was
    not."""
    for key in ("git_head", "clone_head"):
        if value := state_value(state, key):
            setattr(st, key, value)
    st.compiler_tiers = [t.model_copy() for t in st.compiler_tiers]  # not the cached row's
    for tier in st.compiler_tiers:
        if (value := state_value(state, tier.watermark_key)) and value != tier.commit:
            tier.commit = value
            tier.updated_at = str((state.get(tier.watermark_key) or {}).get("updated_at") or "")


async def _state_for_status(org: str, ns: str) -> dict[str, Any]:
    """The namespace's `code_index_state` rows for `code.index_status`; {}
    when the read fails (the status stays whole, the summaries count)."""
    try:
        return await CodeRepo().all_state(org, ns)
    except Exception as e:  # noqa: BLE001 — status never fails on its state read
        logger.debug("code.index_status: state read failed for %s: %s", ns, e)
        return {}


async def symbol_stats_summary_cached(
    org: str,
    ns: str,
    *,
    max_age_s: float | None = None,
    published: Any = _NOT_LOOKED_UP,
) -> tuple[int, str]:
    """`symbol_stats_rows` / `symbol_stats_at` for
    code.index_status. The table moves once a week, so the answer is held
    per namespace for ten minutes in the process (the read scans the
    namespace's stats rows); the read failing open to (0, "") keeps status
    whole."""
    if max_age_s is None:
        max_age_s = settings.code_status_symbol_stats_ttl_s
    return await _table_summary_cached(
        _SYMBOL_STATS_CACHE,
        org,
        ns,
        lambda r: r.symbol_stats_summary(org, ns),
        what="symbol stats",
        max_age_s=max_age_s,
        key="symbol_stats_summary",
        published=published,
    )


async def unit_risk_summary_cached(
    org: str,
    ns: str,
    *,
    max_age_s: float | None = None,
    published: Any = _NOT_LOOKED_UP,
) -> tuple[int, str]:
    """`unit_risk_rows` / `unit_risk_at` for code.index_status
    — the weekly unit-risk leg's progress, held ten minutes per namespace
    like the stats fields; (0, "") when the read fails."""
    if max_age_s is None:
        max_age_s = settings.code_status_symbol_stats_ttl_s
    return await _table_summary_cached(
        _UNIT_RISK_CACHE,
        org,
        ns,
        lambda r: r.unit_risk_summary(org, ns),
        what="unit risk",
        max_age_s=max_age_s,
        key="unit_risk_summary",
        published=published,
    )


async def _table_summary_cached(
    cache: dict[tuple[str, str], tuple[float, int, str]],
    org: str,
    ns: str,
    read: Callable[[Any], Awaitable[tuple[int, datetime | None]]],
    *,
    what: str,
    max_age_s: float,
    key: str = "",
    published: Any = _NOT_LOOKED_UP,
) -> tuple[int, str]:
    """The process memo, else the published state row, else one count — published for everyone
    when the caller looked for a row and found none, so the next open anywhere reads the row."""
    from server.code.store.index import parse_published_summary, summary_stamp

    cache_key = (org, ns)
    now = time.monotonic()
    hit = cache.get(cache_key)
    if hit is not None and now - hit[0] < max_age_s:
        return hit[1], hit[2]
    if len(cache) > 256:
        cache.clear()
    row = parse_published_summary(published) if published is not _NOT_LOOKED_UP else None
    if row is not None:
        cache[cache_key] = (now, row[0], row[1])
        return row
    rows, at = 0, ""
    try:
        n, stamp = await read(CodeRepo())
        rows, at = int(n), summary_stamp(stamp)
    except Exception as e:  # noqa: BLE001 — status stays whole when the read fails
        logger.debug("code status: %s summary unavailable for %s: %s", what, ns, e)
        cache[cache_key] = (now, rows, at)
        return rows, at
    if key and published is None:
        try:
            await CodeRepo().publish_summary(org, ns, key, rows, at)
        except Exception as e:  # noqa: BLE001 — a failed publish keeps the answer
            logger.debug("code status: %s summary not published for %s: %s", what, ns, e)
    cache[cache_key] = (now, rows, at)
    return rows, at


class CodeOpCatalogue(NamedTuple):
    ops: list[str]
    request_fields: dict[str, list[str]]
    maturity: dict[str, str]
    reach: dict[str, list[str]]
    toolsets: dict[str, list[str]]


def code_op_catalogue() -> CodeOpCatalogue:
    """Served code.* operations, the request fields each accepts, and each op's maturity and
    reach set, read from the registry so the catalogue cannot drift from the transport.
    Imported lazily: the registry imports this module's handlers."""
    from server.operations.reach import catalogue_facets
    from server.operations.registry import ALL_OPERATIONS

    code_ops = sorted(
        (op for op in ALL_OPERATIONS if op.name.startswith("code.")), key=lambda o: o.name
    )
    facets = {op.name: catalogue_facets(op) for op in code_ops}
    return CodeOpCatalogue(
        ops=[op.name for op in code_ops],
        request_fields={op.name: sorted(op.input_model.model_fields) for op in code_ops},
        maturity={n: f["maturity"] for n, f in facets.items()},
        reach={n: f["reach"] for n, f in facets.items()},
        toolsets={n: f["toolsets"] for n, f in facets.items()},
    )


async def handle_code_index_errors(input: CodeIndexErrorsInput) -> CodeIndexErrorsOutput:
    """List the files the code index failed to parse or ingest, with the error and the commit."""
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    rows, total = await CodeRepo().index_errors(org, ns, limit=input.limit)
    set_code_span_attributes(results=total, consumer=input.consumer)
    return CodeIndexErrorsOutput(
        rows=[
            CodeIndexErrorRow(
                path=str(r.get("path") or ""),
                lang=str(r.get("lang") or ""),
                error=str(r.get("index_error") or "")[:500],
                indexed_commit=_s(r.get("indexed_commit")),
                at=_ts_or_empty(r.get("created_at")),
            )
            for r in rows
        ],
        total=total,
    )


_recent_commits_cache: TtlCache[tuple[str, str, int], list[dict[str, Any]]] = TtlCache(
    lambda: settings.code_status_recent_commits_ttl_s, maxsize=256
)


async def _recent_commits_cached(org: str, ns: str, limit: int) -> list[dict[str, Any]]:
    """The recent-commits scan (8 MB of code_files
    under FINAL per open) is remembered for a minute per namespace and limit
    — a panel of the last commits does not change by the second."""
    key = (org, ns, limit)
    hit = _recent_commits_cache.get(key)
    if hit is not None:
        return [dict(r) for r in hit]
    rows = await CodeRepo().recent_indexed_commits(org, ns, limit=limit)
    _recent_commits_cache.put(key, [dict(r) for r in rows])
    return rows


async def handle_code_recent_commits(input: CodeRecentCommitsInput) -> CodeRecentCommitsOutput:
    """List the most recently indexed commits of a repository, decorated with subject and time from
    the clone."""
    from server.code.app.index import gitrepo
    from server.code.domain.index.repos import repo_for_namespace

    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    rows = await _recent_commits_cached(org, ns, int(input.limit))
    cfg = repo_for_namespace(org, ns)
    # One `git log --no-walk` decorates every row
    # instead of two subprocesses per row (870 ms p50 for a page of rows).
    meta: dict[str, tuple[str, int]] = {}
    shas = [_s(r.get("commit")) for r in rows]
    if cfg and cfg.git_dir and any(shas):
        try:  # best-effort decoration from the bare clone
            meta = await gitrepo.commit_meta_many(cfg.git_dir, [s for s in shas if s])
        except Exception as e:  # noqa: BLE001 — a missing clone leaves rows undecorated
            logger.debug("code.recent_commits: clone metadata unavailable for %s: %s", ns, e)
    out: list[CodeRecentCommitRow] = []
    for r, sha in zip(rows, shas, strict=True):
        subject, committed_at = "", ""
        found = meta.get(sha)
        if found is not None:
            subject = found[0]
            committed_at = datetime.fromtimestamp(found[1], UTC).isoformat(timespec="seconds")
        out.append(
            CodeRecentCommitRow(
                commit=sha,
                subject=subject[:200],
                committed_at=committed_at,
                ingested_at=_ts_or_empty(r.get("ingested_at")),
                files=int(r.get("files") or 0),
                pr_number=int(r.get("pr_number") or 0),
            )
        )
    set_code_span_attributes(results=len(out), consumer=input.consumer)
    return CodeRecentCommitsOutput(rows=out)


def gaps_from_counts(model: Any, c: dict[str, int]) -> Any:
    """Pure: the six counts → the gaps row with its shares (0.0 when the
    denominator is 0, so an empty namespace reads as no gap, not as NaN)."""

    def share(num: int, den: int) -> float:
        return round(num / den, 4) if den else 0.0

    return model(
        functions_scanned=c.get("functions_scanned", 0),
        functions_uncalled=c.get("functions_uncalled", 0),
        uncalled_share=share(c.get("functions_uncalled", 0), c.get("functions_scanned", 0)),
        source_files=c.get("source_files", 0),
        source_files_untested=c.get("source_files_untested", 0),
        untested_share=share(c.get("source_files_untested", 0), c.get("source_files", 0)),
        code_files=c.get("code_files", 0),
        code_files_no_symbols=c.get("code_files_no_symbols", 0),
        no_symbols_share=share(c.get("code_files_no_symbols", 0), c.get("code_files", 0)),
    )


_CoverageEntry = tuple[str, float, dict[str, Any], dict[str, int] | None]
_coverage_cache: dict[tuple[str, str], _CoverageEntry] = {}


async def _coverage_cached(
    repo: Any, org: str, ns: str, state: IndexState
) -> tuple[dict[str, Any], dict[str, int] | None]:
    """`repo.coverage` + `repo.coverage_gaps`, remembered per (org, ns) while the counts
    fingerprint of `state` matches and the entry is younger than the counts max age (60 s while
    an index builds, 10 min steady). The gap counts are None when their queries failed — the
    page renders without the gaps row."""
    from server.code.store.index import counts_fingerprint

    key = (org, ns)
    fp = counts_fingerprint(state)
    hit = _coverage_cache.get(key)
    now = time.monotonic()
    if hit is not None and hit[0] == fp and now - hit[1] < _counts_max_age_s(state):
        return hit[2], hit[3]
    # The pass that moved the rows published their
    # coverage beside its counts; a row whose fingerprint is ours is the
    # answer — a point read instead of 1.2 GB of FINAL aggregates. No row (a
    # namespace nobody computed yet): compute once and publish it ourselves.
    from server.code.store.index import parse_shared_coverage

    published = parse_shared_coverage(state.get("coverage"), fp)
    refresh = getattr(repo, "refresh_coverage", None)
    if published is not None:
        cov, gap_counts = published
    elif refresh is not None:
        cov, gap_counts = await refresh(org, ns, fingerprint=fp)
    else:  # a repository without the publisher (test fakes): compute, keep
        cov = await repo.coverage(org, ns)
        try:
            gap_counts = await repo.coverage_gaps(org, ns)
        except Exception as e:  # noqa: BLE001 — coverage must not fail on the gap queries
            logger.warning("code.coverage: gap queries failed for %s/%s: %s", org, ns, e)
            gap_counts = None
    if len(_coverage_cache) > 256:
        _coverage_cache.clear()
    _coverage_cache[key] = (fp, now, cov, gap_counts)
    return cov, gap_counts


async def handle_code_coverage(input: CodeCoverageInput) -> CodeCoverageOutput:
    """Report how much of a repository the index covers at its head: per language, per tier, and the
    gaps."""

    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    # The coverage scans (every chunk, symbol and
    # edge of the namespace under FINAL — 175 MB per call) are change-driven:
    # remembered while the writers' watermarks match, like `table_counts`.
    state = await repo.all_state(org, ns)
    head = state_value(state, "git_head")
    cov, gap_counts = await _coverage_cached(repo, org, ns, state)
    gaps: CodeCoverageGaps | None = None
    if gap_counts is not None:
        gaps = gaps_from_counts(CodeCoverageGaps, gap_counts)

    langs: dict[str, CodeLanguageCoverage] = {}

    def lang_row(name: str) -> CodeLanguageCoverage:
        return langs.setdefault(name, CodeLanguageCoverage(lang=name))

    for r in cov["files"]:
        row = lang_row(str(r.get("lang") or ""))
        row.files = int(r.get("files") or 0)
        row.files_errored = int(r.get("errored") or 0)
        row.files_first_at = _ts_or_empty(r.get("first_at"))
        row.files_last_at = _ts_or_empty(r.get("last_at"))
    for r in cov["chunks"]:
        row = lang_row(str(r.get("lang") or ""))
        row.chunks = int(r.get("chunks") or 0)
        row.chunks_embedded = int(r.get("embedded") or 0)
        row.chunks_described = int(r.get("described") or 0)
        row.chunks_last_at = _ts_or_empty(r.get("last_at"))
    for r in cov["symbols"]:
        row = lang_row(str(r.get("lang") or ""))
        n, at = int(r.get("symbols") or 0), _ts_or_empty(r.get("last_at"))
        if str(r.get("tier")) == "scip":
            row.symbols_scip, row.symbols_scip_last_at = n, at
        else:
            row.symbols_treesitter, row.symbols_treesitter_last_at = n, at

    misc = cov["misc"]
    set_code_span_attributes(
        results=len(langs),
        tier="scip" if any(r.symbols_scip for r in langs.values()) else "treesitter",
        consumer=input.consumer,
    )
    return CodeCoverageOutput(
        org=org,
        namespace=ns,
        as_of_commit=head,
        languages=sorted(langs.values(), key=lambda x: (-x.files, x.lang)),
        gaps=gaps,
        tiers=[
            CodeTierCoverage(
                tier=str(r.get("tier")),
                edges=int(r.get("edges") or 0),
                edges_last_at=_ts_or_empty(r.get("last_at")),
            )
            for r in cov["edges"]
        ],
        pr_history_from=_ts_or_empty(misc.get("pr_from")),
        pr_history_to=_ts_or_empty(misc.get("pr_to")),
        pr_history_last_fetch_at=_ts_or_empty(misc.get("pr_last_at")),
        pr_history_enabled=bool(settings.code_pr_history_enabled),
        pr_history_auth="token" if settings.code_github_token else "anonymous",
        context_packs_last_at=_ts_or_empty(misc.get("pack_last_at")),
        summaries_last_at=_ts_or_empty(misc.get("summary_last_at")),
    )


def _ts_or_empty(v: object) -> str:
    """LEFT JOIN misses surface as the epoch, not NULL — render as unknown."""
    t = str(v or "")
    return "" if not t or t.startswith("1970-01-01") else t


def _s(v: object) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").rstrip("\x00")
    return str(v or "")
