"""Replay gate: does the dependents pass close the stale-edge hole?

The fast tier derives a file's edges when THAT file changes. A commit that
adds, removes or re-signs a definition in one file leaves every untouched
file that names it with a stale answer. The bounded per-commit repair
(dependents re-resolution + closure repair) is measured by this replay.

Design (bench namespaces, never registered, no embedding spend):

    plan     K = --base-rev on the bare mirror; the window is the next
             --window first-parent commits; the replayed commits are those
             that touch the indexed slice (--prefix), capped at --max-commits.
    base     full index of the slice at K into <ns>-base (embedder=None:
             chunk rows are dim=0 stubs, no describe), then an edges-only
             SETTLE pass over every slice path — a concurrent backfill
             resolves early files before late files' symbols exist, and a
             namespace that starts with order artefacts would let arm B
             "repair" what the backfill never derived.
    clone    <ns>-base rows copied into <ns>-a / -b / -c with INSERT SELECT
             * REPLACE — identical starting states, bit for bit (the
             code_symbols copy feeds code_symbol_names through its view).
    replay   the replayed commits through the worker's own _ingest_commit,
             per arm, in one process, with the two settings toggled:
                 a  both off
                 b  dependents on + closure repair on
                 c  dependents on, closure repair off
             Every ClickHouse statement carries a log_comment
             `d2r <arm><run-tag> <i> <phase>` (phase = touched | deps), so
             system.query_log gives the dependents pass's own rows/bytes,
             and a re-run under another --run-tag is counted apart.
             Resumable: progress per arm is in the state dir; a killed run
             continues at the next commit.
    gt       full index of the slice at the LAST replayed commit into
             <ns>-gt (+ settle) — the ground truth every arm is compared to.
    compare  live edge sets and live declined-site sets per arm vs gt:
             missing (true edges the arm lacks), stale (edges the arm keeps
             that gt does not have), recall; declined sites whose verdict
             (disposition, candidates) differs under the same key; the
             audit counts A / B (scripts/ops/code_edge_audit.py) per
             arm; wall time and rows/bytes from query_log per phase.

Fidelity notes, stated up front: resolution is namespace-scoped, so a slice
has more unique names (more 0.6 edges, more `unresolved` sites) than the
full repository — the hole is likelier LARGER here than on code-clickhouse
and the relative numbers, not the absolute ones, carry. Reads run under
select_sequential_consistency (--no-sequential-reads to switch off) so a
replica's replication lag does not masquerade as a stale row; it slows both
arms equally.

Run (from a worktree, env from the deployment's .env, no server needed):

    PYTHONPATH=. python bench/code_dependents_replay.py \
        --namespace bench-code-d2-replay --base-rev master~400 --window 400 \
        --max-commits 60 --prefix src/Storages/MergeTree/ --prefix src/Interpreters/ \
        --state-dir /path/to/state  all

Phases can be run one at a time (`plan`, `base`, `clone`, `replay a`, ...,
`gt`, `compare`, `report`); `all` runs them in order and skips what the
state file says is done. `--run-tag=-r3` suffixes the arm namespaces, logs
and progress keys so arms can be re-run against the same base and ground
truth after a code change (a value starting with `-` needs the `=` form).
Leaves its rows behind: the erasure saga does not cover the code_* tables,
so purge is by hand.

Failure classes this replay is built to catch: a declined-site key-diff that never
rewrites a site whose verdict changed under the same key; liveness decided with a
NULL-skipping `argMax` over `tombstoned_at`, which reads a re-inserted key as dead; and
the commit's own paths staying declined when their callee is defined later in the same
commit. Clone verification counts under FINAL — a raw count() of the copy drops as soon
as the destination merges the duplicated (live + tombstone) keys.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# arm -> (dependents, closure repair).
ARMS: dict[str, tuple[bool, bool]] = {
    "a": (False, False),
    "b": (True, True),
    "c": (True, False),
}
CLONE_TABLES = (
    "code_files",
    "code_chunks",
    "code_symbols",
    "code_edges",
    "code_call_sites_unresolved",
    "code_index_state",
)
EDGE_KINDS = ("calls", "inherits", "fixture_use")  # includes are file-level, order-free


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def load_env(env_file: str) -> None:
    """The deployment's .env, line by line (JSON values intact, inline
    comments stripped on scalars only), never overriding the process env;
    telemetry exporters dropped so the replay stays out of the fleet's
    traces. Must run before server.settings is imported."""
    p = Path(env_file)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if v[:1] not in "{\"'" and " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            if v[:1] in "\"'" and v[-1:] == v[:1]:
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)
    for k in list(os.environ):
        if k.startswith("LOOM_OTEL") or k.startswith("OTEL_"):
            os.environ.pop(k)
    os.environ["LOOM_BACKGROUND_WORKERS"] = "off"
    os.environ["LOOM_BOOT_MIGRATE"] = "false"


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


@dataclass
class State:
    base_sha: str = ""
    window_shas: list[str] = field(default_factory=list)  # first-parent, oldest first
    replay_shas: list[str] = field(default_factory=list)  # those touching the slice
    slice_paths: int = 0
    done: dict[str, bool] = field(default_factory=dict)  # phase -> done
    progress: dict[str, int] = field(default_factory=dict)  # arm -> commits replayed
    timings: dict[str, float] = field(default_factory=dict)  # phase -> seconds

    @classmethod
    def load(cls, path: Path) -> State:
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=1))


# ---------------------------------------------------------------------------
# helpers around loom's own code
# ---------------------------------------------------------------------------


def _arm_ns(args: argparse.Namespace, arm: str) -> str:
    return f"{args.namespace}-{arm}{args.run_tag}"


class _GitNoSubject:
    """gitrepo with commit_subject blanked: the replay measures edges, not
    context packs (a `(#NNNNN)` subject would write code_context_packs rows
    and read the PR history table on every arm alike)."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def commit_subject(self, git_dir: str, sha: str) -> str:
        return ""


