"""Pure helpers for `code.blame_range`: a line range of one file, the merged PRs that
last touched it inside a release window.

Two legs meet here. The LEDGER leg is loom's merged-PR record (`code_pr_history`: file
lists and `merged_at`) — every PR in the window that touched the file, a floor. The
BLAME leg is `git blame -w -M -L` at the window's upper commit on the bare mirror: one
commit per line, exact for "who last touched this line", mapped to its pull request
through the window's merge graph (one `rev-list --parents` over `base..to`; a blamed
commit maps to the OLDEST first-parent merge that contains it — the `Merge pull request
#N` that brought it in (the first one a breadth-first walk meets can be a later stacked
PR's merge when that path is shorter) — or to itself when it is a squash commit whose
subject carries `(#N)`).

Blame-leg rules: lines that are blank, comment-only or format-only (a lone brace or
bracket) carry no attribution, whitespace-only changes are skipped (`-w`), moved lines
follow their origin (`-M`), and ties break toward the LATEST change. Blame cannot see
every change that introduced a line (a sizeable share of inducing changes lie beyond
it), which is why the answer names "last touched", never "introduced".
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from server.settings import settings

CANDIDATES = "candidates"
NO_CANDIDATES = "no_candidates"
UNKNOWN = "unknown"

# Bounds: one blame per call, at most this many lines, cached this long per key; the git
# calls are bounded by `code_blame_timeout_s` and `code_blame_graph_timeout_s`.
MAX_LINES = 400
CACHE_TTL_S = 600.0
# The side-branch commits one blamed commit's merge search may visit (off the
# first-parent chain only). 431 was the most any of 2,613 blamed commits over five
# ClickHouse files needed.
MAX_MERGE_WALK = 20_000
MAX_COMMITS_SHOWN = 8

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_MINOR_RE = re.compile(r"^v?(\d{2})\.(\d{1,2})$")
_FULL_RE = re.compile(r"^v?(\d{2}\.\d{1,2}\.\d+\.\d+)(?:-[a-z]+)?$")
_PORCELAIN_HEAD_RE = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")
_MERGE_PR_RE = re.compile(r"Merge pull request #(\d+)")
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
_ANY_PR_RE = re.compile(r"#(\d+)")
_BACKPORT_RE = re.compile(r"(?i)^\s*backport\s+#\d+\s+to\s+(\d{2}\.\d{1,2})\b")
_NEW_TAG_RE = re.compile(r"^v(\d{2}\.\d{1,2})\.1\.1-new$")

_HASH_COMMENT_EXT = frozenset(
    {
        "py",
        "sh",
        "bash",
        "cmake",
        "txt",
        "yml",
        "yaml",
        "toml",
        "ini",
        "cfg",
        "rb",
        "pl",
    }
)
_DASH_COMMENT_EXT = frozenset({"sql", "lua", "hs"})
_FORMAT_ONLY = frozenset(
    {
        "{",
        "}",
        "};",
        "})",
        "});",
        "(",
        ")",
        ");",
        "[",
        "]",
        "],",
        "},",
        "else",
        "else {",
        ",",
        "#pragma once",
    }
)


# ---- window resolution -------------------------------------------------------


def classify_ref(text: str) -> tuple[str, str]:
    """`("sha", hex)` for an abbreviated or full commit, `("minor", "26.3")`
    for a release, `("full", "26.3.4.12")` for a build (a leading `v` and a
    `-lts`/`-stable` suffix are dropped), `("", "")` for anything else."""
    t = (text or "").strip()
    if not t:
        return "", ""
    m = _MINOR_RE.match(t)
    if m and 1 <= int(m.group(2)) <= 12:
        return "minor", f"{int(m.group(1))}.{int(m.group(2))}"
    m = _FULL_RE.match(t)
    if m:
        return "full", m.group(1)
    if _SHA_RE.match(t.lower()):
        return "sha", t.lower()
    return "", ""


def clamp_range(start: int, end: int) -> tuple[int, int, bool]:
    """`(start, end, truncated)`: a range wider than MAX_LINES is cut at the
    top, never refused — the caller learns the cut from `truncated`."""
    s = max(1, int(start))
    e = max(s, int(end))
    if e - s + 1 > MAX_LINES:
        return s, s + MAX_LINES - 1, True
    return s, e, False


def parse_branch_points(for_each_ref: str) -> list[tuple[str, int, str]]:
    """`[(minor, unix_time, sha)]` from `for-each-ref refs/tags/v*.1.1-new`
    (`<tag> <sha> <creatordate:unix>` per line), oldest first. `v26.3.1.1-new`
    is placed where master moves on to 26.3 — the commit the 26.2 release
    branch is cut at (`merge-base 26.2 26.3` is that commit on the mirror)."""
    out: list[tuple[str, int, str]] = []
    for line in (for_each_ref or "").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        m = _NEW_TAG_RE.match(parts[0])
        if not m or not parts[2].isdigit():
            continue
        out.append((m.group(1), int(parts[2]), parts[1]))
    out.sort(key=lambda t: t[1])
    return out


def first_release_of(
    merged_at: datetime | str | None, title: str, branch_points: list[tuple[str, int, str]]
) -> str:
    """The first release that carries a PR merged at `merged_at`: the
    release whose branch was cut first after the merge — the minor BEFORE
    the first `-new` tag dated after it (the tag `v26.3.1.1-new` cuts branch
    26.2). Newer than every cut → the minor master is on (the last tag's).
    A backport PR names its own branch in its title. "" when the merge
    predates the oldest known cut or no cut is known."""
    m = _BACKPORT_RE.match(title or "")
    if m:
        return m.group(1)
    ts = unix_of(merged_at)
    if ts <= 0 or not branch_points:
        return ""
    for i, (_minor, cut, _sha) in enumerate(branch_points):
        if cut >= ts:
            return branch_points[i - 1][0] if i > 0 else ""
    return branch_points[-1][0]


# ---- blame leg ---------------------------------------------------------------


@dataclass
class BlameLine:
    sha: str
    line: int
    content: str
    summary: str = ""
    author_time: int = 0


def parse_porcelain(text: str) -> list[BlameLine]:
    """`git blame --porcelain` → one BlameLine per final line, in order.
    Header lines carry the commit and the final line number; a commit's
    metadata (summary, author-time) follows its first header only."""
    out: list[BlameLine] = []
    meta: dict[str, dict[str, Any]] = {}
    current: BlameLine | None = None
    for raw in (text or "").splitlines():
        if raw.startswith("\t"):
            if current is not None:
                current.content = raw[1:]
                m = meta.setdefault(current.sha, {})
                current.summary = str(m.get("summary") or "")
                current.author_time = int(m.get("author-time") or 0)
                out.append(current)
                current = None
            continue
        m_head = _PORCELAIN_HEAD_RE.match(raw)
        if m_head:
            current = BlameLine(sha=m_head.group(1), line=int(m_head.group(3)), content="")
            continue
        if current is not None and " " in raw:
            key, value = raw.split(" ", 1)
            if key in ("summary", "author-time"):
                meta.setdefault(current.sha, {})[key] = value
    return out


def is_noise_line(content: str, path: str) -> bool:
    """Lines that carry no attribution: blank, whitespace-only, comment-only, or a
    lone brace / bracket. Comment syntax follows the file extension (C-family
    by default; `#` for scripts and configs; `--` for SQL)."""
    s = (content or "").strip()
    if not s or s in _FORMAT_ONLY:
        return True
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if ext in _HASH_COMMENT_EXT:
        return s.startswith("#")
    if ext in _DASH_COMMENT_EXT:
        return s.startswith("--")
    return s.startswith(("//", "/*", "*/", "* ")) or s == "*"


@dataclass
class WindowGraph:
    """The commits of `base..to`: parents per commit, subjects, and the
    first-parent chain (the merges that landed on the target branch)."""

    parents: dict[str, list[str]] = field(default_factory=dict)
    subjects: dict[str, str] = field(default_factory=dict)
    times: dict[str, int] = field(default_factory=dict)  # committer time, unix
    chain: list[str] = field(default_factory=list)
    truncated: bool = False
    _chain_pos: dict[str, int] | None = field(default=None, repr=False, compare=False)

    @property
    def commits(self) -> set[str]:
        return set(self.parents)

    @property
    def chain_pos(self) -> dict[str, int]:
        """chain sha -> position, newest first (0 = `to`); built once."""
        if self._chain_pos is None:
            self._chain_pos = {sha: i for i, sha in enumerate(self.chain)}
        return self._chain_pos


def parse_graph(rev_list: str, to_sha: str) -> WindowGraph:
    """`git rev-list --parents --format=%ct%x09%s base..to` → WindowGraph.
    Each commit is a `commit <sha> <parent>...` line followed by its
    committer time and subject (tab-separated; a bare subject still parses)."""
    g = WindowGraph()
    pending: str | None = None
    for raw in (rev_list or "").splitlines():
        if raw.startswith("commit "):
            parts = raw.split()
            if len(parts) >= 2:
                g.parents[parts[1]] = parts[2:]
                pending = parts[1]
            continue
        if pending is not None:
            ts, _tab, subject = raw.partition("\t")
            if _tab and ts.strip().isdigit():
                g.times[pending] = int(ts.strip())
            else:
                subject = raw
            g.subjects[pending] = subject.strip()
            pending = None
    g.truncated = len(g.parents) >= settings.code_blame_max_graph_commits
    g.chain = _first_parent_chain(g.parents, to_sha)
    return g


def _first_parent_chain(parents: dict[str, list[str]], to_sha: str) -> list[str]:
    chain: list[str] = []
    sha = to_sha
    while sha in parents:
        chain.append(sha)
        ps = parents[sha]
        if not ps:
            break
        sha = ps[0]
    return chain


def subgraph(wide: WindowGraph, to_sha: str) -> WindowGraph | None:
    """`base..to` out of a `base..tip` graph that holds `to`: the commits
    reachable from `to` inside it. That is exact, not an approximation: a commit
    outside the graph is reachable from `base`, and so is every ancestor of it.
    None when `to` is not in the graph, or the graph was truncated (its oldest
    commits are missing, so a narrower window could lack some)."""
    if wide.truncated or to_sha not in wide.parents:
        return None
    keep: set[str] = set()
    stack = [to_sha]
    while stack:
        sha = stack.pop()
        if sha in keep or sha not in wide.parents:
            continue
        keep.add(sha)
        stack.extend(wide.parents[sha])
    parents = {s: wide.parents[s] for s in keep}
    return WindowGraph(
        parents=parents,
        subjects={s: wide.subjects[s] for s in keep if s in wide.subjects},
        times={s: wide.times[s] for s in keep if s in wide.times},
        chain=_first_parent_chain(parents, to_sha),
    )


def merge_graphs(wide: WindowGraph, newer: WindowGraph, to_sha: str) -> WindowGraph:
    """`base..to` from `base..tip` plus `^base ^tip to` when `tip` is an ancestor of
    `to`: the new commits reach the old ones, and nothing else changes."""
    parents = {**wide.parents, **newer.parents}
    return WindowGraph(
        parents=parents,
        subjects={**wide.subjects, **newer.subjects},
        times={**wide.times, **newer.times},
        chain=_first_parent_chain(parents, to_sha),
        truncated=wide.truncated
        or newer.truncated
        or len(parents) >= settings.code_blame_max_graph_commits,
    )


def pr_number_of(subject: str) -> int:
    """`Merge pull request #N from …` → N; a squash `… (#N)` → N; else the
    first `#N` in the subject; 0 when none."""
    for rx in (_MERGE_PR_RE, _SQUASH_PR_RE, _ANY_PR_RE):
        m = rx.search(subject or "")
        if m:
            return int(m.group(1))
    return 0


def landed_pr_of(subject: str) -> int:
    """The PR a target-branch commit landed: `Merge pull request #N …` or a squash's
    trailing `(#N)`; 0 otherwise (a bare `#N` names a PR, it does not land one)."""
    for rx in (_MERGE_PR_RE, _SQUASH_PR_RE):
        m = rx.search(subject or "")
        if m:
            return int(m.group(1))
    return 0


def merge_of(sha: str, graph: WindowGraph, children: dict[str, list[str]]) -> str:
    """The first-parent-chain commit that brought `sha` into the target
    branch: `sha` itself when it is on the chain, else the OLDEST chain node
    that has it as an ancestor. "" when the walk leaves the window.

    That node is reached from `sha` through side-branch commits only (a path through an
    earlier chain node would make that node the older answer), so the walk never expands
    a chain node and keeps the oldest one it meets. Stopping at the first chain node met
    would pick a later stacked PR's merge whenever its path from `sha` is shorter."""
    pos = graph.chain_pos
    if sha in pos:
        return sha
    best, best_pos = "", -1
    seen = {sha}
    queue: deque[str] = deque([sha])
    while queue and len(seen) < MAX_MERGE_WALK:
        cur = queue.popleft()
        for child in children.get(cur, ()):
            if child in seen:
                continue
            seen.add(child)
            p = pos.get(child)
            if p is None:
                queue.append(child)
            elif p > best_pos:
                best, best_pos = child, p
    return best


def children_of(graph: WindowGraph) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sha, ps in graph.parents.items():
        for p in ps:
            out.setdefault(p, []).append(sha)
    return out


@dataclass
class Attribution:
    """Per PR: the lines it last touched, and the commit that carried them."""

    lines: dict[int, int] = field(default_factory=dict)  # pr_number -> lines
    # Every in-window line the PR's commits carry, noise included (what plain `git blame
    # -w` counts), and those commits
    touched: dict[int, int] = field(default_factory=dict)
    commits: dict[int, list[str]] = field(default_factory=dict)
    merge_sha: dict[int, str] = field(default_factory=dict)
    latest_ts: dict[int, int] = field(default_factory=dict)
    subject: dict[int, str] = field(default_factory=dict)
    total: int = 0
    noise: int = 0
    outside_window: int = 0
    unmapped: int = 0
    # every line in the window was noise: the attribution below counts them
    noise_only: bool = False

    def as_counts(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "attributed": sum(self.lines.values()),
            "noise": self.noise,
            "outside_window": self.outside_window,
            "unmapped": self.unmapped,
            "noise_only": self.noise_only,
        }


def attribute(
    blame: list[BlameLine],
    graph: WindowGraph,
    path: str,
    pr_by_merge_sha: dict[str, int] | None = None,
) -> Attribution:
    """Fold blamed lines into PRs. A line blamed to a commit outside
    `base..to` was last touched before the window (counted, not ranked); a
    noise line carries nothing; a commit whose merge the graph cannot find
    is `unmapped`. PR numbers come from the merge's subject, then from the
    ledger's `merge_commit_sha` map, then from the blamed commit's own
    subject (a squash). When every in-window line is noise (a fix that
    deleted only braces or comments) the noise lines are attributed after
    all and `noise_only` says so — "last touched" still has an answer."""
    att = _attribute(blame, graph, path, pr_by_merge_sha or {}, keep_noise=False)
    if not att.lines and att.noise and att.noise > att.outside_window:
        again = _attribute(blame, graph, path, pr_by_merge_sha or {}, keep_noise=True)
        if again.lines:
            again.noise, again.noise_only = att.noise, True
            return again
    return att


def _attribute(
    blame: list[BlameLine],
    graph: WindowGraph,
    path: str,
    ledger: dict[str, int],
    *,
    keep_noise: bool,
) -> Attribution:
    att = Attribution(total=len(blame))
    children = children_of(graph)
    merge_cache: dict[str, str] = {}
    for bl in blame:
        noise = not keep_noise and is_noise_line(bl.content, path)
        if noise:
            att.noise += 1
        if bl.sha not in graph.parents:
            att.outside_window += 0 if noise else 1
            continue
        pr, merge = _pr_of(bl, graph, children, ledger, merge_cache)
        if not pr:
            att.unmapped += 0 if noise else 1
            continue
        _credit(att, pr, bl, graph, merge, substantive=not noise)
    return att


def _pr_of(
    bl: BlameLine,
    graph: WindowGraph,
    children: dict[str, list[str]],
    ledger: dict[str, int],
    merge_cache: dict[str, str],
) -> tuple[int, str]:
    """(pr_number, merge sha) for one blamed line's commit: the merge's
    subject, then the ledger's `merge_commit_sha` map, then the commit's own
    subject (a squash). (0, merge) when none names a PR."""
    if bl.sha not in merge_cache:
        merge_cache[bl.sha] = merge_of(bl.sha, graph, children)
    merge = merge_cache[bl.sha]
    pr = 0
    if merge:
        pr = pr_number_of(graph.subjects.get(merge, "")) or ledger.get(merge, 0)
    if not pr:
        pr = ledger.get(bl.sha, 0) or pr_number_of(bl.summary)
    return pr, merge


def _credit(
    att: Attribution,
    pr: int,
    bl: BlameLine,
    graph: WindowGraph,
    merge: str,
    *,
    substantive: bool,
) -> None:
    att.touched[pr] = att.touched.get(pr, 0) + 1
    shas = att.commits.setdefault(pr, [])
    if bl.sha not in shas:
        shas.append(bl.sha)
    if not substantive:
        return
    att.lines[pr] = att.lines.get(pr, 0) + 1
    att.merge_sha.setdefault(pr, merge or bl.sha)
    att.subject.setdefault(pr, graph.subjects.get(merge, "") or bl.summary)
    landed = graph.times.get(merge or bl.sha, 0) or bl.author_time
    att.latest_ts[pr] = max(att.latest_ts.get(pr, 0), landed)


# ---- candidates --------------------------------------------------------------


@dataclass
class Candidate:
    pr_number: int
    title: str = ""
    author: str = ""
    merged_at: str = ""
    first_release: str = ""
    lines_blamed: int = 0
    share_of_range: float = 0.0
    # Lines incl. blank / comment / brace ones, and the blamed commits credited to this
    # PR (every commit of a multi-commit PR)
    lines_in_range: int = 0
    commits: list[str] = field(default_factory=list)
    # Review comments on the range (None = not asked for)
    reviews: list[dict[str, Any]] | None = None
    review_count: int = 0
    evidence: str = "ledger"  # blame | ledger | both
    merge_commit_sha: str = ""
    # stored (the ledger) | mirror (the window graph or a first-parent search) | ""
    merge_sha_source: str = ""
    kind: str = "change"
    url: str = ""
    has_test: bool | None = None
    caused: list[int] = field(default_factory=list)
    fixes: list[int] = field(default_factory=list)
    # What the `introduced` stage learned (None under `last_touched`, so that stage's
    # dicts keep the plain `last_touched` shape)
    mark: Any = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pr_number": self.pr_number,
            "title": self.title[:160],
            "author": self.author,
            "merged_at": self.merged_at,
            "first_release": self.first_release,
            "lines_blamed": self.lines_blamed,
            "share_of_range": round(self.share_of_range, 3),
            "touches_file": True,
            "evidence": self.evidence,
            "kind": self.kind,
            "merge_commit_sha": self.merge_commit_sha,
            "merge_sha_source": self.merge_sha_source,
            "url": self.url,
        }
        if self.lines_in_range:
            out["lines_in_range"] = self.lines_in_range
        if self.commits:
            out["commits"] = [c[:12] for c in self.commits[:MAX_COMMITS_SHOWN]]
        if self.reviews is not None:
            out["reviews"] = self.reviews
            out["review_count"] = self.review_count
        if self.has_test is not None:
            out["has_test"] = self.has_test
        if self.caused:
            out["caused_by"] = self.caused[:6]
        if self.fixes:
            out["fixes"] = self.fixes[:6]
        if self.mark is not None:
            self.mark.fold_into(out)
        return out


