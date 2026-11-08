"""Weekly graph statistics: PageRank over the include graph, per-file
degrees, and the test↔source map.

PageRank runs in a thread (numpy power iteration, no scipy/networkx); a
failure leaves last week's ranks serving. code_summaries rows are read-
modify-written FULL (mirror-every-column) so the LLM summary text written
by code_summary survives a rank refresh.

test_map methods: `path_convention` (src/**/tests/ name-stem match) and
`pr_cooccurrence` (source+test changed together in >= 2 merged PRs).

Symbol fan-in: one aggregation
over live `calls` edges per namespace becomes loom.code_symbol_stats —
symbols outside the repo dropped, call sites under noise paths counted
separately, every row of a pass at one version, and symbols the previous
pass left with callers re-emitted at zero when they lost them all.

First pass: the supervisor asks `first_pass_due` when it
starts this worker; a configured namespace with live files whose ranks,
test map or symbol stats are missing — or whose stats are older than the
interval — makes the pass run at once instead of after the boot stagger.
The interval is kept afterwards. A test map the leg built and found
empty (`test_map_summary` with no rows, at the current `git_head`, within
the interval) is the namespace's answer, not a missing table.

Two loops: the cheap legs — ranks, test map, symbol stats,
a few seconds a namespace — run first, for every namespace that is owed
them (the first-pass rule per namespace: stats missing or older than the
interval), so the due conditions are met whatever the second loop manages;
then the unit-risk leg runs per namespace under one pass-level budget
(`code_graph_stats_unit_risk_budget_s`, from pass start, checked between
rounds and between namespaces), resumable from the table's own stamps. A
leg cut by the budget is `unit_risk_partial` on the outcome, not a failed
pass. Running every leg per namespace in order instead would hit the
deadline partway through, leave later namespaces owed their symbol stats, and
restart from namespace one on every retry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from server.code.store.index.graph_stats import GraphStatsState
from server.core.datetimes import naive_utc_now
from server.core.datetimes import utc_now as _utcnow
from server.workers import pass_context
from server.workers.status import record_pass

logger = logging.getLogger("loom.workers.code_graph_stats")


@dataclass
class CodeGraphStatsResult:
    files_ranked: int = 0
    test_map_rows: int = 0
    symbol_stats_rows: int = 0
    unit_risk_rows: int = 0  # Precomputed risk rows rewritten
    # The unit-risk leg stopped at its budget; `unit_risk_resume`
    # names where the next pass continues ("<namespace>:<path>" or the
    # namespaces not reached, comma-separated).
    unit_risk_partial: bool = False
    unit_risk_resume: str = ""
    namespaces_refreshed: int = 0  # Namespaces whose cheap legs were due
    # The risk calibration leg — fits written this pass, the
    # rows the last written fit read, its time-split holdout AUC, and the
    # namespaces skipped (fresh fit, no QA binding, restricted, too few rows)
    jit_risk_fits: int = 0
    jit_risk_fit_n: int = 0
    jit_risk_holdout_auc: float = 0.0
    jit_risk_skipped: int = 0
    # The pass's items are namespace-legs — the cheap legs
    # of every namespace owed them plus the unit-risk leg of every
    # namespace (when the leg is on); `legs_done` are the ones that ran to
    # the end (a leg cut by the budget or failed is planned, not done).
    legs_planned: int = 0
    legs_done: int = 0
    errors: list[str] = field(default_factory=list)

    def rows_written(self) -> int:
        return self.files_ranked + self.test_map_rows + self.symbol_stats_rows + self.unit_risk_rows


def pass_due(
    state: GraphStatsState,
    *,
    interval_seconds: float,
    now: datetime | None = None,
) -> str:
    """Pure: why one namespace needs the pass now, '' when it
    does not. `state` is CodeRepo.graph_stats_state — `files`, `ranks`,
    `test_map` (present or not), `symbol_stats_rows`, `symbol_stats_at`.
    A namespace with no live files is never due (the pass would return at
    once). Empty ranks or test map are due; with symbol stats on, an empty
    stats table is due and a stamp older than the interval is due — the
    table is the only record of the last pass that survives a restart
    (the in-process status registry does not), so the interval is
    measured from it. An empty test map is not due when the leg last ran
    within the interval, at the namespace's current `git_head`, and wrote
    no row (`test_map_ran_at` / `test_map_ran_commit`): the namespace has
    no tests the leg can map, and rerunning it would find none again."""
    if not state.get("files"):
        return ""
    if not state.get("ranks"):
        return "ranks_empty"
    if not state.get("test_map") and not _test_map_empty_by_design(
        state, interval_seconds=interval_seconds, now=now
    ):
        return "test_map_empty"
    if int(state.get("symbol_stats_rows") or 0) <= 0:
        return "symbol_stats_empty"
    at = state.get("symbol_stats_at")
    if isinstance(at, datetime):
        stamp = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
        moment = now or _utcnow()
        if (moment - stamp).total_seconds() > float(interval_seconds):
            return "symbol_stats_stale"
    return ""


def _test_map_empty_by_design(
    state: GraphStatsState, *, interval_seconds: float, now: datetime | None
) -> bool:
    """The test-map leg ran and found nothing to map, recently enough and
    at the commit the namespace is at now. A leg never run (no stamp) is
    never empty by design."""
    at = state.get("test_map_ran_at")
    if not isinstance(at, datetime):
        return False
    if str(state.get("test_map_ran_commit") or "") != str(state.get("git_head") or ""):
        return False
    stamp = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return ((now or _utcnow()) - stamp).total_seconds() <= float(interval_seconds)


async def due_namespaces() -> dict[str, str]:
    """Label -> reason for every configured namespace whose
    cheap legs are owed now (see `pass_due`); a state read that fails reads
    as due (the pass runs; the table tells). Both the supervisor's first-pass
    check and the pass's own first loop ask this, so a boot that finds two
    namespaces with an empty table runs the legs for those two, not ten."""
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo
    from server.settings import settings

    repo = CodeRepo()
    out: dict[str, str] = {}
    for cfg in configured_repos():
        try:
            state = await repo.graph_stats_state(cfg.org, cfg.namespace)
        except Exception as e:  # noqa: BLE001 — unknown state: run the legs
            logger.warning(
                "code_graph_stats: state read failed for %s (%s); treating as due", cfg.label, e
            )
            out[cfg.label] = "state_unknown"
            continue
        why = pass_due(
            state,
            interval_seconds=settings.code_graph_stats_interval_seconds,
        )
        if why:
            out[cfg.label] = why
    return out


async def first_pass_due() -> bool:
    """True when any configured namespace needs the pass now
    (see `pass_due`). Four small reads per namespace — three existence
    reads and the published summaries, no count — once per supervisor
    start. The pass itself runs the cheap legs only for the
    namespaces that are due, so this answer and the work agree."""
    due = await due_namespaces()
    for label, why in due.items():
        logger.info("code_graph_stats: first pass due for %s: %s", label, why)
    return bool(due)


async def run_pass() -> CodeGraphStatsResult:
    result = CodeGraphStatsResult()
    with record_pass("code_graph_stats") as rec:
        await _run(result)
        rec.outcome = {
            # The row's counters first, under their own names
            "items_planned": result.legs_planned,
            "items_done": result.legs_done,
            "rows_written": result.rows_written(),
            "files_ranked": result.files_ranked,
            "test_map_rows": result.test_map_rows,
            "symbol_stats_rows": result.symbol_stats_rows,
            "unit_risk_rows": result.unit_risk_rows,
            "unit_risk_partial": result.unit_risk_partial,
            "unit_risk_resume": result.unit_risk_resume,
            "namespaces_refreshed": result.namespaces_refreshed,
            "jit_risk_fits": result.jit_risk_fits,
            "jit_risk_fit_n": result.jit_risk_fit_n,
            "jit_risk_holdout_auc": result.jit_risk_holdout_auc,
            "jit_risk_skipped": result.jit_risk_skipped,
            "errors": len(result.errors),
        }
    return result


def _resolve_includes(
    raw_edges: list[tuple[str, str]], known_paths: set[str]
) -> list[tuple[str, str]]:
    """Include targets are as-written (`Columns/IColumn.h`); resolve each to
    a known repo path by unique suffix match, else drop (system headers)."""
    by_suffix: dict[str, list[str]] = {}
    for p in known_paths:
        name = p.rsplit("/", 1)[-1]
        by_suffix.setdefault(name, []).append(p)
    resolved: list[tuple[str, str]] = []
    for src, target in raw_edges:
        # exact repo-relative or src/-prefixed target
        for candidate in (target, f"src/{target}", f"base/{target}"):
            if candidate in known_paths:
                resolved.append((src, candidate))
                break
        else:
            name = target.rsplit("/", 1)[-1]
            candidates = [p for p in by_suffix.get(name, []) if p.endswith(target)]
            if len(candidates) == 1:
                resolved.append((src, candidates[0]))
    return resolved


def _stem(path: str) -> str:
    return PurePosixPath(path).stem.lower().replace("_", "")


def _path_convention_rows(org: str, ns: str, known: set[str]) -> list[dict[str, Any]]:
    """`code_test_map` rows pairing each test file with the source files whose
    stem is a substring of its stem, for stems that name at most three files.
    Looks up each substring of the test stem up to the longest source stem,
    rather than scanning every stem for every test."""
    from server.code.store.index import build_row

    src_by_stem: dict[str, list[str]] = {}
    for p in known:
        if p.startswith("src/") and "/tests/" not in p:
            src_by_stem.setdefault(_stem(p), []).append(p)
    # A substring is never empty, so an empty stem never matches.
    mappable = {s: paths for s, paths in src_by_stem.items() if len(paths) <= 3}
    longest = max((len(s) for s in mappable), default=0)
    rows = []
    for t in known:
        if not (t.startswith("tests/") or "/tests/" in t):
            continue
        t_stem = _stem(t)
        n = len(t_stem)
        subs = {t_stem[i:j] for i in range(n) for j in range(i + 1, min(n, i + longest) + 1)}
        for s_stem in subs & mappable.keys():
            paths = mappable[s_stem]
            for src_path in paths:
                rows.append(
                    build_row(
                        "code_test_map",
                        org=org,
                        namespace=ns,
                        src_path=src_path,
                        method="path_convention",
                        test_path=t,
                        confidence=0.7 if len(paths) == 1 else 0.4,
                        evidence=f"stem '{s_stem}'",
                    )
                )
    return rows


def compute_symbol_stats(
    org: str,
    ns: str,
    fan_in_rows: list[dict[str, Any]],
    definition_sites: dict[str, tuple[int, int, str]],
    *,
    previous_ids: set[str] | None = None,
    version: int | None = None,
    computed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Full loom.code_symbol_stats rows for one pass. Pure: the aggregation rows come from
    CodeRepo.symbol_fan_in, the definition sites from CodeRepo.symbol_definition_sites.
    Symbols outside the repo (std::, boost::, testing:: ... — is_external_symbol) are
    dropped: symbols like shared_ptr::operator-> or the gtest assertion helpers collect tens
    of thousands of in-edges, none of them an answer about the repo. Every row of a pass
    carries the same version and computed_at so a half-written pass cannot interleave with
    the previous one under FINAL. Ids in `previous_ids` that no longer have callers are
    re-emitted at zero — the read shapes drop zero rows, so a symbol that lost its last
    caller disappears from the field without a tombstone column. fan_in_name is the largest
    fan_in_nontest among the rows sharing a qualified name (tiers, overloads, template
    instantiations); a chunk carries one id, the field shows the name's number."""
    from server.code.domain.external_symbols import is_external_symbol
    from server.code.store.index import build_row, version_now

    ver = version or version_now()
    stamp = computed_at or naive_utc_now()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in fan_in_rows:
        sid = str(r.get("symbol_id") or "")
        qn = str(r.get("qualified_name") or "")
        if not sid or is_external_symbol(qn):
            continue
        defined_files, is_test, path = definition_sites.get(sid, (0, 0, ""))
        seen.add(sid)
        rows.append(
            build_row(
                "code_symbol_stats",
                org=org,
                namespace=ns,
                symbol_id=sid,
                qualified_name=qn,
                path=path,
                fan_in_calls=int(r.get("fan_in_calls") or 0),
                fan_in_nontest=int(r.get("fan_in_nontest") or 0),
                caller_files=int(r.get("caller_files") or 0),
                defined_files=min(int(defined_files), 65535),
                is_test=1 if is_test else 0,
                computed_at=stamp,
                version=ver,
            )
        )
    name_max: dict[str, int] = {}
    for row in rows:
        qn = str(row["qualified_name"])
        name_max[qn] = max(name_max.get(qn, 0), int(row["fan_in_nontest"]))
    for row in rows:
        row["fan_in_name"] = name_max[str(row["qualified_name"])]
    for sid in sorted((previous_ids or set()) - seen):
        rows.append(
            build_row(
                "code_symbol_stats",
                org=org,
                namespace=ns,
                symbol_id=sid,
                computed_at=stamp,
                version=ver,
            )
        )
    return rows


