"""code.blame_range: a line range of one file names the merged PRs that last touched it
inside a release window.

Addressed at a CODE namespace. The window's two ends are release versions
(`26.3` → the release-branch namespace's recorded head when the caller may
read it, else the mirror's branch or `v26.3.1.1-new` tag; `26.3.4.12` → its
tag), commit shas, or empty (`to` defaults to the namespace's own head,
`from` to the oldest release cut the mirror knows). Two legs run together: loom's PR ledger
(`code_pr_history`, file lists — the floor) and `git blame -w -M -L` on the
bare mirror at the upper commit, each blamed commit mapped to its pull
request through the window's merge graph. The tracker namespace bound to this code namespace
adds `has_test` and `caused_by` / `fixes` edges per candidate when it is available; the
answer is complete without it.

The blame leg is exact for "last touched" and says so in `note`; it is not "introduced the
bug": blame-based attribution of bug-inducing changes is imprecise, and a sizeable share of
inducing changes lie beyond what blame reaches.

Stage `introduced` (the default) re-ranks the same candidates toward the change that
introduced the behaviour: demotions from the ledger (backport, revert, reverted) and from a
bounded per-candidate `git diff -w -U0` (whitespace-only, rename-only), promotions from the
enclosing function's record (the name in the title or body, a review comment inside the
span, the tracker diff when bound)
and from the caller's `mentions` in a linked issue's title; a blameless
range falls back to the window's file-touching PRs under
`introduced_unknown`. `last_touched` in the response keeps the first
stage's order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from server.code.api.models_history import CodeBlameRangeOutput
from server.code.app import search_fanout as fanout
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.index import gitrepo, merge_shas, mirrors
from server.code.app.index.bootstrap import history_enabled
from server.code.app.spans import code_span, set_code_span_attributes
from server.code.app.ttl_cache import TtlCache
from server.code.domain.index import blame_introduced as intro
from server.code.domain.index import blame_range as br
from server.code.domain.index import symbol_history as sh
from server.code.domain.index.compiler_tiers import state_value
from server.code.domain.index.repos import default_git_dir
from server.code.store.index import CodeRepo
from server.settings import settings
from server.tracker.domain import bisect as bi

logger = logging.getLogger(__name__)

_LEDGER_LIMIT = 200
_RELEASE_BASIS = "branch-cut tags (v<minor>.1.1-new dates); backport titles"
# (git_dir, kind, key...) -> value; blame text, window graphs and branch points,
# each `code_blame_cache_ttl_s`
_CACHE: TtlCache[tuple[Any, ...], Any] = TtlCache(
    lambda: settings.code_blame_cache_ttl_s, maxsize=512
)


# (git_dir, base sha) -> (tip, the `base..tip` graph): the widest window read so
# far for that base. History does not change, so a narrower window is derived from
# it in memory (`br.subgraph`) and a newer tip adds only its own commits.
_WIDE: dict[tuple[str, str], tuple[str, br.WindowGraph]] = {}
# Bases held at once (each a graph of up to `code_blame_max_graph_commits` commits); a caller's
# good_version picks the base, so the least recently used one is dropped.
_WIDE_MAX = 4
# One build per (git_dir, base) at a time: concurrent ranges of one bisect (same
# base, same parent) wait for the first instead of each walking history.
_WIDE_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def reset_cache_for_tests() -> None:
    _CACHE.clear()
    _WIDE.clear()
    _WIDE_LOCKS.clear()


NO_GIT_HISTORY = f"{mirrors.NO_GIT_HISTORY}: run git blame in a checkout"


@dataclass
class Window:
    from_ref: str = ""
    to_ref: str = ""
    from_sha: str = ""
    to_sha: str = ""
    base_sha: str = ""
    from_time: int = 0
    to_time: int = 0
    # namespace_head | mirror_ref | sha | default per end
    sources: dict[str, str] = field(default_factory=dict)
    ok: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_ref": self.from_ref,
            "to_ref": self.to_ref,
            "from_sha": self.from_sha,
            "to_sha": self.to_sha,
            "base_sha": self.base_sha,
            "from_time": _iso(self.from_time),
            "to_time": _iso(self.to_time),
            "sources": dict(self.sources),
            "error": self.error,
        }


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts > 0 else ""


def repo_siblings(org: str, ns: str) -> tuple[Any, list[Any]]:
    """(this namespace's repo config, every configured entry on the same
    bare clone) — the release-branch namespaces share the mirror."""
    from server.code.domain.index.repos import configured_repos

    cfgs = configured_repos()
    mine = next((c for c in cfgs if c.org == org and c.namespace == ns), None)
    if mine is None:
        return None, []
    return mine, [c for c in cfgs if c.org == org and c.git_dir == mine.git_dir]


async def history_git_dir(cfg: Any, op: str, *, head: str = "") -> tuple[str, str]:
    """`mirrors.history_dir` for a read of `op`, the scalar entry's mirror when no
    entry indexes the namespace."""
    if cfg is None:
        return default_git_dir(), ""
    return await mirrors.history_dir(cfg, op=op, head=head)


async def _namespace_head(repo: CodeRepo, org: str, ns: str) -> str:
    try:
        state = await repo.all_state(org, ns)
    except Exception as e:  # noqa: BLE001 — the mirror's ref is the fallback
        logger.debug("blame_range: state read failed for %s: %s", ns, e)
        return ""
    return state_value(state, "git_head")


async def _resolve_ref(
    repo: CodeRepo, org: str, git_dir: str, siblings: list[Any], text: str
) -> tuple[str, str]:
    """(sha, source) for one window end. A release minor resolves to the
    branch namespace's recorded head when one is configured, indexed and
    readable by the caller, else to the mirror's branch or `-new` tag; a
    build to its tag; a sha through rev-parse."""
    kind, value = br.classify_ref(text)
    if kind == "sha":
        try:
            out = await gitrepo.run(git_dir, "rev-parse", "--verify", "-q", f"{value}^{{commit}}")
            return out.strip(), "sha"
        except gitrepo.GitError:
            return "", ""
    if kind == "minor":
        branch_ns = next((c.namespace for c in siblings if c.git_branch == value), "")
        if branch_ns:
            readable, _skipped = await fanout.readable_subset(org, [branch_ns])
            head = await _namespace_head(repo, org, branch_ns) if readable else ""
            if head:
                return head, "namespace_head"
        patterns = [f"refs/heads/{value}", f"refs/tags/v{value}.1.1-new"]
    elif kind == "full":
        patterns = [f"refs/tags/v{value}", f"refs/tags/v{value}-*"]
    else:
        return "", ""
    try:
        text_out = await gitrepo.run(git_dir, "for-each-ref", "--format=%(objectname)", *patterns)
    except gitrepo.GitError:
        return "", ""
    shas = [line.strip() for line in text_out.splitlines() if line.strip()]
    return (shas[0], "mirror_ref") if shas else ("", "")


async def _branch_points(git_dir: str) -> list[tuple[str, int, str]]:
    key = (git_dir, "branch_points")
    hit = _CACHE.get(key)
    if hit is not None:
        return list(hit)
    try:
        text = await gitrepo.run(
            git_dir,
            "for-each-ref",
            "--format=%(refname:short) %(objectname) %(creatordate:unix)",
            "refs/tags/v*.1.1-new",
        )
    except gitrepo.GitError as e:
        logger.info("blame_range: branch points unread: %s", e)
        return []
    points = br.parse_branch_points(text)
    _CACHE.put(key, points)
    return points


async def _resolve_window(
    repo: CodeRepo,
    org: str,
    ns: str,
    git_dir: str,
    cfg: Any,
    siblings: list[Any],
    inp: Any,
    head: str | None = None,
) -> Window:
    """The blame window; `head`, when read already, is the namespace's indexed head."""
    w = Window(from_ref=inp.from_sha or inp.from_version, to_ref=inp.to_sha or inp.to_version)
    if w.to_ref:
        w.to_sha, src = await _resolve_ref(repo, org, git_dir, siblings, w.to_ref)
        w.sources["to"] = src
    else:
        w.to_sha = head if head is not None else await _namespace_head(repo, org, ns)
        w.sources["to"] = "namespace_head"
        if not w.to_sha and cfg is not None:
            try:
                w.to_sha = await gitrepo.head_sha(git_dir, cfg.git_branch)
                w.sources["to"] = "mirror_ref"
            except gitrepo.GitError:
                w.to_sha = ""
    if not w.to_sha:
        w.error = f"to did not resolve: {w.to_ref or '(namespace head)'}"
        return w
    if w.from_ref:
        w.from_sha, src = await _resolve_ref(repo, org, git_dir, siblings, w.from_ref)
        w.sources["from"] = src
    else:
        points = await _branch_points(git_dir)
        w.from_sha = points[0][2] if points else ""
        w.from_ref = f"v{points[0][0]}.1.1-new" if points else ""
        w.sources["from"] = "default"
    if not w.from_sha:
        w.error = f"from did not resolve: {w.from_ref or '(no release cut known)'}"
        return w
    try:
        w.base_sha = (await gitrepo.run(git_dir, "merge-base", w.from_sha, w.to_sha)).strip()
        times = await gitrepo.run(
            git_dir, "log", "--no-walk", "--format=%H %ct", w.base_sha, w.to_sha
        )
    except gitrepo.GitError as e:
        w.error = f"window commits unread: {e}"[:200]
        return w
    pairs = [ln.split() for ln in times.splitlines()]
    by_sha = {p[0]: int(p[1]) for p in pairs if len(p) == 2}
    w.from_time, w.to_time = by_sha.get(w.base_sha, 0), by_sha.get(w.to_sha, 0)
    w.ok = bool(w.base_sha and w.to_time)
    if not w.ok:
        w.error = "window commits unread"
    return w