def merge_legs(
    att: Attribution,
    ledger_rows: list[dict[str, Any]],
    *,
    range_len: int,
    branch_points: list[tuple[str, int, str]],
    repo: str = "",
    limit: int = 10,
) -> list[Candidate]:
    """One candidate per PR: blame-attributed PRs first by lines touched,
    then by recency (ties keep the latest); ledger-only PRs after them,
    newest first. A PR both legs name is `both`."""
    from server.tracker.domain.bisect import kind_of

    by_pr: dict[int, dict[str, Any]] = {}
    for r in ledger_rows:
        n = int(r.get("pr_number") or 0)
        newer = n in by_pr and unix_of(r.get("merged_at")) > unix_of(by_pr[n].get("merged_at"))
        if n and (n not in by_pr or newer):
            by_pr[n] = r
    out: list[Candidate] = []
    for n in set(by_pr) | set(att.lines):
        row = by_pr.get(n, {})
        lines = att.lines.get(n, 0)
        title = str(row.get("title") or att.subject.get(n, "") or "")
        merged = _iso(row.get("merged_at")) if row else _iso_from_unix(att.latest_ts.get(n, 0))
        stored = sha_text(row.get("merge_commit_sha"))
        sha = stored or att.merge_sha.get(n, "")
        out.append(
            Candidate(
                pr_number=n,
                title=title,
                author=str(row.get("author") or ""),
                merged_at=merged,
                first_release=first_release_of(merged, title, branch_points),
                lines_blamed=lines,
                lines_in_range=att.touched.get(n, 0) if lines else 0,
                commits=list(att.commits.get(n, ())) if lines else [],
                share_of_range=(lines / range_len) if range_len else 0.0,
                evidence="both" if (lines and row) else ("blame" if lines else "ledger"),
                merge_commit_sha=sha,
                merge_sha_source=("stored" if stored else "mirror") if sha else "",
                kind=kind_of(title),
                url=f"https://github.com/{repo}/pull/{n}" if repo else "",
            )
        )
    out.sort(key=lambda c: (-c.lines_blamed, -unix_of(c.merged_at), -c.pr_number))
    return out[:limit]


