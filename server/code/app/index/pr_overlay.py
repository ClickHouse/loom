"""A pull request's post-image as a transient overlay on the index.

The index describes the branch tip. A PR that ADDS a function has no row for
it: `code.impact` cannot seed from it, `code.callers` cannot find it, and the
call sites the PR itself introduces are edges nobody wrote. This module
parses the PR's post-image files with the same
tree-sitter extractors the ingest runs, classifies each function as `added`
(no indexed row of that qualified name in the changed paths), `modified` (a
new-side hunk overlaps its span) or `unchanged`, and binds the files' call
sites against the index's name table WITH the PR's own definitions added —
so an added function is a seed and a caller inside the PR is an edge.

The post-image comes from one of two places. A caller holding the PR's
checkout sends it (`pr_files` / `pr_diff`, set for the request by
`reads_post_image`): then no git is read, so any Loom on the service answers,
one with no mirror (yet) included. Otherwise it is `refs/pull/N/head` on
the bare mirror, diffed against its merge-base with the entry's branch.

Nothing is written. The read paths merge the overlay's rows over the index's
at request time and say so (`overlay: true`); `epistemic` is untouched — an
overlay edge is a parser-tier edge like any other, and a call site the PR
deletes is not removed from the index's rows (disclosed here, not hidden).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from server.code.app.ttl_cache import TtlCache
from server.settings import settings

logger = logging.getLogger("loom.code_index.pr_overlay")

OVERLAY_TIER = "overlay"
MAX_FILES = 50
MAX_FILE_BYTES = 1_000_000
MAX_DIFF_BYTES = 1_500_000
MAX_NAMES = 8000
NAME_CHUNK = 2000
_CACHE_MAX = 16
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

HEAD_NOT_IN_MIRROR = "head_not_in_mirror"
NO_REPO = "no_repo_entry"
NO_MERGE_BASE = "no_merge_base"
# A Loom with no git mirror (still cloning, or LOOM_GIT_HISTORY=none) and no post-image
# in the request.
NO_GIT_MIRROR = "no_git_mirror"
# `overlay_head_sha` of a post-image sent without its head commit.
POSTED_PREFIX = "posted:"


@dataclass(frozen=True)
class OverlaySymbol:
    symbol_id: str
    qualified_name: str
    name: str
    kind: str
    signature: str
    path: str
    start_line: int
    end_line: int
    status: str  # added | modified | unchanged

    def row(self) -> dict[str, Any]:
        """The shape `CodeRepo.functions_for_paths` returns, tier `overlay`."""
        return {
            "symbol_id": self.symbol_id,
            "qualified_name": self.qualified_name,
            "name": self.name,
            "signature": self.signature,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "tier": OVERLAY_TIER,
            "kind": self.kind,
            "overlay_status": self.status,
        }


@dataclass(frozen=True)
class OverlayEdge:
    edge_kind: str
    from_symbol_id: str
    to_symbol_id: str
    from_qualified_name: str
    to_qualified_name: str
    path: str
    line: int
    confidence: float

    def row(self) -> dict[str, Any]:
        """The shape `CodeRepo.edges_for_symbols` returns, tier `overlay`."""
        return {
            "tier": OVERLAY_TIER,
            "edge_kind": self.edge_kind,
            "to_symbol_id": self.to_symbol_id,
            "from_symbol_id": self.from_symbol_id,
            "path": self.path,
            "line": self.line,
            "from_qualified_name": self.from_qualified_name,
            "to_qualified_name": self.to_qualified_name,
            "confidence": self.confidence,
            "is_live": 1,
        }


@dataclass
class PrOverlay:
    pr_number: int
    head_sha: str = ""
    base_sha: str = ""
    changed_paths: list[str] = field(default_factory=list)
    diff: str = ""
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    symbols: list[OverlaySymbol] = field(default_factory=list)
    edges: list[OverlayEdge] = field(default_factory=list)
    reason: str = ""  # '' when the overlay is live

    @property
    def ok(self) -> bool:
        return not self.reason and bool(self.head_sha)

    @property
    def added(self) -> list[OverlaySymbol]:
        return [s for s in self.symbols if s.status == "added"]

    def symbols_named(self, name: str) -> list[OverlaySymbol]:
        """Definitions the PR's post-image holds under `name`: the exact
        qualified name, a `::`-suffix of it, or the bare name."""
        name = name.strip()
        if not name:
            return []
        out = [s for s in self.symbols if s.qualified_name == name]
        if not out:
            out = [s for s in self.symbols if s.qualified_name.endswith("::" + name)]
        if not out and "::" not in name:
            out = [s for s in self.symbols if s.name == name]
        return out

    def edge_rows(self, ids: list[str], *, direction: str) -> list[dict[str, Any]]:
        """The overlay's edges whose near end is one of `ids` — `callers`
        matches `to_symbol_id`, `callees` matches `from_symbol_id`."""
        wanted = set(ids)
        key = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        return [e.row() for e in self.edges if getattr(e, key) in wanted]

    def disclosure(self) -> dict[str, Any]:
        return {
            "overlay": self.ok,
            "overlay_head_sha": self.head_sha if self.ok else "",
            "overlay_added": len(self.added) if self.ok else 0,
            "overlay_note": self.reason,
        }


@dataclass(frozen=True)
class PostImage:
    """An open PR's post-image as its caller sent it: changed files' text at the
    head, and the PR's diff. Lives for one request."""

    pr_number: int
    files: dict[str, str]
    diff: str
    head_sha: str = ""
    base_sha: str = ""

    def digest(self) -> str:
        h = hashlib.sha256()
        for path in sorted(self.files):
            h.update(path.encode() + b"\0" + self.files[path].encode() + b"\0")
        h.update(self.diff.encode())
        return h.hexdigest()