async def _graph(git_dir: str, w: Window) -> br.WindowGraph:
    key = (git_dir, "graph", w.base_sha, w.to_sha)
    hit: br.WindowGraph | None = _CACHE.get(key)
    if hit is not None:
        return hit
    wide_key = (git_dir, w.base_sha)
    lock = _WIDE_LOCKS.setdefault(wide_key, asyncio.Lock())
    async with lock:
        hit = _CACHE.get(key)
        if hit is None:
            hit = await _window_graph(git_dir, w, wide_key)
            _CACHE.put(key, hit)
    return hit


async def _window_graph(git_dir: str, w: Window, wide_key: tuple[str, str]) -> br.WindowGraph:
    """`base..to` from the widest graph held for `base` when it holds `to` or its tip
    is an ancestor of `to` (then only `^tip to` is read); a full read otherwise."""
    held = _WIDE.pop(wide_key, None)
    if held is not None:
        _WIDE[wide_key] = held  # most recently used last
        tip, wide = held
        sub = br.subgraph(wide, w.to_sha)
        if sub is not None:
            return sub
        if not wide.truncated and await gitrepo.is_ancestor(git_dir, tip, w.to_sha):
            newer = br.parse_graph(
                await _rev_list(git_dir, w.to_sha, f"^{tip}", f"^{w.base_sha}"), w.to_sha
            )
            grown = br.merge_graphs(wide, newer, w.to_sha)
            if not grown.truncated:
                _hold(wide_key, w.to_sha, grown)
            return grown
    g = br.parse_graph(await _rev_list(git_dir, f"{w.base_sha}..{w.to_sha}"), w.to_sha)
    if not g.truncated and (held is None or len(g.parents) > len(held[1].parents)):
        _hold(wide_key, w.to_sha, g)
    return g