async def _write_symbol_stats(repo: Any, org: str, ns: str, result: CodeGraphStatsResult) -> None:
    fan_in_rows = await repo.symbol_fan_in(org, ns)
    sites = await repo.symbol_definition_sites(org, ns)
    previous = await repo.live_symbol_stats_ids(org, ns)
    rows = compute_symbol_stats(org, ns, fan_in_rows, sites, previous_ids=previous)
    for start in range(0, len(rows), 5000):
        pass_context.check_stop()
        await repo.insert_rows("code_symbol_stats", rows[start : start + 5000])
    result.symbol_stats_rows += len(rows)


async def _write_unit_risk(
    repo: Any,
    org: str,
    ns: str,
    model: str,
    result: CodeGraphStatsResult,
    *,
    deadline: float | None = None,
) -> None:
    """Every live unit's precomputed risk row, rewritten once
    a week with churn and the tested set read once for the namespace — the
    refresh for the facts that move without the unit (churn with the PR
    backfill, callers with other files' edits). Resumable
    (only paths without a row, or with one that would go stale before the
    next pass) and cut cleanly at `deadline` with the resume point noted."""
    from server.code.app import unit_risk as code_unit_risk

    done = await code_unit_risk.refresh_namespace(repo, org, ns, model, deadline=deadline)
    result.unit_risk_rows += done.rows_written
    if done.partial:
        result.unit_risk_partial = True
        result.unit_risk_resume = f"{ns}:{done.resume_at}"
    logger.info(
        "code_graph_stats: unit risk %s — %d live paths, %d due, %d built, %d rows%s",
        ns,
        done.paths_live,
        done.paths_due,
        done.paths_built,
        done.rows_written,
        f", partial (resume at {done.resume_at})" if done.partial else "",
    )