async def _slice_paths(
    gitrepo: Any, git_dir: str, sha: str, prefixes: tuple[str, ...]
) -> list[str]:
    return await gitrepo.ls_tree(git_dir, sha, prefixes)


async def full_index(
    repo: Any,
    gitrepo: Any,
    ingest_file: Any,
    *,
    org: str,
    ns: str,
    git_dir: str,
    sha: str,
    prefixes: tuple[str, ...],
    model: str,
    concurrency: int,
    label: str,
) -> dict[str, int]:
    """Backfill of the slice at `sha` (stubs, no describe) followed by the
    edges-only settle pass. Mirrors code_backfill._backfill's loop."""
    from server.settings import settings

    paths = await _slice_paths(gitrepo, git_dir, sha, prefixes)
    counters = {
        "files": 0,
        "chunks": 0,
        "symbols": 0,
        "edges": 0,
        "misses": 0,
        "errors": 0,
        "settle_edges_new": 0,
        "settle_edges_tombstoned": 0,
        "settle_misses_new": 0,
        "settle_misses_tombstoned": 0,
        "settle_misses_refreshed": 0,
        "paths": len(paths),
    }
    sem = asyncio.Semaphore(concurrency)
    t0 = time.monotonic()

    async def _one(path: str, edges_only: bool) -> None:
        async with sem:
            source = await gitrepo.show_file(git_dir, sha, path)
            if source is None:
                return
            try:
                st = await ingest_file(
                    repo,
                    org,
                    ns,
                    path=path,
                    source=source,
                    commit_sha=sha,
                    embedding_model=model,
                    embedder=None,
                    max_chunk_chars=settings.code_chunk_max_chars,
                    edges_only=edges_only,
                )
            except Exception as e:  # noqa: BLE001 — per file
                counters["errors"] += 1
                print(f"  ! {path}: {type(e).__name__}: {e}", file=sys.stderr)
                return
            if st.skipped:
                return
            if edges_only:
                counters["settle_edges_new"] += st.edges_new
                counters["settle_edges_tombstoned"] += st.edges_tombstoned
                counters["settle_misses_new"] += st.misses_new
                counters["settle_misses_tombstoned"] += st.misses_tombstoned
                counters["settle_misses_refreshed"] += getattr(st, "misses_refreshed", 0)
            else:
                counters["files"] += 1
                counters["chunks"] += st.chunks_new
                counters["symbols"] += st.symbols_new
                counters["edges"] += st.edges_new
                counters["misses"] += st.misses_new

    for start in range(0, len(paths), 200):
        await asyncio.gather(*(_one(p, False) for p in paths[start : start + 200]))
        print(f"  [{label} index {min(start + 200, len(paths))}/{len(paths)}] {counters}")
    counters["index_s"] = int(time.monotonic() - t0)
    t1 = time.monotonic()
    for start in range(0, len(paths), 200):
        await asyncio.gather(*(_one(p, True) for p in paths[start : start + 200]))
    counters["settle_s"] = int(time.monotonic() - t1)
    print(f"  [{label} settle] {counters}")
    await repo.set_state(org, ns, "git_head", sha, detail=f"{label} backfill+settle")
    return counters


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------


async def phase_plan(args: argparse.Namespace, st: State, gitrepo: Any) -> None:
    from server.code.app.index.gitrepo import _git

    base = (await _git(args.git_dir, "rev-parse", args.base_rev)).strip()
    out = await _git(
        args.git_dir,
        "rev-list",
        "--first-parent",
        "--reverse",
        "-n",
        str(args.window),
        f"{base}..{args.branch}",
    )
    window = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(window) > args.window:
        window = window[: args.window]
    replay: list[str] = []
    prev = base
    for sha in window:
        entries = await gitrepo.diff_files(args.git_dir, prev, sha)
        prev = sha
        if any(e.path.startswith(args.prefixes) for e in entries):
            replay.append(sha)
        if len(replay) >= args.max_commits:
            break
    st.base_sha, st.window_shas, st.replay_shas = base, window, replay
    st.slice_paths = len(await _slice_paths(gitrepo, args.git_dir, base, args.prefixes))
    st.done["plan"] = True
    print(
        f"plan: K={base[:12]} window={len(window)} replay={len(replay)} "
        f"slice files at K={st.slice_paths} prefixes={args.prefixes}"
    )


