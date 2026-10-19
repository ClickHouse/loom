"""code.backport_gap: a merged fix names the release branches that still carry the bug and
the exact PR to cherry-pick there.

Addressed at the CODE namespace of the trunk (`code-clickhouse`); the
branches are the release-branch namespaces configured for the same
repository (`LOOM_CODE_REPOS_JSON`, `git_branch` a release such as `26.4`),
newest first, or the caller's `branches` (namespaces or versions). Three
legs per branch, gathered under one deadline:

1. the fix — the PR row from the tracker (`minor_version`, files, the
   enclosing declarations of every hunk, extracted upstream) beside loom's
   ledger (merge commit, files); from an issue, its `fixes` edges name the
   PR(s), and the issue's `fix_versions` are a record leg of their own;
2. the record — QA's backport PRs (`is_backport_pr`, `upstream_pr_number`
   or the "Backport #N to X:" title, `base_branch`, merged or open) and,
   per branch namespace, the ledger's merged rows titled the same;
3. presence — `code.symbol`'s presence verdicts for the touched
   functions over every branch namespace at once (≤ `max_symbols`
   fan-outs), or the fix's source files on each branch's file table when
   the hunks name nothing.

`server/tracker/domain/backport.py` turns the three into one verdict per
branch — `gap`, `pending`, `covered`, `not_affected`, `unknown` — with the
evidence lines and, on a gap, `cherry_pick: {pr_number, merge_sha}`.

`exact: true` adds a fourth leg on request: the fix's diff sections from the tracker record,
ONE chunk read per branch for the touched paths, and
`server/tracker/domain/patch_presence.py`'s line ratios and token bag — a `gap` or
`covered` decided by the patch's own lines is `exact` (basis `hunk`), a refactored backport
confirmed by the bag is `covered` with basis `token_bag`, and `presence.patch` discloses the
counts.

`history: true` (implies `exact`) adds a fifth leg for the branches the lines left
`undecided`: the fix's removed lines on the
branch's own history in the mirror (`server/tracker/app/patch_history.py`:
`git grep` at the tip, `git log -S` over the first-parent history, at most 3
lines, 2 s per branch) — the pre-image at the tip is `gap` exact (basis
`history`), never carried or removed since is `not_affected` with the
reason. The nightly `code_backport_gaps` worker runs the composed read
through `run_backport_gap` for every labelled fix and writes the rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.code.api.models_history import CodeBackportGapOutput
from server.code.app.common import (
    index_as_of,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.spans import code_span, set_code_span_attributes
from server.code.domain.index.blame_range import sha_text
from server.code.store.index import CodeRepo
from server.code.store.index.pr_history import PrFacts
from server.core.errors import ValidationError
from server.settings import settings
from server.tracker.app import links as lk
from server.tracker.app import patch_history as ph
from server.tracker.domain import backport as bp
from server.tracker.domain import patch_presence as pp

logger = logging.getLogger(__name__)

MAX_BRANCHES = 8
_MAX_FIXES = 8
_HISTORY_GIT_PROCS = 4


async def _settle(tasks: Any, wall_ms: int) -> set[asyncio.Future[Any]]:
    """Wait for per-namespace `tasks` up to the wall, cancel (and reap) the ones
    still running, and return them. A cancel of the caller cancels every task too:
    `asyncio.wait` alone would leave them running (git subprocesses, CH reads)."""
    tasks = list(tasks)
    try:
        _, pending = await asyncio.wait(tasks, timeout=max(0.05, wall_ms / 1000.0))
    except BaseException:
        for t in tasks:
            t.cancel()
        raise
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return pending


# ---- branches ----------------------------------------------------------------------


def release_namespaces(org: str, code_ns: str) -> list[tuple[str, str]]:
    """[(namespace, version)] of the release-branch namespaces configured
    for the repository `code_ns` indexes, newest first; the trunk itself
    never. Operator configuration, not a guess from names."""
    from server.code.domain.index.repos import configured_repos, repo_for_namespace

    me = repo_for_namespace(org, code_ns)
    if me is None:
        return []
    out: list[tuple[str, str]] = []
    for r in configured_repos():
        if r.org != org or r.namespace == code_ns or r.repo != me.repo:
            continue
        v = bp.version_key(r.git_branch)
        if v is None:
            continue
        out.append((r.namespace, f"{v[0]}.{v[1]}"))
    out.sort(key=lambda x: bp.version_key(x[1]) or (0, 0), reverse=True)
    return out


def resolve_branches(org: str, code_ns: str, wanted: list[str]) -> list[tuple[str, str]]:
    """The caller's `branches` — a namespace (`code-clickhouse-26-4`) or a
    release (`26.4`) each — against the configuration; an entry the
    configuration does not know keeps its name and, when it parses as a
    release, that version. Empty → every configured release branch."""
    configured = release_namespaces(org, code_ns)
    if not wanted:
        return configured[:MAX_BRANCHES]
    by_ns = dict(configured)
    by_version = {v: ns for ns, v in configured}
    out: list[tuple[str, str]] = []
    for raw in wanted:
        s = str(raw).strip()
        if not s:
            continue
        v = bp.version_key(s)
        if s in by_ns:
            pair = (s, by_ns[s])
        elif v is not None and f"{v[0]}.{v[1]}" in by_version:
            pair = (by_version[f"{v[0]}.{v[1]}"], f"{v[0]}.{v[1]}")
        else:
            pair = (s, f"{v[0]}.{v[1]}" if v is not None else "")
        if pair not in out:
            out.append(pair)
    return out[:MAX_BRANCHES]


# ---- the fix -----------------------------------------------------------------------


async def _fixes_of_issue(
    source: Any, org: str, tracker_ns: str, number: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(issue row, the PRs whose `fixes` edge names the issue — with the
    flags that tell the trunk fix from its backports and cherry-picks)."""
    rows, prs = await asyncio.gather(
        source.items_by_number(org, tracker_ns, [number]),
        source.fix_prs_of_issue(org, tracker_ns, number),
    )
    issue = dict(rows.get(number) or {})
    cands: list[dict[str, Any]] = []
    seen: set[int] = set()
    for r in prs:
        n = int(r.get("pr_number") or 0)
        if n <= 0 or n in seen:
            continue
        seen.add(n)
        cands.append(
            {
                "pr_number": n,
                "title": str(r.get("title") or "")[:160],
                "is_merged": int(r.get("is_merged") or 0),
                "is_backport_pr": int(r.get("is_backport_pr") or 0),
                "is_cherrypick_pr": int(r.get("is_cherrypick_pr") or 0),
                "base_branch": str(r.get("base_branch") or ""),
                "minor_version": str(r.get("minor_version") or ""),
            }
        )
    return issue, cands[:_MAX_FIXES]