def unit_risk_deadline(started: float) -> float:
    """The monotonic instant the unit-risk leg stops
    scheduling rounds — pass start plus `code_graph_stats_unit_risk_budget_s`."""
    from server.settings import settings

    return started + max(0.0, float(settings.code_graph_stats_unit_risk_budget_s))


async def _refit_jit_risk(repos: list[Any], result: CodeGraphStatsResult) -> None:
    """The risk calibration of every namespace bound to a QA
    namespace, refitted when its stored fit is a week old — one QA statement
    and a ~0.3 s fit per namespace, never fatal to the pass."""
    from server.code.app.index import risk_fit
    from server.code.store.index import CodeRepo

    repo = CodeRepo()
    for cfg in repos:
        out = await risk_fit.refit_namespace(repo, cfg.org, cfg.namespace)
        if out.status == "written":
            result.jit_risk_fits += 1
            result.jit_risk_fit_n = out.n
            result.jit_risk_holdout_auc = out.holdout_auc
            logger.info("code_graph_stats: risk fit %s — %s", cfg.label, out.detail)
        elif out.status == "failed":
            result.errors.append(f"{cfg.label}: jit_risk: {out.detail}")
        else:
            result.jit_risk_skipped += 1


async def _run(result: CodeGraphStatsResult) -> None:
    """Loop one: every namespace's cheap legs. Loop two: the unit-risk leg per
    namespace under the pass budget. Then the risk calibration
    leg, seconds for every bound namespace."""
    from server.code.domain.index.repos import configured_repos

    started = time.monotonic()
    repos = configured_repos()
    # The cheap legs run for the namespaces that are owed
    # them — stats missing or older than the interval (the first-pass rule,
    # per namespace).
    due = await due_namespaces()
    result.namespaces_refreshed = len(due)
    # Items are namespace-legs; planned up front so a pass cut
    # short still says how much it set out to do.
    result.legs_planned += len(due)
    for cfg in repos:
        if cfg.label not in due:
            continue
        pass_context.set_namespace(cfg.namespace)  # Where a cut pass stopped
        pass_context.check_stop()
        try:
            await _run_repo(cfg.org, cfg.namespace, result)
            result.legs_done += 1
        except Exception as e:
            result.errors.append(f"{cfg.label}: {type(e).__name__}: {e}")
            logger.warning("code_graph_stats: stats legs failed for %s", cfg.label, exc_info=True)
            pass_context.note_failure(e, cfg.namespace)
    result.legs_planned += len(repos)
    deadline = unit_risk_deadline(started)
    for i, cfg in enumerate(repos):
        if time.monotonic() >= deadline:
            left = [c.namespace for c in repos[i:]]
            result.unit_risk_partial = True
            result.unit_risk_resume = result.unit_risk_resume or ",".join(left)
            logger.warning(
                "code_graph_stats: unit-risk budget reached before %d namespace(s): %s "
                "— the next pass continues there",
                len(left),
                ",".join(left),
            )
            break
        pass_context.set_namespace(cfg.namespace)
        pass_context.check_stop()
        try:
            before = result.unit_risk_partial
            await _run_unit_risk(cfg.org, cfg.namespace, result, deadline)
            if result.unit_risk_partial == before:
                result.legs_done += 1  # a leg cut by the budget is planned, not done
        except Exception as e:
            result.errors.append(f"{cfg.label}: unit_risk: {type(e).__name__}: {e}")
            logger.exception("code_graph_stats: unit risk failed for %s", cfg.label)
            pass_context.note_failure(e, f"{cfg.namespace}:unit_risk")
    await _refit_jit_risk(list(repos), result)