async def _count(ch: Any, table: str, org: str, ns: str) -> int:
    """Live-key count under FINAL: a plain count() on the copy drops as soon
    as the destination's ReplacingMergeTree merges the duplicated
    (live row + tombstone) keys it just received, so raw row counts of
    source and copy disagree within seconds without a row being lost."""
    (r,) = await ch.query_all(
        f"SELECT count() AS n FROM loom.{table} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "SETTINGS select_sequential_consistency = 1",
        {"org": org, "ns": ns},
    )
    return int(r["n"])


async def phase_clone(args: argparse.Namespace, st: State, ch: Any) -> None:
    for arm in ARMS:
        target = _arm_ns(args, arm)
        if await _count(ch, "code_files", args.org, target):
            print(f"  clone {arm}: already populated, verifying only")
            continue
        for table in CLONE_TABLES:
            # The filter lives in a subquery on purpose: an alias in the SELECT
            # list is visible to WHERE, so `... REPLACE ('x' AS namespace) ...
            # WHERE namespace = 'base'` compares 'x' and copies nothing.
            await ch.execute(
                f"INSERT INTO loom.{table} SELECT * REPLACE ({{target:String}} AS namespace) "
                f"FROM (SELECT * FROM loom.{table} "
                f"WHERE org = {{org:String}} AND namespace = {{src:String}})",
                {"target": target, "org": args.org, "src": f"{args.namespace}-base"},
            )
    # verify under sequential consistency (a fresh SharedMergeTree count can lag)
    for table in CLONE_TABLES:
        src = await _count(ch, table, args.org, f"{args.namespace}-base")
        for arm in ARMS:
            dst = await _count(ch, table, args.org, _arm_ns(args, arm))
            if src != dst:
                raise RuntimeError(f"clone mismatch {table} base={src} {arm}={dst}")
        print(f"  clone {table}: {src} rows x {len(ARMS)} arms")
    st.done["clone" + args.run_tag] = True


@dataclass
class CommitRow:
    i: int
    sha: str
    files: int
    wall_ms: int
    touched_ms: int
    deps_ms: int
    closure_ms: int
    edges_new: int
    edges_tombstoned: int
    misses_new: int
    misses_tombstoned: int
    misses_refreshed: int
    dep_edges_new: int = 0
    dep_edges_tombstoned: int = 0
    dep_misses_new: int = 0
    dep_misses_tombstoned: int = 0
    dep_misses_refreshed: int = 0
    dependents: int = 0
    dependents_candidates: int = 0
    dependents_queued: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    closure_resolved: int = 0
    names_added: int = 0
    names_removed: int = 0
    symbols_tombstoned: int = 0
    errors: int = 0