def _hold(wide_key: tuple[str, str], tip: str, graph: br.WindowGraph) -> None:
    _WIDE.pop(wide_key, None)
    _WIDE[wide_key] = (tip, graph)
    while len(_WIDE) > _WIDE_MAX:
        _WIDE.pop(next(iter(_WIDE)))


async def _rev_list(git_dir: str, *revs: str) -> str:
    return await gitrepo.run(
        git_dir,
        "rev-list",
        "--parents",
        "--format=%ct%x09%s",
        f"--max-count={settings.code_blame_max_graph_commits}",
        *revs,
        timeout_s=settings.code_blame_graph_timeout_s,
    )


async def _blame(
    git_dir: str, path: str, to_sha: str, start: int, end: int
) -> tuple[list[br.BlameLine], bool]:
    """(lines, warm) — one `git blame -w -M -L` per (path, to, range), cached."""
    key = (git_dir, "blame", path, to_sha, start, end)
    hit: list[br.BlameLine] | None = _CACHE.get(key)
    if hit is not None:
        return hit, True
    text = await gitrepo.run(
        git_dir,
        "blame",
        "-w",
        "-M",
        "--porcelain",
        "-L",
        f"{start},{end}",
        to_sha,
        "--",
        path,
        timeout_s=settings.code_blame_timeout_s,
    )
    lines = br.parse_porcelain(text)
    _CACHE.put(key, lines)
    return lines, False


@dataclass
class BlameLeg:
    lines: list[br.BlameLine] = field(default_factory=list)
    graph: br.WindowGraph = field(default_factory=br.WindowGraph)
    ok: bool = False
    warm: bool = False
    error: str = ""
    ms: int = 0