async def _run_unit_risk(org: str, ns: str, result: CodeGraphStatsResult, deadline: float) -> None:
    from server.code.store.index import CodeRepo
    from server.embedding.embedder import get_embedder

    repo = CodeRepo()
    before = result.unit_risk_rows
    await _write_unit_risk(repo, org, ns, get_embedder().model_name, result, deadline=deadline)
    # This leg writes no stats row, and a leg that wrote no risk row
    # leaves the published risk summary true: neither is recounted.
    unchanged = {"symbol_stats_summary"}
    if result.unit_risk_rows == before:
        unchanged.add("unit_risk_summary")
    await _publish_summaries(repo, org, ns, unchanged=frozenset(unchanged))


async def _publish_summaries(
    repo: Any, org: str, ns: str, *, unchanged: frozenset[str] = frozenset()
) -> None:
    """`refresh_stats_summaries`, soft: a pass never fails on its summary.
    `unchanged` names the summaries whose table the leg wrote no row to."""
    refresh = getattr(repo, "refresh_stats_summaries", None)
    if refresh is None:
        return
    try:
        await refresh(org, ns, unchanged=unchanged)
    except Exception as e:  # noqa: BLE001 — the rows on the tables keep serving
        logger.debug("code_graph_stats: summaries not published for %s: %s", ns, e)