async def phase_replay(
    args: argparse.Namespace, st: State, arm: str, repo: Any, gitrepo: Any
) -> None:
    from server.code.app.index import dependents as dep_mod
    from server.code.app.index import ingest as ingest_mod
    from server.code.app.index import pass_budget as ingest_budget
    from server.code.app.index import pass_commit as ingest_commit
    from server.code.store.index import build_row
    from server.core.ch import set_query_tag
    from server.settings import settings

    deps_on, closure_on = ARMS[arm]
    settings.code_ingest_dependents_enabled = deps_on
    settings.code_closure_repair_enabled = closure_on
    ns = _arm_ns(args, arm)
    log = Path(args.state_dir) / f"replay_{arm}{args.run_tag}.jsonl"
    start = st.progress.get(arm + args.run_tag, 0)
    if start >= len(st.replay_shas):
        st.done[f"replay_{arm}{args.run_tag}"] = True
        return
    git = _GitNoSubject(gitrepo)

    # per-commit capture: every ingest_file call (touched files are not
    # edges_only; dependents are) and the dependents entry point, tagged
    cur: dict[str, Any] = {}

    async def ingest_capture(*a: Any, **kw: Any) -> Any:
        stats = await ingest_mod.ingest_file(*a, **kw)
        key = "dep" if kw.get("edges_only") else "touched"
        c = cur.setdefault(
            key,
            {
                "edges_new": 0,
                "edges_tombstoned": 0,
                "misses_new": 0,
                "misses_tombstoned": 0,
                "misses_refreshed": 0,
                "n": 0,
            },
        )
        c["n"] += 1
        for k in (
            "edges_new",
            "edges_tombstoned",
            "misses_new",
            "misses_tombstoned",
            "misses_refreshed",
        ):
            c[k] += int(getattr(stats, k, 0))
        return stats

    async def tombstone_capture(*a: Any, **kw: Any) -> Any:
        stats = await ingest_mod.tombstone_file(*a, **kw)
        c = cur.setdefault(
            "touched",
            {
                "edges_new": 0,
                "edges_tombstoned": 0,
                "misses_new": 0,
                "misses_tombstoned": 0,
                "misses_refreshed": 0,
                "n": 0,
            },
        )
        c["n"] += 1
        c["edges_tombstoned"] += stats.edges_tombstoned
        c["misses_tombstoned"] += stats.misses_tombstoned
        return stats

    real_run = dep_mod.run_for_commit

    async def run_tagged(*a: Any, **kw: Any) -> Any:
        set_query_tag(f"d2r {arm}{args.run_tag} {cur['i']} deps")
        try:
            closure, deps = await real_run(*a, **kw)
        finally:
            set_query_tag(f"d2r {arm}{args.run_tag} {cur['i']} touched")
        cur["deps"] = deps
        cur["closure"] = closure
        delta = kw.get("delta")
        if delta is not None:
            cur["delta"] = (
                len(delta.names_added),
                len(delta.names_removed),
                len(delta.symbols_tombstoned),
            )
        return closure, deps

    dep_mod.run_for_commit = run_tagged  # _resolve_dependents imports it per call
    try:
        base = st.base_sha if start == 0 else st.replay_shas[start - 1]
        for i in range(start, len(st.replay_shas)):
            sha = st.replay_shas[i]
            cur.clear()
            cur["i"] = i
            result = ingest_budget.CodeIngestResult()
            set_query_tag(f"d2r {arm}{args.run_tag} {i} touched")
            t0 = time.monotonic()
            await ingest_commit._ingest_commit(
                repo,
                args.org,
                ns,
                args.git_dir,
                base,
                sha,
                embedder=_StubEmbedder(args.model),
                llm=None,
                describe=False,
                prefixes=args.prefixes,
                result=result,
                build_pack=None,
                estimate_tokens=None,
                build_row=build_row,
                gitrepo=git,
                ingest_file=ingest_capture,
                tombstone_file=tombstone_capture,
            )
            wall = int((time.monotonic() - t0) * 1000)
            set_query_tag("")
            await repo.set_state(args.org, ns, "git_head", sha, detail=f"replay {arm}")
            t = cur.get("touched", {})
            dp = cur.get("dep", {})
            deps = cur.get("deps")
            closure = cur.get("closure")
            delta = cur.get("delta", (0, 0, 0))
            row = CommitRow(
                i=i,
                sha=sha,
                files=int(t.get("n", 0)),
                wall_ms=wall,
                touched_ms=wall - result.dependents_ms - result.closure_ms,
                deps_ms=result.dependents_ms,
                closure_ms=result.closure_ms,
                edges_new=int(t.get("edges_new", 0)),
                edges_tombstoned=int(t.get("edges_tombstoned", 0)),
                misses_new=int(t.get("misses_new", 0)),
                misses_tombstoned=int(t.get("misses_tombstoned", 0)),
                misses_refreshed=int(t.get("misses_refreshed", 0)),
                dep_edges_new=int(dp.get("edges_new", 0)),
                dep_edges_tombstoned=int(dp.get("edges_tombstoned", 0)),
                dep_misses_new=int(dp.get("misses_new", 0)),
                dep_misses_tombstoned=int(dp.get("misses_tombstoned", 0)),
                dep_misses_refreshed=int(dp.get("misses_refreshed", 0)),
                dependents=deps.reingested if deps else 0,
                dependents_candidates=deps.candidates if deps else 0,
                dependents_queued=deps.queued if deps else 0,
                by_source=dict(deps.by_source) if deps else {},
                closure_resolved=closure.resolved if closure else 0,
                names_added=delta[0],
                names_removed=delta[1],
                symbols_tombstoned=delta[2],
                errors=len(result.errors),
            )
            with log.open("a") as fh:
                fh.write(json.dumps(asdict(row)) + "\n")
            st.progress[arm + args.run_tag] = i + 1
            st.save(Path(args.state_dir) / "state.json")
            print(
                f"  [{arm} {i + 1}/{len(st.replay_shas)}] {sha[:12]} files={row.files} "
                f"wall={wall}ms deps={row.deps_ms}ms({row.dependents}/{row.dependents_candidates}) "
                f"closure={row.closure_ms}ms({row.closure_resolved}) "
                f"edges +{row.edges_new}/-{row.edges_tombstoned} "
                f"dep +{row.dep_edges_new}/-{row.dep_edges_tombstoned} "
                f"misses ~{row.misses_refreshed}/{row.dep_misses_refreshed}"
                + (f" errors={result.errors}" if result.errors else "")
            )
            base = sha
    finally:
        dep_mod.run_for_commit = real_run
        set_query_tag("")
    st.done[f"replay_{arm}{args.run_tag}"] = True