def _pick_fix(cands: list[dict[str, Any]]) -> int:
    """Among the PRs an issue's edges name, the merged non-derivative PR on
    the trunk, newest first; 0 when none merged there."""
    trunk = [
        c
        for c in cands
        if c["is_merged"]
        and not c["is_backport_pr"]
        and not c["is_cherrypick_pr"]
        and c["base_branch"] in ("", "master", "main")
        and not bp.parse_backport_title(c["title"])[0]
        and not c["title"].startswith("Cherry pick")
    ]
    trunk.sort(key=lambda c: -c["pr_number"])
    return trunk[0]["pr_number"] if trunk else 0


def _fix_block(
    change: dict[str, Any] | None, ledger: PrFacts | None, number: int
) -> dict[str, Any]:
    fix: dict[str, Any] = {"pr_number": number}
    if change:
        for k in (
            "title",
            "url",
            "author",
            "base_branch",
            "minor_version",
            "is_merged",
            "is_backport_pr",
            "is_reverted",
            "changelog_category",
            "src_components",
            "files",
        ):
            fix[k] = change.get(k)
        merged = change.get("merged_at")
        fix["merged_at"] = merged.isoformat() if merged is not None else ""
        fix["hunk_contexts"] = list(change.get("hunk_contexts") or [])
    labels = [str(x) for x in ((ledger.get("labels") if ledger else None) or [])]
    if ledger:
        fix.setdefault("title", str(ledger.get("title") or ""))
        if not fix.get("files"):
            fix["files"] = [str(p) for p in (ledger.get("files") or [])]
        fix["merge_sha"] = sha_text(ledger.get("merge_commit_sha"))
        if not fix.get("merged_at") and ledger.get("merged_at"):
            fix["merged_at"] = ledger["merged_at"].isoformat()
        fix["ledger"] = True
    else:
        fix.setdefault("merge_sha", "")
        fix["ledger"] = False
    fix["labels"] = labels
    # the maintainers' own signal that a backport is owed: the QA column or
    # the ledger's pr-must-backport family of labels
    fix["backport_required"] = bool((change or {}).get("backport_required")) or any(
        lab.startswith("pr-must-backport") for lab in labels
    )
    return fix