_POSTED: ContextVar[PostImage | None] = ContextVar("loom_pr_post_image", default=None)


def posted(pr_number: int) -> PostImage | None:
    """The post-image this request sent for `pr_number`, if any."""
    img = _POSTED.get()
    return img if img is not None and img.pr_number == int(pr_number) else None


def post_image_of(inp: Any) -> PostImage | None:
    """The post-image an op input carries (`pr_files` / `pr_diff`), or None."""
    files = dict(getattr(inp, "pr_files", None) or {})
    diff = str(getattr(inp, "pr_diff", "") or "")
    if not (files or diff):
        return None
    return PostImage(
        pr_number=int(getattr(inp, "pr_number", 0) or 0),
        files=files,
        diff=diff,
        head_sha=str(getattr(inp, "pr_head_sha", "") or "").strip().lower(),
        base_sha=str(getattr(inp, "pr_base_sha", "") or "").strip().lower(),
    )


def reads_post_image[H: Callable[..., Awaitable[Any]]](handler: H) -> H:
    """Make the input's post-image the request's for the handler's run, so every
    overlay read inside it (the brief's nested impact read too) parses what the
    caller sent. An input without one leaves an outer request's in place."""

    @functools.wraps(handler)
    async def run(inp: Any, *args: Any, **kwargs: Any) -> Any:
        img = post_image_of(inp)
        if img is None:
            return await handler(inp, *args, **kwargs)
        token = _POSTED.set(img)
        try:
            return await handler(inp, *args, **kwargs)
        finally:
            _POSTED.reset(token)

    return run  # type: ignore[return-value]


def diff_paths(diff: str) -> list[str]:
    """Every path a unified diff names (`diff --git a/x b/y`, both sides), in
    order: a deleted file is named although it has no post-image."""
    out: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        for part in line[len("diff --git ") :].split(" "):
            p = part[2:] if part.startswith(("a/", "b/")) else part
            if p and p != "/dev/null" and p not in out:
                out.append(p)
    return out


# ---- pure pieces ---------------------------------------------------------------