async def _blame_leg(git_dir: str, path: str, w: Window, start: int, end: int) -> BlameLeg:
    leg = BlameLeg()
    t0 = time.monotonic()
    try:
        (leg.lines, leg.warm), leg.graph = await asyncio.gather(
            _blame(git_dir, path, w.to_sha, start, end), _graph(git_dir, w)
        )
        leg.ok = True
    except gitrepo.GitError as e:
        msg = str(e)
        if "no such path" in msg:
            leg.error = "no_such_path"
        else:
            leg.error = "timeout" if "timed out" in msg else "git_error"
        logger.info("blame_range: blame leg failed (%s): %s", leg.error, msg[:200])
    leg.ms = int((time.monotonic() - t0) * 1000)
    return leg


async def _ledger_leg(
    repo: CodeRepo, org: str, ns: str, siblings: list[Any], path: str, w: Window
) -> tuple[list[dict[str, Any]], bool, list[dict[str, str]]]:
    """(rows, ok, skipped): ledger rows from this namespace and from the mirror's trunk
    namespace (a release-branch namespace holds its own backports only; the trunk entry
    holds the PR record), merged by PR number. The transport gated only `ns`, so the
    trunk is read only when the caller may read it, and named in `skipped` otherwise."""
    from server.code.domain.index.repos import trunk_entry

    namespaces = [ns]
    skipped: list[dict[str, str]] = []
    branch_of = {c.namespace: str(c.git_branch or "") for c in siblings}
    mine = next((c for c in siblings if c.namespace == ns), None)
    trunk = trunk_entry(mine, siblings) if mine is not None else None
    if trunk is not None:
        readable, skipped = await fanout.readable_subset(org, [trunk.namespace])
        namespaces.extend(readable)
    since = datetime.fromtimestamp(w.from_time, tz=UTC).replace(tzinfo=None)
    until = datetime.fromtimestamp(w.to_time, tz=UTC).replace(tzinfo=None)
    # Each namespace's rows must name its own branch (a backport filed under the master
    # namespace is not a master candidate).
    results = await asyncio.gather(
        *(
            repo.pr_history_in_window(
                org,
                n,
                path,
                since=since,
                until=until,
                limit=_LEDGER_LIMIT,
                branch=branch_of.get(n, ""),
            )
            for n in namespaces
        ),
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    ok = True
    for res in results:
        if isinstance(res, BaseException):
            logger.info("blame_range: ledger read failed: %s", res)
            ok = False
            continue
        rows.extend(res)
    return rows, ok, skipped


async def _tracker_side(
    org: str, ns: str, pr_numbers: list[int], *, names: list[str] | None = None
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]], str, bool]:
    """(pr facts, edges, qa namespace, ok) — optional; unbound → ({}, [], "", False).
    `names` (the enclosing function's bare name) makes the facts carry `matched_symbols`:
    which candidates' diffs name it."""
    from server.tracker.app.binding import authorized_tracker_namespace
    from server.tracker.app.source import tracker_source
    from server.tracker.domain.errors import TrackerUnavailable

    tracker_ns = await authorized_tracker_namespace(org, ns, hides_answer=False)
    if not tracker_ns or not pr_numbers:
        return {}, [], tracker_ns, False
    source = tracker_source(org, tracker_ns)
    try:
        facts = await source.prs_facts(org, tracker_ns, pr_numbers, names=names or None)
        refs = [f"{f['repo']}#{n}" for n, f in facts.items() if f.get("repo")]
        edges = (
            await source.edges_from(org, tracker_ns, refs, ["fixes", "caused_by"]) if refs else []
        )
    except TrackerUnavailable as e:
        logger.info("blame_range: qa unavailable on %s (%s)", tracker_ns, e)
        return {}, [], tracker_ns, False
    return facts, edges, tracker_ns, True


def _fold_tracker(
    candidates: list[br.Candidate], facts: dict[int, dict[str, Any]], edges: list[dict[str, Any]]
) -> None:
    from server.code.domain.index.symbol_history import _num

    caused: dict[int, list[int]] = {}
    fixes: dict[int, list[int]] = {}
    for e in edges:
        src, dst = _num(str(e.get("src_ref") or "")), _num(str(e.get("dst_ref") or ""))
        rel, direction = str(e.get("rel_type") or ""), str(e.get("direction") or "")
        if src and dst and rel == "caused_by" and direction == "in":
            caused.setdefault(src, []).append(dst)
        if src and dst and rel == "fixes" and direction == "out":
            fixes.setdefault(src, []).append(dst)
    for c in candidates:
        fact = facts.get(c.pr_number)
        if fact is not None and int(fact.get("has_test", -1)) >= 0:
            c.has_test = bool(int(fact["has_test"]))
        if fact is not None and not c.first_release and fact.get("minor_version"):
            c.first_release = str(fact["minor_version"])
        c.caused = sorted(set(caused.get(c.pr_number, [])))
        c.fixes = sorted(set(fixes.get(c.pr_number, [])))