class _StubEmbedder:
    """The worker only reads .model_name from the embedder for stats and
    passes it on; ingest_file gets embedder=None via the capture below."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

_LIVE_EDGES = """
SELECT edge_kind, from_symbol_id, to_symbol_id, path, line,
       argMax(confidence, version) AS conf, argMax(tombstoned_at IS NULL, version) AS live
FROM loom.code_edges
WHERE org = {org:String} AND namespace = {ns:String} AND tier = 'treesitter'
  AND edge_kind IN {kinds:Array(String)}
GROUP BY edge_kind, from_symbol_id, to_symbol_id, path, line
HAVING live = 1
SETTINGS select_sequential_consistency = 1
"""

_LIVE_MISSES = """
SELECT callee_name, path, line, from_symbol_id,
       argMax(disposition, version) AS disposition, argMax(candidates, version) AS candidates,
       argMax(tombstoned_at IS NULL, version) AS live
FROM loom.code_call_sites_unresolved
WHERE org = {org:String} AND namespace = {ns:String}
GROUP BY callee_name, path, line, from_symbol_id
HAVING live = 1
SETTINGS select_sequential_consistency = 1
"""

_QUERY_LOG = """
SELECT
    splitByChar(' ', log_comment)[2] AS arm,
    splitByChar(' ', log_comment)[4] AS phase,
    count() AS queries,
    sum(read_rows) AS read_rows, sum(read_bytes) AS read_bytes,
    sum(written_rows) AS written_rows,
    sum(query_duration_ms) AS duration_ms,
    countIf(query_kind = 'Insert') AS inserts
FROM clusterAllReplicas('default', system.query_log)
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL {hours:UInt32} HOUR
  AND position(log_comment, 'd2r ') = 1
GROUP BY arm, phase ORDER BY arm, phase
"""

_QUERY_LOG_PER_COMMIT = """
SELECT
    splitByChar(' ', log_comment)[2] AS arm,
    toUInt32OrZero(splitByChar(' ', log_comment)[3]) AS i,
    splitByChar(' ', log_comment)[4] AS phase,
    sum(read_rows) AS read_rows, sum(read_bytes) AS read_bytes, sum(written_rows) AS written_rows,
    count() AS queries
FROM clusterAllReplicas('default', system.query_log)
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL {hours:UInt32} HOUR
  AND position(log_comment, 'd2r ') = 1
GROUP BY arm, i, phase ORDER BY arm, i, phase
"""


def _pct(x: float, y: float) -> float:
    return round(100.0 * x / y, 2) if y else 0.0


def _p(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return float(s[k])


async def compare_arm(
    ch: Any, org: str, ns: str, gt_ns: str, gt: dict[str, Any] | None, kinds: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(report for ns, gt cache). Edge key = (kind, from, to, path, line);
    site key = (callee, path, line, from)."""
    p = {"org": org, "kinds": list(kinds)}
    if gt is None:
        e = await ch.query_all(_LIVE_EDGES, {**p, "ns": gt_ns})
        m = await ch.query_all(_LIVE_MISSES, {"org": org, "ns": gt_ns})
        gt = {
            "edges": {
                (
                    r["edge_kind"],
                    r["from_symbol_id"],
                    r["to_symbol_id"],
                    r["path"],
                    int(r["line"]),
                ): float(r["conf"])
                for r in e
            },
            "misses": {
                (r["callee_name"], r["path"], int(r["line"]), r["from_symbol_id"]): (
                    str(r["disposition"]),
                    int(r["candidates"]),
                )
                for r in m
            },
        }
    e = await ch.query_all(_LIVE_EDGES, {**p, "ns": ns})
    m = await ch.query_all(_LIVE_MISSES, {"org": org, "ns": ns})
    edges = {
        (r["edge_kind"], r["from_symbol_id"], r["to_symbol_id"], r["path"], int(r["line"])): float(
            r["conf"]
        )
        for r in e
    }
    misses = {
        (r["callee_name"], r["path"], int(r["line"]), r["from_symbol_id"]): (
            str(r["disposition"]),
            int(r["candidates"]),
        )
        for r in m
    }
    ge, gm = gt["edges"], gt["misses"]
    missing = set(ge) - set(edges)
    stale = set(edges) - set(ge)
    both = set(edges) & set(ge)
    conf_diff = sum(1 for k in both if abs(edges[k] - ge[k]) > 0.01)
    by_grade = lambda keys, src: {  # noqa: E731
        "1.0": sum(1 for k in keys if src[k] > 0.95),
        "0.8-0.9": sum(1 for k in keys if 0.75 < src[k] <= 0.95),
        "0.6": sum(1 for k in keys if src[k] <= 0.75),
    }
    m_missing = set(gm) - set(misses)
    m_stale = set(misses) - set(gm)
    m_both = set(misses) & set(gm)
    verdict_diff = [k for k in m_both if misses[k] != gm[k]]
    disp_diff = sum(1 for k in verdict_diff if misses[k][0] != gm[k][0])
    rep = {
        "namespace": ns,
        "edges": {
            "gt": len(ge),
            "arm": len(edges),
            "shared": len(both),
            "missing": len(missing),
            "missing_by_grade": by_grade(missing, ge),
            "stale": len(stale),
            "stale_by_grade": by_grade(stale, edges),
            "conf_differs": conf_diff,
            "recall_pct": _pct(len(both), len(ge)),
            "precision_pct": _pct(len(both), len(edges)),
            "hole": len(missing) + len(stale),
            "missing_paths": len({k[3] for k in missing}),
            "stale_paths": len({k[3] for k in stale}),
        },
        "sites": {
            "gt": len(gm),
            "arm": len(misses),
            "shared": len(m_both),
            "missing": len(m_missing),
            "stale": len(m_stale),
            "verdict_differs": len(verdict_diff),
            "disposition_differs": disp_diff,
            "hole": len(m_missing) + len(m_stale) + len(verdict_diff),
        },
    }
    return rep, gt