def verdict_of(
    *,
    candidates: list[Candidate],
    window_ok: bool,
    blame_ok: bool,
    ledger_ok: bool,
    path_indexed: bool,
    blame_error: str = "",
) -> tuple[str, list[str]]:
    """`candidates` when either leg named one; `no_candidates` when both
    legs ran and found nothing in the window; `unknown` with the cause when
    the window did not resolve, the path is not in the tree / index, or
    the blame failed with nothing to fall back on."""
    if not window_ok:
        return UNKNOWN, ["window_unresolved"]
    causes: list[str] = []
    if not blame_ok:
        causes.append("path_not_indexed" if blame_error == "no_such_path" else "blame_failed")
    if not ledger_ok:
        causes.append("ledger_failed")
    if candidates:
        return CANDIDATES, causes
    if not blame_ok or not ledger_ok:
        return UNKNOWN, causes
    if not path_indexed:
        return UNKNOWN, ["path_not_indexed"]
    return NO_CANDIDATES, []


def unmapped_note(att: Attribution) -> str:
    """Say so when blamed lines' commits named no PR."""
    if not att.unmapped:
        return ""
    return (
        f" {att.unmapped} blamed line(s) came from commits no PR could be mapped to "
        "(no first-parent merge inside the window contains them, and neither the merge "
        "subject, the ledger's merge_commit_sha nor the commit subject names a PR); "
        "they are counted in lines.unmapped, not credited."
    )