def _ledger_in_graph(rows: list[dict[str, Any]], graph: br.WindowGraph) -> list[dict[str, Any]]:
    """Keep the ledger rows whose merge commit is inside the window's graph
    (a date window over master would count PRs a release branch never
    took); rows whose sha the graph lacks stay when the graph was cut."""
    if not graph.parents:
        return rows
    commits = graph.commits
    kept = [r for r in rows if br.sha_text(r.get("merge_commit_sha")) in commits]
    return kept if not graph.truncated else rows


@dataclass
class IntroducedLeg:
    """The second stage's record: the enclosing symbol, one Mark per candidate, the first
    stage's order, and what it cost."""

    stage: str = intro.STAGE_LAST_TOUCHED
    ok: bool = False
    symbol: dict[str, Any] = field(default_factory=dict)
    frames: list[str] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    marks: dict[int, intro.Mark] = field(default_factory=dict)
    last_touched: list[dict[str, Any]] = field(default_factory=list)
    git_calls: int = 0
    blameless: bool = False
    ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        marks = list(self.marks.values())
        return {
            "status": "ok" if self.ok else "skipped",
            "ms": self.ms,
            "git_calls": self.git_calls,
            "demoted": sum(1 for m in marks if m.demoted),
            "promoted": sum(1 for m in marks if m.promoted),
            "blameless": self.blameless,
        }