async def phase_compare(args: argparse.Namespace, st: State, ch: Any) -> dict[str, Any]:
    from scripts.ops.code_edge_audit import audit_namespace

    gt_ns = f"{args.namespace}-gt"
    out: dict[str, Any] = {"arms": {}, "audit": {}, "query_log": {}, "per_commit": {}}
    gt: dict[str, Any] | None = None
    for arm in ARMS:
        if not st.done.get(f"replay_{arm}{args.run_tag}"):
            continue
        rep, gt = await compare_arm(ch, args.org, _arm_ns(args, arm), gt_ns, gt, EDGE_KINDS)
        out["arms"][arm] = rep
        print(
            f"  compare {arm}: edges gt={rep['edges']['gt']} missing={rep['edges']['missing']} "
            f"stale={rep['edges']['stale']} recall={rep['edges']['recall_pct']}%; "
            f"sites missing={rep['sites']['missing']} stale={rep['sites']['stale']} "
            f"verdict!={rep['sites']['verdict_differs']}"
        )
    for label in [*ARMS, "gt"]:
        ns = _arm_ns(args, label) if label != "gt" else f"{args.namespace}-gt"
        if label != "gt" and not st.done.get(f"replay_{label}{args.run_tag}"):
            continue
        a = await audit_namespace(ch, args.org, ns)
        out["audit"][label] = {
            "A_unresolved_with_def_sites": a["A_unresolved_with_definition"]["sites"],
            "A_one_def_sites": a["A_unresolved_with_definition"]["sites_one_def"],
            "A_ambiguous_shrank_to_one": a["A_ambiguous_with_definition"]["sites_one_def"],
            "B_dangling_le_06": a["B_dangling_target"]["dangling_le_06"],
            "B_dangling_any": a["B_dangling_target"]["dangling_any"],
            "live_calls": a["edges"]["live_calls"],
            "live_calls_le_06": a["edges"]["live_calls_le_06"],
            "unresolved_sites": a["misses"]["unresolved"]["sites"],
            "ambiguous_sites": a["misses"]["ambiguous"]["sites"],
        }
    try:
        # this run's arms only: the tag carries the run tag, so earlier runs drop out
        tagged = {f"{a}{args.run_tag}": a for a in ARMS}
        rows = await ch.query_all(_QUERY_LOG, {"hours": int(args.query_log_hours)})
        for r in rows:
            if str(r["arm"]) not in tagged:
                continue
            out["query_log"].setdefault(tagged[str(r["arm"])], {})[str(r["phase"])] = {
                k: int(r[k])
                for k in (
                    "queries",
                    "read_rows",
                    "read_bytes",
                    "written_rows",
                    "duration_ms",
                    "inserts",
                )
            }
        rows = await ch.query_all(_QUERY_LOG_PER_COMMIT, {"hours": int(args.query_log_hours)})
        for r in rows:
            if str(r["arm"]) not in tagged:
                continue
            out["per_commit"].setdefault(tagged[str(r["arm"])], {}).setdefault(str(r["i"]), {})[
                str(r["phase"])
            ] = {k: int(r[k]) for k in ("read_rows", "read_bytes", "written_rows", "queries")}
    except Exception as e:  # noqa: BLE001 — query_log is telemetry, not the result
        out["query_log_error"] = f"{type(e).__name__}: {e}"
    # per-commit wall/rows from the replay logs
    for arm in ARMS:
        log = Path(args.state_dir) / f"replay_{arm}{args.run_tag}.jsonl"
        if not log.exists():
            continue
        rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
        wall = [r["wall_ms"] for r in rows]
        touched = [r["touched_ms"] for r in rows]
        deps = [r["deps_ms"] + r["closure_ms"] for r in rows]
        inserted = [
            r["edges_new"]
            + r["edges_tombstoned"]
            + r["misses_new"]
            + r["misses_tombstoned"]
            + r["misses_refreshed"]
            + r["dep_edges_new"]
            + r["dep_edges_tombstoned"]
            + r["dep_misses_new"]
            + r["dep_misses_tombstoned"]
            + r["dep_misses_refreshed"]
            for r in rows
        ]
        ratio = [d / t for d, t in zip(deps, touched, strict=True) if t > 0]
        out["arms"].setdefault(arm, {})["timing"] = {
            "commits": len(rows),
            "files_touched": sum(r["files"] for r in rows),
            "wall_p50_ms": _p(wall, 0.5),
            "wall_p95_ms": _p(wall, 0.95),
            "wall_total_s": round(sum(wall) / 1000, 1),
            "touched_p50_ms": _p(touched, 0.5),
            "deps_p50_ms": _p(deps, 0.5),
            "deps_p95_ms": _p(deps, 0.95),
            "deps_total_s": round(sum(deps) / 1000, 1),
            "deps_share_of_wall_pct": _pct(sum(deps), sum(wall)),
            "deps_over_touched_median_pct": round(100 * statistics.median(ratio), 1)
            if ratio
            else 0.0,
            "rows_inserted_p50": _p(inserted, 0.5),
            "rows_inserted_p95": _p(inserted, 0.95),
            "rows_inserted_total": sum(inserted),
            "dependents_total": sum(r["dependents"] for r in rows),
            "dependents_candidates_total": sum(r["dependents_candidates"] for r in rows),
            "dependents_p95": _p([r["dependents_candidates"] for r in rows], 0.95),
            "dependents_max": max((r["dependents_candidates"] for r in rows), default=0),
            "commits_over_cap": sum(1 for r in rows if r["dependents_queued"] > 0),
            "commits_with_dependents": sum(1 for r in rows if r["dependents_candidates"] > 0),
            "closure_resolved_total": sum(r["closure_resolved"] for r in rows),
            "closure_p50_ms": _p([r["closure_ms"] for r in rows], 0.5),
            "dep_edges_new": sum(r["dep_edges_new"] for r in rows),
            "dep_edges_tombstoned": sum(r["dep_edges_tombstoned"] for r in rows),
            "dep_misses_new": sum(r["dep_misses_new"] for r in rows),
            "dep_misses_tombstoned": sum(r["dep_misses_tombstoned"] for r in rows),
            "dep_misses_refreshed": sum(r["dep_misses_refreshed"] for r in rows),
            "by_source": {
                k: sum(r["by_source"].get(k, 0) for r in rows)
                for k in (
                    "a_declined_sites",
                    "b_tombstoned_targets",
                    "d_now_ambiguous",
                )
            },
            "errors": sum(r["errors"] for r in rows),
        }
    return out