def new_side_hunks(diff: str) -> dict[str, list[tuple[int, int]]]:
    """path → NEW-side line ranges from `+++ b/…` headers and `@@` hunks
    (a `-U0` diff makes the ranges exactly the changed lines)."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            p = p[2:] if p.startswith("b/") else p
            path = "" if p == "/dev/null" else p
            if path:
                ranges.setdefault(path, [])
        elif path and line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or 1)
                ranges[path].append((start, max(start, start + count - 1)))
    return ranges


def cut_diff(diff: str, max_bytes: int) -> tuple[str, bool]:
    """Cut at a file boundary when longer than `max_bytes` (the conventions diff's
    rule)."""
    if len(diff) <= max_bytes:
        return diff, False
    cut = diff.rfind("\ndiff --git ", 0, max_bytes)
    return (diff[:cut] if cut > 0 else diff[:max_bytes]), True


def classify_symbols(
    pf_symbols: list[Any],
    indexed: set[str],
    hunks: list[tuple[int, int]],
) -> list[OverlaySymbol]:
    """The post-image functions of one file against the qualified names the
    index defines anywhere (`indexed`, from the name-keyed side table — a
    point read, so a hub file cannot starve the answer): `added` when the
    index holds no definition of the qualified name, `modified` when a
    new-side hunk overlaps the span, else `unchanged`. A second definition
    of a name the index already has is not `added`: the index's row is the
    seed the walk continues from."""
    from server.code.domain.extract.treesitter import symbol_id

    out: list[OverlaySymbol] = []
    for s in pf_symbols:
        if s.kind not in ("function", "method") or not getattr(s, "has_body", True):
            continue
        if s.qualified_name not in indexed:
            status = "added"
        elif any(a <= s.end_line and b >= s.start_line for a, b in hunks):
            status = "modified"
        else:
            status = "unchanged"
        out.append(
            OverlaySymbol(
                symbol_id=symbol_id(s.qualified_name, s.signature),
                qualified_name=s.qualified_name,
                name=s.name,
                kind=s.kind,
                signature=s.signature,
                path=s.path,
                start_line=int(s.start_line),
                end_line=int(s.end_line),
                status=status,
            )
        )
    return out


def merge_name_table(
    master: dict[str, list[tuple[str, str, str]]],
    symbols: list[OverlaySymbol],
) -> dict[str, list[tuple[str, str, str]]]:
    """The index's name table plus the PR's definitions the index lacks — a
    qualified name the table already holds keeps the index's id, so a call
    to an existing function binds to the row the walk can continue from."""
    table = {k: list(v) for k, v in master.items()}
    for s in symbols:
        cands = table.setdefault(s.name, [])
        if not any(c[1] == s.qualified_name for c in cands):
            cands.append((s.symbol_id, s.qualified_name, s.kind))
    return table


def merge_edge_rows(
    master_rows: list[dict[str, Any]],
    overlay_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Index rows first, then the overlay's rows whose (from, to, path) the
    index does not already carry — a call site both saw counts once, the
    index's tier winning; a site the PR adds counts once, as `overlay`."""
    seen = {
        (str(r["from_qualified_name"]), str(r["to_qualified_name"]), str(r["path"]))
        for r in master_rows
    }
    out = list(master_rows)
    for r in overlay_rows:
        key = (str(r["from_qualified_name"]), str(r["to_qualified_name"]), str(r["path"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def parsable_paths(
    paths: list[str],
    prefixes: tuple[str, ...],
    hunks: dict[str, list[tuple[int, int]]],
) -> list[str]:
    """The changed paths worth parsing: post-image files (a path with a
    `+++` header — a deletion has none) under the indexed prefixes."""
    present = set(hunks)
    out = [p for p in paths if p in present and (not prefixes or p.startswith(prefixes))]
    return out[:MAX_FILES]


# ---- the load ------------------------------------------------------------------

# (mirror, PR) -> (head sha, overlay): a pushed head misses at once. A posted
# post-image keys on ("posted:<org>/<ns>", PR) with its digest as the head.
_CACHE: TtlCache[tuple[str, int], tuple[str, PrOverlay]] = TtlCache(
    lambda: settings.code_pr_overlay_cache_ttl_s, maxsize=_CACHE_MAX
)


def reset_cache_for_tests() -> None:
    _CACHE.clear()
    _FILE_FACTS.clear()
    _FILE_EDGES.clear()
    _NAMES.clear()


async def load_pr_overlay(
    repo: Any,
    org: str,
    ns: str,
    *,
    git_dir: str,
    pr_number: int,
    branch: str,
    path_prefixes: tuple[str, ...] = (),
    diff_max_bytes: int = MAX_DIFF_BYTES,
) -> PrOverlay:
    """The overlay of one PR from the mirror. Never raises: a head the
    mirror lacks, or a range with no merge-base, returns an overlay whose
    `reason` says so and whose `ok` is False. Cached 60 s per (mirror, PR)
    keyed on the head sha, so the brief's impact read parses once."""
    from server.code.app.index import gitrepo

    ov = PrOverlay(pr_number=int(pr_number))
    if not git_dir:
        ov.reason = NO_REPO
        return ov
    head = await gitrepo.pull_head_sha(git_dir, pr_number)
    if not head:
        ov.reason = HEAD_NOT_IN_MIRROR
        return ov
    key = (git_dir, int(pr_number))
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == head:
        return hit[1]
    ov.head_sha = head
    base = await gitrepo.merge_base(git_dir, head, f"refs/heads/{branch}")
    if not base:
        ov.reason = NO_MERGE_BASE
        return ov
    ov.base_sha = base
    try:
        raw = await gitrepo.run(git_dir, "diff", "--no-renames", "-U0", base, head)
    except gitrepo.GitError as e:
        logger.info("pr_overlay: diff of PR %s failed: %s", pr_number, e)
        raw = ""
    ov.diff, _ = cut_diff(raw, diff_max_bytes)
    ov.hunks = new_side_hunks(ov.diff)
    try:
        ov.changed_paths = await gitrepo.diff_name_only(git_dir, base, head)
    except gitrepo.GitError:
        ov.changed_paths = list(ov.hunks)

    async def from_mirror(path: str) -> bytes | None:
        try:
            return await gitrepo.show_file(git_dir, head, path, max_bytes=MAX_FILE_BYTES)
        except gitrepo.BlobTooLarge:
            return None  # not parsed either way

    await _parse_post_image(repo, org, ns, ov, read=from_mirror, path_prefixes=path_prefixes)
    _CACHE.put(key, (head, ov))
    return ov


async def load_posted_overlay(
    repo: Any,
    org: str,
    ns: str,
    img: PostImage,
    *,
    path_prefixes: tuple[str, ...] = (),
    diff_max_bytes: int = MAX_DIFF_BYTES,
) -> PrOverlay:
    """The overlay of one PR from the post-image its caller sent: no git read.
    The sent files are the post-image; the diff's hunks classify `modified` and
    its paths add the deletions. Cached 60 s on the content's digest, so the
    calls of one review parse once. Never raises."""
    digest = img.digest()
    key = (f"{POSTED_PREFIX}{org}/{ns}", int(img.pr_number))
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == digest:
        return hit[1]
    ov = PrOverlay(
        pr_number=int(img.pr_number),
        head_sha=img.head_sha or f"{POSTED_PREFIX}{digest[:12]}",
        base_sha=img.base_sha,
    )
    ov.diff, _ = cut_diff(img.diff, diff_max_bytes)
    ov.hunks = new_side_hunks(ov.diff)
    for path in img.files:
        ov.hunks.setdefault(path, [])  # a sent file is a post-image, in the diff or not
    ov.changed_paths = list(dict.fromkeys([*img.files, *diff_paths(ov.diff)]))

    async def from_request(path: str) -> bytes | None:
        text = img.files.get(path)
        return None if text is None else text.encode()

    await _parse_post_image(repo, org, ns, ov, read=from_request, path_prefixes=path_prefixes)
    _CACHE.put(key, (digest, ov))
    return ov


# Per file, so a push that changes one file of a PR re-parses that file alone, for
# `code_pr_overlay_file_ttl_s`. Only small results are kept (a parsed file holds its
# tree and source).
# (path, content digest) -> (the file's symbols, the names it defines or calls).
_FILE_FACTS: TtlCache[tuple[str, str], tuple[tuple[Any, ...], frozenset[str]]] = TtlCache(
    lambda: settings.code_pr_overlay_file_ttl_s, maxsize=512
)
# (path, content digest, digest of the name-table rows its names bind to) -> edges.
_FILE_EDGES: TtlCache[tuple[str, str, str], tuple[OverlayEdge, ...]] = TtlCache(
    lambda: settings.code_pr_overlay_file_ttl_s, maxsize=512
)
# (org, ns, index head, name) -> the index's definitions of that name.
_NAMES: TtlCache[tuple[str, str, str, str], tuple[tuple[str, str, str], ...]] = TtlCache(
    lambda: settings.code_pr_overlay_file_ttl_s, maxsize=200_000
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _binding_digest(names: frozenset[str], table: dict[str, list[tuple[str, str, str]]]) -> str:
    """What a file's edges depend on besides its text: the rows its names bind to
    (the index's and the PR's own definitions)."""
    h = hashlib.sha256()
    for n in sorted(names):
        for c in sorted(table.get(n) or ()):
            h.update("\0".join((n, *c)).encode() + b"\n")
    return h.hexdigest()


async def _file_facts(
    path: str, source: bytes, digest: str
) -> tuple[Any, tuple[Any, ...], frozenset[str]]:
    """(parsed file or None when cached, symbols, names) of one post-image file."""
    hit = _FILE_FACTS.get((path, digest))
    if hit is not None:
        return None, hit[0], hit[1]
    # parsing is CPU work: off the event loop
    pf, facts = await asyncio.to_thread(_parsed_facts, path, source)
    _FILE_FACTS.put((path, digest), facts)
    return pf, facts[0], facts[1]


def _parsed_facts(path: str, source: bytes) -> tuple[Any, tuple[tuple[Any, ...], frozenset[str]]]:
    from server.code.app.index.ingest_parse import parse_file
    from server.code.domain.extract.edges_treesitter import collect_call_names

    pf = parse_file(path, source)
    if pf is None or not pf.symbols:
        return None, ((), frozenset())
    names = {s.name for s in pf.symbols if s.kind in ("function", "method")}
    try:
        names |= collect_call_names(pf)
    except Exception as e:  # noqa: BLE001 — a file whose calls cannot be listed binds nothing
        logger.debug("pr_overlay: call names of %s failed: %s", path, e)
    return pf, (tuple(pf.symbols), frozenset(names))


def _file_edges(
    pf: Any, path: str, source: bytes, table: dict[str, list[tuple[str, str, str]]]
) -> tuple[OverlayEdge, ...]:
    """The calls and inherits edges of one post-image file; `pf` is its parse when this
    request already holds it."""
    from server.code.app.index.ingest_parse import parse_file
    from server.code.domain.extract.edges_treesitter import extract_edges_and_misses

    pf = pf or parse_file(path, source)
    try:
        edges, _misses = extract_edges_and_misses(pf, table, None)
    except Exception as e:  # noqa: BLE001 — per file, never the overlay
        logger.debug("pr_overlay: edges of %s failed: %s", pf.path, e)
        return ()
    return tuple(
        OverlayEdge(
            edge_kind=ed.edge_kind,
            from_symbol_id=ed.from_symbol_id,
            to_symbol_id=ed.to_symbol_id,
            from_qualified_name=ed.from_qualified_name,
            to_qualified_name=ed.to_qualified_name,
            path=ed.path,
            line=int(ed.line),
            confidence=float(ed.confidence),
        )
        for ed in edges
        if ed.edge_kind in ("calls", "inherits")
    )


async def _parse_post_image(
    repo: Any,
    org: str,
    ns: str,
    ov: PrOverlay,
    *,
    read: Callable[[str], Awaitable[bytes | None]],
    path_prefixes: tuple[str, ...],
) -> None:
    paths = parsable_paths(ov.changed_paths, path_prefixes, ov.hunks)
    if not paths:
        return
    # path -> (source, digest, parsed file this request already holds, symbols, names)
    files: dict[str, tuple[bytes, str, Any, tuple[Any, ...], frozenset[str]]] = {}
    names: set[str] = set()
    for p in paths:
        source = await read(p)
        if source is None or len(source) > MAX_FILE_BYTES:
            continue
        digest = _digest(source)
        pf, symbols, file_names = await _file_facts(p, source, digest)
        if not symbols:
            continue
        files[p] = (source, digest, pf, symbols, file_names)
        names |= file_names
    if not files:
        return
    # one name-keyed read serves both questions: which post-image functions
    # the index already defines (classification) and what the files' call
    # sites may bind to (edges)
    master_table = await _name_table(repo, org, ns, sorted(names), pr_number=ov.pr_number)
    indexed = {c[1] for cands in master_table.values() for c in cands}
    for p, (_, _, _, symbols, _) in files.items():
        ov.symbols.extend(classify_symbols(list(symbols), indexed, ov.hunks.get(p, [])))
    table = merge_name_table(master_table, ov.symbols)
    for p, (source, digest, pf, _, file_names) in files.items():
        key = (p, digest, _binding_digest(file_names, table))
        edges = _FILE_EDGES.get(key)
        if edges is None:
            # parsing and binding are CPU work: off the event loop
            edges = await asyncio.to_thread(_file_edges, pf, p, source, table)
            _FILE_EDGES.put(key, edges)
        ov.edges.extend(edges)


async def _name_table(
    repo: Any,
    org: str,
    ns: str,
    names: list[str],
    *,
    pr_number: int,
) -> dict[str, list[tuple[str, str, str]]]:
    """`CodeRepo.name_candidates` over `names` in chunks (bare name →
    [(symbol_id, qualified_name, kind)], repo-wide, both tiers); a failed
    chunk contributes nothing — the PR's own definitions still bind. Each
    name's answer is kept per index head, so a later review of the same PR
    reads only the names it has not asked about at this head."""
    names = names[:MAX_NAMES]
    head = await _index_head(repo, org, ns)
    table: dict[str, list[tuple[str, str, str]]] = {}
    missing: list[str] = []
    for n in names:
        hit = _NAMES.get((org, ns, head, n)) if head else None
        if hit is None:
            missing.append(n)
        elif hit:
            table[n] = list(hit)
    for i in range(0, len(missing), NAME_CHUNK):
        chunk = missing[i : i + NAME_CHUNK]
        try:
            got = await repo.name_candidates(org, ns, chunk)
        except Exception as e:  # noqa: BLE001 — a failed chunk adds nothing; the PR's own names still bind
            logger.info("pr_overlay: name table read failed for PR %s: %s", pr_number, e)
            continue
        for n in chunk:
            rows = list(got.get(n) or [])
            if rows:
                table.setdefault(n, []).extend(rows)
            if head:
                _NAMES.put((org, ns, head, n), tuple(rows))
    return table


async def _index_head(repo: Any, org: str, ns: str) -> str:
    """The indexed commit the name answers are kept under; '' (no keeping) when
    it does not read."""
    try:
        return str(await repo.get_state(org, ns, "git_head") or "")
    except Exception:  # noqa: BLE001 — unknown head: read the names, keep nothing
        return ""


async def overlay_for_namespace(repo: Any, org: str, ns: str, pr_number: int) -> PrOverlay:
    """The overlay through the namespace's repo entry (mirror, branch,
    indexed prefixes): the request's posted post-image when it sent one, else
    the mirror's. `no_repo_entry` when the namespace has none, `no_git_mirror: …`
    (`mirrors.history_dir`) on a host without the mirror the caller sent nothing to."""
    from server.code.domain.index.repos import repo_for_namespace

    cfg = repo_for_namespace(org, ns)
    if cfg is None:
        return PrOverlay(pr_number=int(pr_number), reason=NO_REPO)
    img = posted(pr_number)
    if img is not None:
        return await load_posted_overlay(repo, org, ns, img, path_prefixes=tuple(cfg.path_prefixes))
    from server.code.app.index.mirrors import history_dir

    git_dir, gap = await history_dir(cfg)
    if gap:
        return PrOverlay(pr_number=int(pr_number), reason=gap)
    return await load_pr_overlay(
        repo,
        org,
        ns,
        git_dir=git_dir,
        pr_number=pr_number,
        branch=cfg.git_branch,
        path_prefixes=tuple(cfg.path_prefixes),
    )


__all__ = [
    "HEAD_NOT_IN_MIRROR",
    "NO_GIT_MIRROR",
    "NO_MERGE_BASE",
    "NO_REPO",
    "OVERLAY_TIER",
    "POSTED_PREFIX",
    "OverlayEdge",
    "OverlaySymbol",
    "PostImage",
    "PrOverlay",
    "classify_symbols",
    "cut_diff",
    "diff_paths",
    "load_posted_overlay",
    "load_pr_overlay",
    "merge_edge_rows",
    "merge_name_table",
    "new_side_hunks",
    "overlay_for_namespace",
    "parsable_paths",
    "post_image_of",
    "posted",
    "reads_post_image",
    "reset_cache_for_tests",
]