async def _symbol_and_comments(
    repo: CodeRepo, org: str, ns: str, path: str, start: int, end: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the enclosing function's row, the human review comments on the
    file), read beside the legs; either read failing leaves its half empty."""
    got = await asyncio.gather(
        repo.functions_for_paths(org, ns, [path], limit=2000),
        repo.review_comments_for(org, ns, [path], limit=120, humans_only=True),
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = [] if isinstance(got[0], BaseException) else list(got[0])
    comments: list[dict[str, Any]] = [] if isinstance(got[1], BaseException) else list(got[1])
    for name, res in (("functions", got[0]), ("review comments", got[1])):
        if isinstance(res, BaseException):
            logger.info("blame_range: %s read failed: %s", name, res)
    sym = intro.enclosing_symbol(rows, start, end)
    return (dict(sym) if sym else {}), comments


async def _no_symbol() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return {}, []


def _order_view(candidates: list[br.Candidate]) -> list[dict[str, Any]]:
    """The first stage's ranking, as the response's `last_touched`."""
    return [
        {
            "pr_number": c.pr_number,
            "rank": i,
            "lines_blamed": c.lines_blamed,
            "evidence": c.evidence,
        }
        for i, c in enumerate(candidates, start=1)
    ]


async def diff_kind(
    git_dir: str, sha: str, path: str | tuple[str, ...], frames: list[str]
) -> tuple[str, bool | None, bool]:
    """(classify_diff of the PR's change to `path` (one path, or the tuple of paths a bisect
    candidate is judged on), whether its hunks land in the function named by `frames`,
    whether a git call ran): one `git diff -w -U0 <sha>^1 <sha> -- <paths>`, cached; a
    failing call marks nothing."""
    paths = (path,) if isinstance(path, str) else tuple(path)
    key = (git_dir, "diff", sha, paths)
    text: str | None = _CACHE.get(key)
    called = text is None
    if text is None:
        try:
            text = await gitrepo.run(
                git_dir,
                "diff",
                "-w",
                "-U0",
                "--no-color",
                f"{sha}^1",
                sha,
                "--",
                *paths,
                timeout_s=settings.code_blame_introduced_diff_timeout_s,
            )
        except gitrepo.GitError as e:
            logger.info("blame_range: diff of %s failed: %s", sha[:12], str(e)[:160])
            return "", None, True
        _CACHE.put(key, text)
    return intro.classify_diff(text), intro.diff_touches(text, frames), called


async def _issue_titles(
    repo: CodeRepo, org: str, ns: str, linked: dict[int, list[int]], words: list[str]
) -> dict[int, str]:
    nums = sorted({n for ns_ in linked.values() for n in ns_ if n > 0})[:64]
    if not words or not nums:
        return {}
    try:
        items = await repo.items_by_number(org, ns, nums)
    except Exception as e:  # noqa: BLE001 — a promotion source, not a leg
        logger.info("blame_range: issue titles unread: %s", e)
        return {}
    return {n: str(r.get("title") or "") for n, r in items.items()}


async def _introduced_stage(
    repo: CodeRepo,
    org: str,
    ns: str,
    git_dir: str,
    path: str,
    rng: tuple[int, int],
    candidates: list[br.Candidate],
    att: br.Attribution,
    leg: BlameLeg,
    ledger_rows: list[dict[str, Any]],
    *,
    mentions: str,
    tracker_facts: dict[int, dict[str, Any]],
    qa_ok: bool,
    pre: IntroducedLeg,
) -> tuple[list[br.Candidate], IntroducedLeg]:
    """Mark and re-rank the first stage's candidates. Reads: the linked issues' titles when
    `mentions` is given (one ledger read) and at most `code_blame_introduced_max_diff_calls`
    bounded diffs; everything else is already here."""
    t0 = time.monotonic()
    out = pre
    out.last_touched = _order_view(candidates)
    span = (int(out.symbol.get("start_line") or rng[0]), int(out.symbol.get("end_line") or rng[1]))
    bodies = {int(r.get("pr_number") or 0): str(r.get("body_excerpt") or "") for r in ledger_rows}
    linked = {
        int(r.get("pr_number") or 0): [int(x) for x in (r.get("linked_issues") or []) if int(x) > 0]
        for r in ledger_rows
    }
    words = intro.behaviour_words(mentions)
    span_prs = {
        int(c.get("pr_number") or 0)
        for c in out.comments
        if sh.in_span(int(c.get("line") or 0), span[0], span[1])
    }
    tracker_names = {
        n: [str(x) for x in (f.get("matched_symbols") or [])] for n, f in tracker_facts.items()
    }

    def _mark(
        diff_kinds: dict[int, str], touch: dict[int, bool], titles: dict[int, str]
    ) -> dict[int, intro.Mark]:
        return intro.mark_candidates(
            candidates,
            bodies=bodies,
            linked=linked,
            issue_titles=titles,
            words=words,
            frames=out.frames,
            span_comment_prs=span_prs,
            tracker_names=tracker_names,
            qa_ok=qa_ok,
            diff_kinds=diff_kinds,
            diff_touch=touch,
        )

    first = _mark({}, {}, {})
    targets = intro.diff_order(candidates, first)
    got = await asyncio.gather(
        _issue_titles(repo, org, ns, linked, words),
        *(diff_kind(git_dir, c.merge_commit_sha, path, out.frames) for c in targets),
    )
    titles = cast(dict[int, str], got[0])
    kinds = cast(list[tuple[str, bool | None, bool]], list(got[1:]))
    out.git_calls = sum(1 for _k, _t, called in kinds if called)
    diff_kinds = {c.pr_number: k for c, (k, _t, _c) in zip(targets, kinds, strict=True) if k}
    touch = {c.pr_number: t for c, (_k, t, _c) in zip(targets, kinds, strict=True) if t is not None}
    out.marks = _mark(diff_kinds, touch, titles)
    for c in candidates:
        c.mark = out.marks.get(c.pr_number)
    attributed = sum(att.lines.values())
    out.blameless = intro.is_blameless(
        blame_ok=leg.ok,
        attributed_lines=attributed,
        noise_only=att.noise_only,
    )
    # a noise-only range is `introduced_unknown` (the lines were braces and
    # comments) but blame's pick stays the first key: measured 4/4 right on
    # the three-release gold set when the blame keys were dropped instead
    ranked = intro.rank_introduced(
        candidates,
        out.marks,
        blameless=not leg.ok or attributed <= 0,
    )
    out.ok = True
    out.ms = int((time.monotonic() - t0) * 1000)
    return ranked, out


async def handle_code_blame_range(input: Any) -> Any:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    cfg, siblings = repo_siblings(org, ns)
    # the window's default end is the indexed head: read once, for both
    head = (
        await _namespace_head(repo, org, ns)
        if cfg is not None and not (input.to_sha or input.to_version)
        else None
    )
    git_dir, gap = await history_git_dir(cfg, "code.blame_range", head=head or "")
    gh_repo = (cfg.repo if cfg is not None else "") or settings.code_repo
    path = input.path.strip().lstrip("/")
    start, end, truncated = br.clamp_range(input.start_line, input.end_line)
    stage = str(getattr(input, "stage", "") or intro.STAGE_INTRODUCED)
    introduced = stage == intro.STAGE_INTRODUCED
    stage_leg = IntroducedLeg(stage=stage)
    with code_span("blame_range.window"):
        if not history_enabled():
            w = Window(error=NO_GIT_HISTORY)
        elif gap:
            w = Window(error=gap)
        else:
            w = await _resolve_window(repo, org, ns, git_dir, cfg, siblings, input, head)
    trace_run_id = new_trace_run_id()
    if not w.ok:
        return _finish(
            input,
            org,
            ns,
            path,
            (start, end, truncated),
            w,
            [],
            br.Attribution(),
            BlameLeg(),
            ledger_ok=False,
            path_indexed=False,
            qa=("", False),
            started=started,
            trace_run_id=trace_run_id,
            output_cls=CodeBlameRangeOutput,
            branch_points=[],
            stage_leg=stage_leg,
        )
    with code_span("blame_range.legs", lines=end - start + 1):
        (
            (ledger_rows, ledger_ok, ledger_skipped),
            leg,
            indexed,
            points,
            (sym, comments),
        ) = await asyncio.gather(
            _ledger_leg(repo, org, ns, siblings, path, w),
            _blame_leg(git_dir, path, w, start, end),
            _indexed(repo, org, ns, path),
            _branch_points(git_dir),
            _symbol_and_comments(repo, org, ns, path, start, end) if introduced else _no_symbol(),
        )
    stage_leg.comments = comments
    if sym:
        stage_leg.symbol = {
            "qualified_name": str(sym.get("qualified_name") or ""),
            "start_line": int(sym.get("start_line") or 0),
            "end_line": int(sym.get("end_line") or 0),
        }
        stage_leg.frames = bi.frame_names([stage_leg.symbol["qualified_name"]])
    ledger_rows = _ledger_in_graph(ledger_rows, leg.graph) if leg.ok else ledger_rows
    pr_by_sha = {
        br.sha_text(r.get("merge_commit_sha")): int(r.get("pr_number") or 0) for r in ledger_rows
    }
    att = br.attribute(leg.lines, leg.graph, path, pr_by_sha) if leg.ok else br.Attribution()
    candidates = br.merge_legs(
        att,
        ledger_rows,
        range_len=end - start + 1,
        branch_points=points,
        repo=gh_repo,
        limit=max(input.limit, intro.STAGE_POOL) if introduced else input.limit,
    )
    await fill_merge_shas(git_dir, w.to_sha, candidates)
    tracker_ns, qa_ok = "", False
    facts: dict[int, dict[str, Any]] = {}
    if candidates:
        with code_span("blame_range.qa", seeds=len(candidates)):
            facts, edges, tracker_ns, qa_ok = await _tracker_side(
                org,
                ns,
                [c.pr_number for c in candidates],
                names=stage_leg.frames,
            )
        if qa_ok:
            _fold_tracker(candidates, facts, edges)
    if introduced and candidates:
        with code_span("blame_range.introduced", seeds=len(candidates)):
            candidates, stage_leg = await _introduced_stage(
                repo,
                org,
                ns,
                git_dir,
                path,
                (start, end),
                candidates,
                att,
                leg,
                ledger_rows,
                mentions=str(getattr(input, "mentions", "") or ""),
                tracker_facts=facts,
                qa_ok=qa_ok,
                pre=stage_leg,
            )
        candidates = candidates[: input.limit]
    else:
        stage_leg.last_touched = _order_view(candidates)
    await _attach_reviews(repo, org, ns, path, (start, end), candidates, input.reviews_per_pr)
    return _finish(
        input,
        org,
        ns,
        path,
        (start, end, truncated),
        w,
        candidates,
        att,
        leg,
        ledger_ok=ledger_ok,
        ledger_skipped=ledger_skipped,
        path_indexed=indexed,
        qa=(tracker_ns, qa_ok),
        started=started,
        trace_run_id=trace_run_id,
        output_cls=CodeBlameRangeOutput,
        branch_points=points,
        stage_leg=stage_leg,
    )


async def fill_merge_shas(git_dir: str, ref: str, candidates: list[Any]) -> None:
    """A candidate neither the ledger nor the window graph gave a merge commit gets one
    from the mirror: one first-parent search of `ref` for all of them."""
    missing = [c for c in candidates if not c.merge_commit_sha]
    if not missing:
        return
    since = min((br.unix_of(c.merged_at) for c in missing), default=0)
    got = await merge_shas.resolve(
        git_dir, ref, [c.pr_number for c in missing], {}, since_unix=since
    )
    for c in missing:
        c.merge_commit_sha, c.merge_sha_source = got.get(c.pr_number, ("", ""))


async def _attach_reviews(
    repo: CodeRepo,
    org: str,
    ns: str,
    path: str,
    rng: tuple[int, int],
    candidates: list[br.Candidate],
    per_pr: int,
) -> None:
    """Each returned candidate's human review comments on a line inside the range, plus its
    count on the file; one read for all."""
    from server.code.app.index.pr_reviews import reviews_for

    if not candidates or per_pr <= 0:
        return
    with code_span("blame_range.reviews", seeds=len(candidates)):
        got = await reviews_for(
            repo, org, ns, [c.pr_number for c in candidates], [path], per_pr=per_pr, lines=rng
        )
    for c in candidates:
        if got and c.pr_number in got:
            c.reviews, c.review_count = got[c.pr_number]


async def _indexed(repo: CodeRepo, org: str, ns: str, path: str) -> bool:
    try:
        return path in await repo.live_paths_present(org, ns, [path])
    except Exception as e:  # noqa: BLE001 — a fact for the verdict, not a leg
        logger.debug("blame_range: live_paths_present failed: %s", e)
        return False


def _finish(
    inp: Any,
    org: str,
    ns: str,
    path: str,
    rng: tuple[int, int, bool],
    w: Window,
    candidates: list[br.Candidate],
    att: br.Attribution,
    leg: BlameLeg,
    *,
    ledger_ok: bool,
    ledger_skipped: list[dict[str, str]] | None = None,
    path_indexed: bool,
    qa: tuple[str, bool],
    started: float,
    trace_run_id: str,
    output_cls: Any,
    branch_points: list[tuple[str, int, str]],
    stage_leg: IntroducedLeg,
) -> Any:
    start, end, truncated = rng
    verdict, causes = br.verdict_of(
        candidates=candidates,
        window_ok=w.ok,
        blame_ok=leg.ok,
        ledger_ok=ledger_ok,
        path_indexed=path_indexed,
        blame_error=leg.error,
    )
    epistemic = br.epistemic_of(leg.ok)
    note = br.NOTE + br.unmapped_note(att)
    if stage_leg.stage == intro.STAGE_INTRODUCED:
        note += intro.NOTE_INTRODUCED
        if stage_leg.ok and stage_leg.blameless and verdict == br.CANDIDATES:
            verdict, epistemic = intro.INTRODUCED_UNKNOWN, "lower_bound"
    tracker_ns, qa_ok = qa
    if tracker_ns and not qa_ok and candidates:
        causes = [*causes, "tracker_unavailable"]
    blamed = [c for c in candidates if c.lines_blamed]
    latest = 0
    if blamed:
        latest = max(blamed, key=lambda c: (br.unix_of(c.merged_at), c.pr_number)).pr_number
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.blame_range",
        query=f"{path}:{start}-{end}",
        route="blame",
        returned_ids=[f"pr:{c.pr_number}" for c in candidates],
        returned_paths=[path],
        returned_scores=[float(c.lines_blamed) for c in candidates],
        top_k=inp.limit,
        tokens_returned=0,
        abstain=verdict == br.UNKNOWN,
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=inp.consumer,
        agent=inp.agent,
        user_id=inp.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(route="blame", results=len(candidates), consumer=inp.consumer)
    return output_cls(
        verdict=verdict,
        causes=causes,
        epistemic=epistemic,
        note=note,
        stage=stage_leg.stage,
        path=path,
        path_indexed=path_indexed,
        range={
            "start": start,
            "end": end,
            "requested_end": int(inp.end_line),
            "truncated": truncated,
        },
        window=w.as_dict(),
        candidates=[c.as_dict() for c in candidates],
        latest_pr=latest,
        last_touched=list(stage_leg.last_touched),
        symbol=dict(stage_leg.symbol),
        lines=att.as_counts(),
        legs={
            "blame": {
                "status": "ok" if leg.ok else (leg.error or "skipped"),
                "warm": leg.warm,
                "ms": leg.ms,
                "window_commits": len(leg.graph.parents),
                "graph_truncated": leg.graph.truncated,
            },
            "ledger": {
                "status": "ok" if ledger_ok else "failed",
                **({"skipped": ledger_skipped} if ledger_skipped else {}),
            },
            "introduced": stage_leg.as_dict(),
        },
        first_release_basis=_RELEASE_BASIS if branch_points else "",
        tracker_namespace=tracker_ns,
        tracker_unavailable=bool(tracker_ns) and not qa_ok,
        trace_run_id=trace_run_id,
    )