def render_report(st: State, cmp: dict[str, Any]) -> str:
    lines = [
        f"# dependents replay: K={st.base_sha[:12]} window={len(st.window_shas)} "
        f"replayed={len(st.replay_shas)} slice files={st.slice_paths}",
        "",
    ]
    lines.append(
        "| arm | commits | files | wall p50 ms | deps+closure p50 ms | deps share of wall | "
        "rows ins p50 / p95 | dependents (cand) | over cap | closure bound |"
        " edges missing | edges stale | recall | sites verdict!= | audit A one-def | audit B ≤0.6 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for arm, rep in cmp["arms"].items():
        t = rep.get("timing", {})
        e = rep.get("edges", {})
        s = rep.get("sites", {})
        au = cmp["audit"].get(arm, {})
        lines.append(
            f"| {arm} | {t.get('commits', 0)} | {t.get('files_touched', 0)} | "
            f"{t.get('wall_p50_ms', 0):.0f} | "
            f"{t.get('deps_p50_ms', 0):.0f} | {t.get('deps_share_of_wall_pct', 0)}% | "
            f"{t.get('rows_inserted_p50', 0):.0f} / {t.get('rows_inserted_p95', 0):.0f} | "
            f"{t.get('dependents_total', 0)} ({t.get('dependents_candidates_total', 0)}) | "
            f"{t.get('commits_over_cap', 0)} | "
            f"{t.get('closure_resolved_total', 0)} | {e.get('missing', 0)} | {e.get('stale', 0)} | "
            f"{e.get('recall_pct', 0)}% | {s.get('verdict_differs', 0)} | "
            f"{au.get('A_one_def_sites', '')} | {au.get('B_dangling_le_06', '')} |"
        )
    gt = cmp["audit"].get("gt", {})
    lines.append(
        f"| gt | | | | | | | | | | 0 | 0 | 100% | 0 | {gt.get('A_one_def_sites', '')} | "
        f"{gt.get('B_dangling_le_06', '')} |"
    )
    lines.append("")
    if cmp.get("query_log"):
        lines.append(
            "| arm | phase | queries | inserts | read rows | read MiB | written rows | CH ms |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for arm, phases in cmp["query_log"].items():
            for ph, r in phases.items():
                lines.append(
                    f"| {arm} | {ph} | {r['queries']} | {r['inserts']} | {r['read_rows']} | "
                    f"{r['read_bytes'] / 2**20:.1f} | {r['written_rows']} | {r['duration_ms']} |"
                )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "phase", nargs="+", help="all | plan | base | clone | replay <arm> | gt | compare | report"
    )
    ap.add_argument(
        "--env-file",
        default="",
        help="the .env whose ClickHouse this bench writes to (required; no host default)",
    )
    ap.add_argument("--org", default="dev")
    ap.add_argument("--namespace", default="bench-code-d2-replay", help="bench namespace stem")
    ap.add_argument("--git-dir", default="var/code_repos/clickhouse.git")
    ap.add_argument("--branch", default="master")
    ap.add_argument("--base-rev", default="master~400")
    ap.add_argument("--window", type=int, default=400)
    ap.add_argument("--max-commits", type=int, default=60)
    ap.add_argument("--prefix", action="append", default=[], help="slice prefixes (repeatable)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--query-log-hours", type=int, default=12)
    ap.add_argument("--no-sequential-reads", action="store_true")
    ap.add_argument(
        "--run-tag",
        default="",
        help="suffix for the arm namespaces, logs and progress keys, so arms can be "
        "re-run against the same base and ground truth after a code change",
    )
    return ap


async def run(args: argparse.Namespace) -> int:
    from server.code.app.index import gitrepo
    from server.code.app.index.ingest import ingest_file
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client, sequential_reads
    from server.settings import settings

    args.model = settings.embedding_model
    ch = get_client()
    await ch.connect()
    repo = CodeRepo()
    state_path = Path(args.state_dir) / "state.json"
    Path(args.state_dir).mkdir(parents=True, exist_ok=True)
    st = State.load(state_path)
    phases = list(args.phase)
    if phases == ["all"]:
        phases = [
            "plan",
            "base",
            "clone",
            "replay",
            "a",
            "replay",
            "b",
            "replay",
            "c",
            "gt",
            "compare",
            "report",
        ]
    scope = sequential_reads() if not args.no_sequential_reads else _noop()
    with scope:
        i = 0
        while i < len(phases):
            ph = phases[i]
            i += 1
            t0 = time.monotonic()
            if ph == "plan":
                if not st.done.get("plan"):
                    await phase_plan(args, st, gitrepo)
            elif ph == "base":
                if not st.done.get("base"):
                    c = await full_index(
                        repo,
                        gitrepo,
                        ingest_file,
                        org=args.org,
                        ns=f"{args.namespace}-base",
                        git_dir=args.git_dir,
                        sha=st.base_sha,
                        prefixes=args.prefixes,
                        model=args.model,
                        concurrency=args.concurrency,
                        label="base",
                    )
                    (Path(args.state_dir) / "base_index.json").write_text(json.dumps(c, indent=1))
                    st.done["base"] = True
            elif ph == "clone":
                if not st.done.get("clone" + args.run_tag):
                    await phase_clone(args, st, ch)
            elif ph == "replay":
                arm = phases[i]
                i += 1
                if not st.done.get(f"replay_{arm}{args.run_tag}"):
                    await phase_replay(args, st, arm, repo, gitrepo)
            elif ph == "gt":
                if not st.done.get("gt"):
                    c = await full_index(
                        repo,
                        gitrepo,
                        ingest_file,
                        org=args.org,
                        ns=f"{args.namespace}-gt",
                        git_dir=args.git_dir,
                        sha=st.replay_shas[-1],
                        prefixes=args.prefixes,
                        model=args.model,
                        concurrency=args.concurrency,
                        label="gt",
                    )
                    (Path(args.state_dir) / "gt_index.json").write_text(json.dumps(c, indent=1))
                    st.done["gt"] = True
            elif ph == "compare":
                cmp = await phase_compare(args, st, ch)
                (Path(args.state_dir) / "compare.json").write_text(
                    json.dumps(cmp, indent=1, default=str)
                )
            elif ph == "report":
                cmp = json.loads((Path(args.state_dir) / "compare.json").read_text())
                md = render_report(st, cmp)
                (Path(args.state_dir) / "report.md").write_text(md)
                print(md)
            else:
                print(f"unknown phase {ph}", file=sys.stderr)
                return 2
            st.timings[ph if ph != "replay" else f"replay_{phases[i - 1]}"] = round(
                time.monotonic() - t0, 1
            )
            st.save(state_path)
    await ch.disconnect()
    return 0


class _noop:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: Any) -> None:
        return None


def main() -> int:
    args = _parser().parse_args()
    args.prefixes = tuple(args.prefix) or ("src/Storages/MergeTree/", "src/Interpreters/")
    if not args.env_file:
        raise SystemExit("--env-file is required (the ClickHouse the replay namespaces live in)")
    load_env(args.env_file)
    import server.operations  # noqa: F401 — circular-import guard (handlers import it first)

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