async def _git_head(repo: Any, org: str, ns: str) -> str:
    """The namespace's ingest watermark, '' when unset or unreadable."""
    try:
        return str(await repo.get_state(org, ns, "git_head") or "")
    except Exception as e:  # noqa: BLE001 — a missing head only makes the next check due
        logger.debug("code_graph_stats: git_head unavailable for %s: %s", ns, e)
        return ""


async def _publish_test_map(repo: Any, org: str, ns: str, n: int, head: str) -> None:
    """The `test_map_summary` row, soft: an unpublished row leaves an empty
    test map due again, which is what it was before."""
    from server.code.store.index import summary_stamp

    publish = getattr(repo, "publish_test_map_summary", None)
    if publish is None:
        return
    try:
        await publish(org, ns, n, summary_stamp(_utcnow()), head)
    except Exception as e:  # noqa: BLE001 — the leg's rows are written; the row is advisory
        logger.debug("code_graph_stats: test map summary not published for %s: %s", ns, e)


# File cards whose full rows one statement reads when their ranks changed.
_CARD_READ_BATCH = 1000


def _rank_unchanged(live: dict[str, Any], rank: float, in_degree: int, out_degree: int) -> bool:
    """A card whose rank and degrees are what it already
    holds is not re-emitted. Re-emitting every card each pass would mirror
    the whole file, vector included, and multiply code_summaries' physical
    rows per live card, which every FINAL card read pays for. The rank is
    recomputed each pass, so equal means within float noise."""
    old = float(live.get("pagerank") or 0.0)
    return (
        abs(old - float(rank)) <= 1e-12 + 1e-6 * abs(float(rank))
        and int(live.get("in_degree") or 0) == int(in_degree)
        and int(live.get("out_degree") or 0) == int(out_degree)
    )