# ---- presence ------------------------------------------------------------------------


async def _symbol_presence(
    org: str,
    repo: CodeRepo,
    namespaces: list[str],
    symbols: list[str],
    wall_ms: int,
) -> dict[str, dict[str, tuple[str, str]]]:
    """symbol → namespace → (verdict, reason) with `code.symbol`'s presence verdicts
    (present / absent under a watermark / unknown), from ONE name-keyed read per namespace
    for every symbol beside that namespace's `index_as_of`, all under one deadline. Every
    namespace is authorized first, fail closed (the same read gate `code.symbol` presence
    uses). A namespace still running at the deadline is `unknown(deadline)` for every
    symbol."""
    from server.code.app import presence as code_presence
    from server.code.app import search_fanout as fanout

    if not symbols or not namespaces:
        return {}
    await fanout.authorize(org, namespaces, op="code.backport_gap")
    code_presence.check_cap("code.backport_gap", namespaces)

    async def one(ns: str) -> tuple[dict[str, list[str]], dict[str, Any]]:
        hits, as_of = await asyncio.gather(
            repo.symbol_names_present(org, ns, symbols),
            index_as_of(repo, org, ns),
        )
        return hits, dict(as_of or {})

    tasks = {ns: asyncio.ensure_future(one(ns)) for ns in namespaces}
    pending = await _settle(tasks.values(), wall_ms)
    result: dict[str, dict[str, tuple[str, str]]] = {s: {} for s in symbols}
    for ns, t in tasks.items():
        if t in pending or t.cancelled():
            for s in symbols:
                result[s][ns] = ("unknown", "deadline")
            continue
        err = t.exception()
        if err is not None:
            logger.debug("backport_gap: presence read failed on %s: %s", ns, err)
            for s in symbols:
                result[s][ns] = ("unknown", f"error:{type(err).__name__}")
            continue
        hits, as_of = t.result()
        indexed = bool(as_of.get("commit"))
        for s in symbols:
            if s in hits:
                result[s][ns] = ("present", "")
            elif indexed:
                result[s][ns] = ("absent", "")
            else:
                result[s][ns] = ("unknown", "no_index")
    return result


async def _file_presence(
    repo: CodeRepo, org: str, ns: str, files: list[str]
) -> tuple[list[str], list[str], bool]:
    """(present, absent, indexed) for the fix's source files on one branch."""
    if not files:
        return [], [], False
    try:
        present, as_of = await asyncio.gather(
            repo.live_paths_present(org, ns, files),
            index_as_of(repo, org, ns),
        )
    except Exception as e:  # noqa: BLE001 — one branch's read, disclosed as unknown
        logger.debug("backport_gap: file presence failed on %s: %s", ns, e)
        return [], [], False
    return (
        [f for f in files if f in present],
        [f for f in files if f not in present],
        bool(dict(as_of or {}).get("commit")),
    )