def epistemic_of(blame_ok: bool) -> str:
    return "exact" if blame_ok else "lower_bound"


NOTE = (
    "git blame is exact for the merged PR that LAST touched each line at to_sha "
    "('last touched'), not for the PR that introduced the bug: a later "
    "refactor, rename or reformat of a line takes the attribution (whitespace-only "
    "changes are skipped, moved lines follow their origin, blank/comment/format-only "
    "lines carry none). A ledger-only candidate touched the file inside the window, "
    "not necessarily the range. lines_blamed counts a PR's substantive lines; "
    "lines_in_range adds its blank/comment/brace lines, and commits lists every "
    "blamed commit credited to it."
)


# ---- small conversions -------------------------------------------------------


def sha_text(value: Any) -> str:
    """A FixedString(40) sha as hex text (the client hands bytes back)."""
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("ascii", errors="ignore").rstrip("\x00")
    return str(value or "").rstrip("\x00")


def unix_of(value: Any) -> int:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(dt.timestamp())
    if isinstance(value, int | float):
        return int(value)
    s = str(value or "")
    if not s:
        return 0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value or "")


def _iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts > 0 else ""


__all__ = [
    "CACHE_TTL_S",
    "CANDIDATES",
    "MAX_LINES",
    "NOTE",
    "NO_CANDIDATES",
    "UNKNOWN",
    "Attribution",
    "BlameLine",
    "Candidate",
    "WindowGraph",
    "attribute",
    "children_of",
    "clamp_range",
    "classify_ref",
    "epistemic_of",
    "first_release_of",
    "is_noise_line",
    "landed_pr_of",
    "merge_legs",
    "merge_of",
    "parse_branch_points",
    "parse_graph",
    "parse_porcelain",
    "pr_number_of",
    "sha_text",
    "unix_of",
    "unmapped_note",
    "verdict_of",
]