async def _run_repo(org: str, ns: str, result: CodeGraphStatsResult) -> None:
    from server.code.domain.index.pagerank import degrees, pagerank
    from server.code.store.index import CodeRepo, build_row
    from server.embedding.embedder import get_embedder

    repo = CodeRepo()
    model = get_embedder().model_name

    head = await _git_head(repo, org, ns)  # before the files: the commit they are at
    file_hashes = await repo.live_file_hashes(org, ns)
    if not file_hashes:
        return
    known = set(file_hashes)

    raw_includes = await repo.include_edges(org, ns)
    edges = _resolve_includes(raw_includes, known)

    try:
        ranks = await asyncio.to_thread(pagerank, edges)
        degs = degrees(edges)
        pass_context.check_stop()  # the thread ran on past a lost lease
    except Exception as e:
        result.errors.append(f"pagerank: {type(e).__name__}: {e}")
        logger.exception("code_graph_stats: pagerank failed")
        pass_context.note_failure(e, f"{ns}:pagerank")
        ranks, degs = {}, {}

    if ranks:
        # Compare against the cards' rank columns only; the full rows (vector
        # included, mirrored forward) are read for the cards that change.
        held = await repo.file_card_ranks(org, ns, model)
        changed: list[tuple[str, float, int, int]] = []
        for path in known:
            rank = ranks.get(path, 0.0)
            ind, outd = degs.get(path, (0, 0))
            old = held.get(path)
            if old is not None and _rank_unchanged(
                {"pagerank": old[0], "in_degree": old[1], "out_degree": old[2]}, rank, ind, outd
            ):
                continue
            changed.append((path, rank, ind, outd))
        mirrored = sorted(p for p, *_ in changed if p in held)
        existing: dict[str, dict[str, Any]] = {}
        for start in range(0, len(mirrored), _CARD_READ_BATCH):
            for r in await repo.live_file_cards(
                org, ns, model, mirrored[start : start + _CARD_READ_BATCH]
            ):
                existing[str(r["scope_path"])] = r
        rows = []
        for path, rank, ind, outd in changed:
            live = existing.get(path)
            if live is not None:
                row = CodeRepo.tombstone_of("code_summaries", live)  # full mirror
                row["tombstoned_at"] = None
                row["pagerank"] = rank
                row["in_degree"] = ind
                row["out_degree"] = outd
            else:
                row = build_row(
                    "code_summaries",
                    org=org,
                    namespace=ns,
                    embedding_model=model,
                    scope_type="file",
                    scope_path=path,
                    pagerank=rank,
                    in_degree=ind,
                    out_degree=outd,
                )
            rows.append(row)
        for start in range(0, len(rows), 5000):
            pass_context.check_stop()
            await repo.insert_rows("code_summaries", rows[start : start + 5000])
        result.files_ranked = len(rows)

    # ---- test map ----------------------------------------------------------
    # Seconds of pure Python on a large repository: in a thread, so the event
    # loop (and the supervisor lease it renews) keeps running.
    test_rows = await asyncio.to_thread(_path_convention_rows, org, ns, known)
    pass_context.check_stop()  # the thread ran on past a lost lease
    pairs = await repo.pr_file_pairs(org, ns)
    for pr in pairs:
        test_rows.append(
            build_row(
                "code_test_map",
                org=org,
                namespace=ns,
                src_path=str(pr["src_path"]),
                method="pr_cooccurrence",
                test_path=str(pr["test_path"]),
                confidence=min(0.9, 0.3 + 0.1 * int(pr["n"])),
                evidence=f"co-changed in {pr['n']} PRs",
            )
        )
    for start in range(0, len(test_rows), 5000):
        await repo.insert_rows("code_test_map", test_rows[start : start + 5000])
    result.test_map_rows = len(test_rows)
    await _publish_test_map(repo, org, ns, len(test_rows), head)

    # ---- symbol fan-in -------------------------------------
    try:
        await _write_symbol_stats(repo, org, ns, result)
    except Exception as e:
        result.errors.append(f"symbol_stats: {type(e).__name__}: {e}")
        logger.exception("code_graph_stats: symbol stats failed")
        pass_context.note_failure(e, f"{ns}:symbol_stats")
    # Publish what the leg left, so code.index_status
    # reads a state row instead of recounting the table under FINAL. This
    # leg writes no unit-risk row.
    await _publish_summaries(repo, org, ns, unchanged=frozenset({"unit_risk_summary"}))
    # the per-unit risk leg runs in `_run`'s second loop, after
    # every namespace's cheap legs; it reads the test map
    # written above.