async def _ledger_backports(repo: CodeRepo, org: str, ns: str, number: int) -> list[dict[str, Any]]:
    try:
        rows = await repo.backport_prs_titled(org, ns, number)
    except Exception as e:  # noqa: BLE001 — the ledger leg is a second opinion
        logger.debug("backport_gap: ledger read failed on %s: %s", ns, e)
        return []
    return [
        {
            "pr_number": int(r.get("pr_number") or 0),
            "title": str(r.get("pr_title") or ""),
            "state": "merged",
            "merge_sha": sha_text(r.get("merge_sha")),
            "merged_at": r["merged"].isoformat() if r.get("merged") else "",
            "source": "ledger",
            "url": "",
        }
        for r in rows
    ]


def _wire_backport(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if hasattr(out.get("merged_at"), "isoformat"):
        out["merged_at"] = out["merged_at"].isoformat()
    return out


# ---- hunk-level presence ---------------------------------------------------------------


async def _no_sections() -> list[str]:
    return []


async def _patch_stage(
    org: str,
    repo: CodeRepo,
    namespaces: list[str],
    sections: list[str],
    *,
    authorized: bool,
    wall_ms: int,
) -> tuple[list[pp.HunkGroup], dict[str, pp.PatchPresence]]:
    """(the fix's hunk groups, namespace → patch presence): ONE chunk read per
    branch for the fix's source paths — the enclosing functions the hunks
    name and every chunk carrying one of the patch's lines — then the line
    ratios and the token bag, all branches at once under one deadline. A
    branch still running at the deadline is `symbol_only(deadline)`; a read
    that raised is `symbol_only(error:<type>)` — never a verdict."""
    from server.code.app import search_fanout as fanout

    groups = pp.parse_sections(sections)
    if not groups or not namespaces:
        return groups, {}
    if not authorized:
        await fanout.authorize(org, namespaces, op="code.backport_gap")
    paths, symbols, needles = pp.paths_of(groups), pp.symbols_of(groups), pp.needles(groups)

    async def one(ns: str) -> pp.PatchPresence:
        chunks = await repo.chunks_for_patch(org, ns, paths, symbols, needles)
        return pp.decide(groups, chunks)

    tasks = {ns: asyncio.ensure_future(one(ns)) for ns in namespaces}
    pending = await _settle(tasks.values(), wall_ms)
    out: dict[str, pp.PatchPresence] = {}
    for ns, t in tasks.items():
        if t in pending or t.cancelled():
            out[ns] = pp.PatchPresence(reason="deadline")
        elif t.exception() is not None:
            logger.debug("backport_gap: patch read failed on %s: %s", ns, t.exception())
            out[ns] = pp.PatchPresence(reason=f"error:{type(t.exception()).__name__}")
        else:
            out[ns] = t.result()
    return groups, out


def _patch_summary(groups: list[pp.HunkGroup], sections: list[str]) -> dict[str, Any]:
    """What the exact leg tested with, on the fix block."""
    return {
        "files": len(pp.paths_of(groups)),
        "sections": len(sections),
        "hunks": sum(g.hunks for g in groups),
        "added": sum(len(g.added) for g in groups),
        "removed": sum(len(g.removed) for g in groups),
        "symbols": pp.symbols_of(groups),
    }


@dataclass
class BackportGapRequest:
    """The composed read's arguments, after the gate: the handler builds one
    from the wire input; the nightly `code_backport_gaps` worker builds one per labelled fix
    with `exact` and `history` set and `trace` off (a pass writes no code_search_trace
    row per fix)."""

    org: str
    namespace: str
    pr_number: int = 0
    issue_number: int = 0
    branches: list[str] = field(default_factory=list)
    max_symbols: int = 8
    exact: bool = False
    history: bool = False
    consumer: str = ""
    agent: str = "-"
    user_id: str = "-"
    trace: bool = True


@dataclass
class _FixStage:
    """What stage one gathered: the fix's QA row, its ledger row, the record's
    backport PRs, the diff sections (under `exact`), the branch ledgers'
    backports (one list per branch), the issue block and its candidates."""

    number: int = 0
    qa_ok: bool = True
    causes: list[str] = field(default_factory=list)
    issue: dict[str, Any] = field(default_factory=dict)
    fixes: list[dict[str, Any]] = field(default_factory=list)
    fix_candidate: dict[str, Any] | None = None
    change: dict[str, Any] | None = None
    ledger: PrFacts | None = None
    tracker_backports: list[dict[str, Any]] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    ledger_rows: list[list[dict[str, Any]]] = field(default_factory=list)


async def _issue_stage(
    st: _FixStage, source: Any, org: str, tracker_ns: str, issue_number: int
) -> None:
    """From an issue: its row, the PRs its `fixes` edges name, the trunk fix
    among them — or the link pass's candidate."""
    from server.tracker.domain.errors import TrackerUnavailable

    try:
        row, st.fixes = await _fixes_of_issue(source, org, tracker_ns, issue_number)
        st.issue = {
            "number": issue_number,
            "title": str(row.get("title") or "")[:160],
            "state": str(row.get("state") or ""),
            "affected_versions": list(row.get("affected_versions") or []),
            "fix_versions": list(row.get("fix_versions") or []),
            "fixed_by": [c["pr_number"] for c in st.fixes],
        }
        if not st.number:
            st.number = _pick_fix(st.fixes)
        if not st.number:
            st.fix_candidate = await lk.fix_candidate(org, tracker_ns, issue_number)
            st.number = int(st.fix_candidate["pr_number"]) if st.fix_candidate else 0
    except TrackerUnavailable as e:
        logger.info("backport_gap: issue read unavailable (%s)", e)
        st.qa_ok = False


async def _fix_stage(
    req: BackportGapRequest,
    repo: CodeRepo,
    source: Any,
    tracker_ns: str,
    branch_ns: list[str],
    *,
    exact: bool,
) -> _FixStage:
    """Stage one, gathered: the QA change row, the record's backports and (under
    `exact`) the diff sections beside the trunk ledger's row and every branch
    ledger's titled backports. A QA miss keeps the ledger legs."""
    from server.tracker.domain.errors import TrackerUnavailable

    st = _FixStage(number=int(req.pr_number or 0), qa_ok=source is not None)
    if source is not None and req.issue_number:
        await _issue_stage(st, source, req.org, tracker_ns, int(req.issue_number))
    elif req.issue_number and source is None:
        st.causes.append("tracker_unbound")
    number = st.number
    if not number:
        st.ledger_rows = [[] for _ in branch_ns]
        return st

    async def _tracker_change() -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
        if source is None or not st.qa_ok:
            return None, [], []
        change_row, rows, sections = await asyncio.gather(
            source.pr_change(req.org, tracker_ns, number),
            source.backport_prs_of(req.org, tracker_ns, number),
            source.pr_diff_sections(req.org, tracker_ns, number) if exact else _no_sections(),
        )
        return change_row, list(rows), list(sections)

    async def _ledger_fix() -> PrFacts | None:
        try:
            return await repo.pr_facts(req.org, req.namespace, number)
        except Exception as e:  # noqa: BLE001 — the ledger miss is disclosed on the fix block
            logger.debug("backport_gap: ledger fix read failed: %s", e)
            return None

    ledgers = asyncio.gather(*(_ledger_backports(repo, req.org, b, number) for b in branch_ns))
    try:
        (st.change, st.tracker_backports, st.sections), st.ledger, rows = await asyncio.gather(
            _tracker_change(),
            _ledger_fix(),
            ledgers,
        )
    except TrackerUnavailable as e:
        logger.info("backport_gap: qa unavailable (%s)", e)
        st.qa_ok = False
        st.ledger, rows = await asyncio.gather(
            _ledger_fix(),
            asyncio.gather(*(_ledger_backports(repo, req.org, b, number) for b in branch_ns)),
        )
    st.ledger_rows = list(rows)
    return st


# ---- the branch's history --------------------------------------------------------------


def branch_refs(org: str, code_ns: str) -> tuple[str, dict[str, str]]:
    """(the repository's bare mirror, branch namespace → its git ref) from the
    configuration: what the history leg reads the release branches with."""
    from server.code.domain.index.repos import configured_repos, repo_for_namespace

    me = repo_for_namespace(org, code_ns)
    if me is None:
        return "", {}
    return me.git_dir, {
        r.namespace: r.git_branch
        for r in configured_repos()
        if r.org == org and r.repo == me.repo and r.namespace != code_ns
    }


async def _history_stage(
    org: str,
    code_ns: str,
    namespaces: list[str],
    groups: list[pp.HunkGroup],
    *,
    wall_ms: int,
    pr_number: int = 0,
    merge_sha: str = "",
) -> dict[str, ph.HistoryPresence]:
    """namespace → tip-and-history presence for the branches the chunk lines
    left undecided: one grep per path at the branch's tip, then ≤ PROBE_LINES
    `-S` reads for the pre-image the tip lacks, at most _HISTORY_GIT_PROCS
    git processes at once, all under one wall. A branch still running at the
    wall is `deadline` — never a verdict."""
    from server.code.app.index.mirrors import history_dir
    from server.code.domain.index.repos import repo_for_namespace

    git_dir, refs = branch_refs(org, code_ns)
    if not namespaces or not groups:
        return {}
    cfg = repo_for_namespace(org, code_ns)
    if cfg is not None and (gap := (await history_dir(cfg))[1]):
        return {ns: ph.HistoryPresence(reason=gap) for ns in namespaces}
    gate = asyncio.Semaphore(_HISTORY_GIT_PROCS)
    tasks = {
        ns: asyncio.ensure_future(
            ph.branch_history(
                git_dir,
                refs.get(ns, ""),
                groups,
                gate=gate,
                pr_number=pr_number,
                merge_sha=merge_sha,
            )
        )
        for ns in namespaces
    }
    pending = await _settle(tasks.values(), wall_ms)
    out: dict[str, ph.HistoryPresence] = {}
    for ns, t in tasks.items():
        if t in pending or t.cancelled():
            out[ns] = ph.HistoryPresence(reason="deadline")
        elif t.exception() is not None:
            logger.debug("backport_gap: history read failed on %s: %s", ns, t.exception())
            out[ns] = ph.HistoryPresence(reason=f"error:{type(t.exception()).__name__}")
        else:
            out[ns] = t.result()
    return out


# ---- the handler -------------------------------------------------------------------


async def handle_code_backport_gap(input: Any) -> Any:
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    if not (int(input.pr_number or 0) or int(input.issue_number or 0)):
        raise ValidationError("pr_number or issue_number is required")
    return await run_backport_gap(
        BackportGapRequest(
            org=org,
            namespace=ns,
            pr_number=int(input.pr_number or 0),
            issue_number=int(input.issue_number or 0),
            branches=list(input.branches),
            max_symbols=int(input.max_symbols),
            exact=bool(getattr(input, "exact", False)),
            history=bool(getattr(input, "history", False)),
            consumer=input.consumer,
            agent=input.agent,
            user_id=input.user_id,
        )
    )


async def run_backport_gap(req: BackportGapRequest) -> Any:  # noqa: PLR0915 — one composed read, staged
    """The composed read after the gate: stage one (the fix), presence on
    every branch, the lines (`exact`), the history (`history`), then one
    verdict per branch. Shared by the wire op and the nightly worker."""
    from server.tracker.app.binding import authorized_tracker_namespace
    from server.tracker.app.source import tracker_source

    started = time.monotonic()
    org, ns = req.org, req.namespace
    repo = CodeRepo()
    history = bool(req.history)
    exact = bool(req.exact) or history
    branches, skipped_branches = await _implicit_branches_readable(
        org, resolve_branches(org, ns, list(req.branches)), explicit=bool(req.branches)
    )
    branch_ns = [b for b, _ in branches]
    # The caller always names a PR or an issue, whose record the tracker holds.
    tracker_ns = await authorized_tracker_namespace(org, ns)
    source = tracker_source(org, tracker_ns) if tracker_ns else None

    stage_ms: dict[str, int] = {}
    with code_span("backport.fix", seeds=1):
        st = await _fix_stage(req, repo, source, tracker_ns, branch_ns, exact=exact)
    stage_ms["fix"] = int((time.monotonic() - started) * 1000)
    number, qa_ok, causes = st.number, st.qa_ok, list(st.causes)
    fix_found = bool(number and (st.change or st.ledger))
    fix = _fix_block(st.change, st.ledger, number) if number else {}
    if st.fix_candidate is not None:
        fix.update(source="candidate", epistemic=lk.EPISTEMIC, score=st.fix_candidate["score"])
        st.issue["fixed_by_candidate"] = [int(st.fix_candidate["pr_number"])]
    if number and not fix_found:
        causes.append("fix_not_found")
    if not tracker_ns:
        causes.append("tracker_unbound")
    elif not qa_ok:
        causes.append("tracker_unavailable")
    fix_minor = str((st.change or {}).get("minor_version") or "")
    symbols = bp.hunk_symbols(
        list((st.change or {}).get("hunk_contexts") or []), limit=int(req.max_symbols)
    )
    files = bp.source_files(list(fix.get("files") or []))
    cherry = {"pr_number": number, "merge_sha": str(fix.get("merge_sha") or "")} if number else {}

    # stage 2: presence on every branch at once
    sym_rows: dict[str, dict[str, tuple[str, str]]] = {}
    file_rows: list[tuple[list[str], list[str], bool]] = [([], [], False) for _ in branch_ns]
    if fix_found and branch_ns:
        with code_span("backport.presence", seeds=len(symbols) or len(files)):
            if symbols:
                sym_rows = await _symbol_presence(
                    org, repo, branch_ns, symbols, settings.code_backport_presence_budget_ms
                )
            elif files:
                from server.code.app import search_fanout as fanout

                await fanout.authorize(org, branch_ns, op="code.backport_gap")
                file_rows = list(
                    await asyncio.gather(*(_file_presence(repo, org, b, files) for b in branch_ns))
                )
    stage_ms["presence"] = int((time.monotonic() - started) * 1000) - stage_ms["fix"]
    # stage 2b (on request): the fix's lines on every branch at once
    patch_rows: dict[str, pp.PatchPresence] = {}
    groups: list[pp.HunkGroup] = []
    if exact and fix_found and branch_ns:
        with code_span("backport.patch", seeds=len(st.sections)):
            groups, patch_rows = await _patch_stage(
                org,
                repo,
                branch_ns,
                st.sections,
                authorized=bool(symbols or files),
                wall_ms=settings.code_backport_patch_budget_ms,
            )
        fix["patch"] = _patch_summary(groups, st.sections)
        stage_ms["patch"] = int((time.monotonic() - started) * 1000) - sum(stage_ms.values())
    # stage 2c (on request): the branch's history for the undecided rows
    history_rows: dict[str, ph.HistoryPresence] = {}
    if history and fix_found and branch_ns:
        undecided = [
            b for b in branch_ns if getattr(patch_rows.get(b), "reason", "") == "undecided"
        ]
        with code_span("backport.history", seeds=len(undecided)):
            history_rows = await _history_stage(
                org,
                ns,
                undecided,
                groups,
                wall_ms=settings.code_backport_history_budget_ms,
                pr_number=number,
                merge_sha=str(cherry.get("merge_sha") or ""),
            )
        stage_ms["history"] = int((time.monotonic() - started) * 1000) - sum(stage_ms.values())
    # stage 3: one verdict per branch
    verdicts: list[bp.BranchVerdict] = []
    for i, (b_ns, version) in enumerate(branches):
        rows = [r for r in st.tracker_backports if r.get("base_branch") == version]
        rows += list(st.ledger_rows[i] if number else [])
        present, absent, indexed = file_rows[i]
        inp = bp.BranchInputs(
            namespace=b_ns,
            branch=version,
            backports=rows,
            symbols={s: sym_rows[s].get(b_ns, ("unknown", "no_answer"))[0] for s in sym_rows},
            symbol_reasons={
                s: sym_rows[s].get(b_ns, ("unknown", "no_answer"))[1] for s in sym_rows
            },
            files_present=present,
            files_absent=absent,
            files_checked=bool(files) and not symbols,
            indexed=indexed,
        )
        if not fix_found:
            verdicts.append(
                bp.BranchVerdict(
                    namespace=b_ns,
                    branch=version,
                    verdict=bp.UNKNOWN,
                    epistemic="lower_bound",
                    basis="",
                    reason="no_fix",
                    evidence=["no merged fix to test the branch against"],
                )
            )
            continue
        verdicts.append(
            bp.verdict_for(
                inp,
                fix_minor=fix_minor,
                fix_versions=list(st.issue.get("fix_versions") or []),
                qa_ok=qa_ok,
                cherry_pick=cherry,
                fix_reverted=bool(fix.get("is_reverted")),
                backport_required=bool(fix.get("backport_required")),
            )
        )
        if b_ns in patch_rows:
            pp.refine(verdicts[-1], patch_rows[b_ns], cherry)
        if b_ns in history_rows:
            ph.refine(verdicts[-1], history_rows[b_ns], cherry)
    ranked = bp.rank(verdicts)
    for v in ranked:
        v.backport = _wire_backport(v.backport) if v.backport else {}
        if not tracker_ns and v.reason == "tracker_unavailable":
            v.reason = "tracker_unbound"  # nothing to read, not a read that failed
    verdict, more = bp.overall(ranked, fix_found=fix_found)
    for c in more:
        if c not in causes:
            causes.append(c)
    summary = bp.summarize(ranked)
    trace_run_id = new_trace_run_id()
    if req.trace:
        schedule_code_trace(
            org=org,
            ns=ns,
            op="code.backport_gap",
            query=f"#{number}" if number else f"issue #{req.issue_number}",
            route="backport_gap",
            returned_ids=[v.namespace for v in ranked if v.verdict == bp.GAP],
            returned_paths=files[:20],
            returned_scores=[],
            top_k=len(branches),
            tokens_returned=0,
            abstain=verdict == bp.UNKNOWN,
            retrieval_risk="low",
            embedding_model="",
            started_monotonic=started,
            trace_run_id=trace_run_id,
            consumer=req.consumer,
            agent=req.agent,
            user_id=req.user_id,
            verdict=verdict,
        )
    set_code_span_attributes(
        route="backport_gap",
        results=len(summary[bp.GAP]),
        fanout=len(branches),
        consumer=req.consumer,
    )
    return CodeBackportGapOutput(
        skipped_branches=skipped_branches,
        verdict=verdict,
        causes=causes,
        fix=fix,
        issue=st.issue,
        fixes=st.fixes,
        symbols=symbols,
        files=files,
        fix_candidate=st.fix_candidate,
        branches=[v.as_dict() for v in ranked],
        summary=summary,
        tracker_namespace=tracker_ns,
        tracker_unavailable=bool(tracker_ns) and not qa_ok,
        ms=int((time.monotonic() - started) * 1000),
        stage_ms=stage_ms,
        trace_run_id=trace_run_id,
    )


async def _implicit_branches_readable(
    org: str, branches: list[tuple[str, str]], *, explicit: bool
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """When the caller names no branches, Loom expands to every configured release branch;
    the ones this principal cannot read (a branch just added to the registry, not yet
    registered or joined) are skipped and disclosed instead of refusing the whole call.
    Named branches fail closed, here, before any stage reads them."""
    if not branches:
        return branches, []
    from server.code.app import search_fanout as fanout

    if explicit:
        await fanout.authorize(org, [b for b, _ in branches], op="code.backport_gap")
        return branches, []
    readable, skipped = await fanout.readable_subset(org, [b for b, _ in branches])
    keep = set(readable)
    return [b for b in branches if b[0] in keep], skipped
