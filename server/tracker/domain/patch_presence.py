"""Hunk-level patch presence for `code.backport_gap(exact: true)`: is the FIX's
change — not merely the function it touched — in a release branch's indexed
chunk bodies?

The backport gap's symbol-existence verdict (`gap` / `not_affected`) is a
floor. A patch-presence test decides at the level of the patch's added and
removed lines in normalised function bodies. This module is the pure half: parse the fix's unified
diff into hunk groups keyed by (path, enclosing declaration), normalise
lines, compute the line ratios against the branch's chunks, and name one
of two modes per branch:

- `exact`       — the fix is NOT there (`absent`): ≤ 20% of the informative
                  added lines are present while the removed lines (the
                  pre-image) still are, or the pre-image
                  is intact (≥ 80% of two or more removed lines) with at most
                  half of the added lines matching; or the fix IS there
                  (`present`): ≥ 80% of the added lines present and ≥ 80% of
                  two or more removed lines gone. A pure addition is never
                  `present` by lines — generic lines match by chance;
- `symbol_only` — the lines do not decide (the function carries neither
                  image, the path is not on the branch, too few lines);
                  the symbol-existence verdict stands.

A bag-of-tokens overlap (θ = 0.7) over the fix's NEW tokens (added − removed − the hunks'
context − structural keywords, a multiset) against the enclosing function's chunk is
computed and DISCLOSED (`new_token_overlap`, `dropped_token_overlap`) but decides nothing:
similar API usage makes it confirm refactored backports that are not there, and a hunk is
below the fragment size such overlap is precise at.

Lines are whitespace-normalised (stripped, runs collapsed); a line with
fewer than 8 characters or no identifier (`}`, `else`, `return;`) and a
comment line are not evidence either way, and an exact claim needs two such
lines. Removed lines that were re-added verbatim (a move) do not count as
removed. Hunks in tests, docs, CI and utilities are ignored: a test file is
on every branch its suite runs on.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from server.tracker.domain.backport import TEST_PREFIXES, decl_name

EXACT = "exact"
SYMBOL_ONLY = "symbol_only"
PRESENT = "present"  # the fix's state is on the branch
ABSENT = "absent"  # the fix's state is not; the pre-image is

ADDED_PRESENT_MIN = 0.8  # covered/exact: added lines present
REMOVED_GONE_MIN = 0.8  # covered/exact: removed lines gone
ADDED_ABSENT_MAX = 0.2  # gap/exact: added lines absent …
REMOVED_PRESENT_MIN = 0.5  # … while the pre-image is still there
TOKEN_OVERLAP_MIN = 0.7  # token-bag clone θ — disclosed, never a verdict (see above)
MIN_NEW_TOKENS = 4  # fewer new tokens carry no similarity signal
MIN_LINE_CHARS = 8
MIN_LINES = 2  # informative lines an exact claim needs (a one-line change is two)
MAX_SECTIONS = 40
MAX_LINES_PER_KIND = 400
NEEDLE_LIMIT = 100  # per kind, for the chunk read's multiSearchAny (≤ 200 in all)
MIN_NEEDLE_CHARS = 8
# The branch's chunk read (`CodeRepo.chunks_for_patch`) returns at most this
# many chunks over at most this many paths. A read at either cap may have
# left out chunks that hold the fix's lines, and a line not read looks gone
# (or absent) exactly like one the branch lacks: no verdict on such a read.
CHUNK_READ_LIMIT = 400
PATH_READ_LIMIT = 40

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")
_WS_RE = re.compile(r"\s+")
# a comment line is prose, not code, so comments are normalised away; a
# `#include` / `#define` (no space after the hash) is code
_COMMENT_RE = re.compile(r"^(//|/\*|\*/|\* |\*$|# |#$|--)")
_TEST_SEGMENT_RE = re.compile(r"(^|/)tests?/")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|\"(?:[^\"\\]|\\.)*\"")
_TRIVIAL = frozenset(
    {
        "{",
        "}",
        "};",
        "});",
        "else",
        "break;",
        "continue;",
        "return;",
        "try",
        "do",
        "end",
        "pass",
        "endif",
        "#endif",
        "#else",
        "public:",
        "private:",
        "protected:",
    }
)
_SKIP_RE = "^a/(?:docs|utils|ci)/|^a/(?:[^ \\n]*/)?tests?/"
# structural words of C++ / Python that every function carries: not evidence
# that THIS fix's tokens arrived (true / false / nullptr stay — a flipped flag
# is often the fix)
_KEYWORDS = frozenset(
    """
auto if else for while do return const static void int bool size_t case break continue switch
new delete this class struct template typename using namespace public private protected virtual
override final try catch throw noexcept explicit inline constexpr operator sizeof
def self None and or not in is import from pass with as elif lambda yield raise except finally
""".split()
)


# ---- the fix's hunks ---------------------------------------------------------------


@dataclass
class HunkGroup:
    """Every hunk of one file under one enclosing declaration ('' when the
    header names none), as normalised lines."""

    path: str
    symbol: str  # `IDataType::getSubcolumnInfo` or ''
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)  # unchanged lines: on both images
    hunks: int = 0

    def informative(self) -> tuple[list[str], list[str]]:
        """(added, removed) informative lines, a moved line counted on neither side."""
        added = [x for x in self.added if is_informative(x)]
        removed = [x for x in self.removed if is_informative(x)]
        moved = set(added) & set(removed)
        return [x for x in added if x not in moved], [x for x in removed if x not in moved]


def skip_pattern() -> str:
    """The RE2 pattern the tracker read filters diff sections with: a section
    starts `a/<path> b/<path>`; docs, CI and utilities at the root and a
    `tests/` (or `test/`) directory anywhere never count."""
    return _SKIP_RE


def normalise(line: str) -> str:
    return _WS_RE.sub(" ", line.strip())


def is_informative(norm: str) -> bool:
    if len(norm) < MIN_LINE_CHARS or norm in _TRIVIAL or _COMMENT_RE.match(norm):
        return False
    return any(c.isalnum() for c in norm)


def is_test_path(path: str) -> bool:
    """Tests, docs, CI and utilities never count: a test file is on every
    branch its suite runs on, wherever the `tests/` directory sits."""
    return path.startswith(TEST_PREFIXES) or bool(_TEST_SEGMENT_RE.search(path))


def _section_path(header: str, lines: list[str]) -> str:
    """The post-image path: `+++ b/<path>` when present, else the header's
    `b/` side; '' for a deleted file (nothing to be present)."""
    for ln in lines[:6]:
        if ln.startswith("+++ "):
            target = ln[4:].strip()
            if target == "/dev/null":
                return ""
            return target[2:] if target.startswith("b/") else target
    parts = header.split(" b/", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def parse_sections(sections: list[str]) -> list[HunkGroup]:
    """Diff sections (`splitByString('\\ndiff --git ', …)` upstream: each
    starts `a/<path> b/<path>\\n`) → hunk groups by (path, declaration),
    source files only, in order of first appearance."""
    groups: dict[tuple[str, str], HunkGroup] = {}
    for section in sections[:MAX_SECTIONS]:
        lines = section.split("\n")
        if not lines:
            continue
        path = _section_path(lines[0], lines[1:])
        if not path or is_test_path(path):
            continue
        _parse_hunks(path, lines[1:], groups)
    return list(groups.values())


def _parse_hunks(path: str, lines: list[str], groups: dict[tuple[str, str], HunkGroup]) -> None:
    current: HunkGroup | None = None
    for ln in lines:
        m = _HUNK_RE.match(ln)
        if m:
            symbol = decl_name(m.group(1) or "")
            current = groups.setdefault((path, symbol), HunkGroup(path=path, symbol=symbol))
            current.hunks += 1
            continue
        if current is None or not ln:
            continue
        mark, body = ln[0], ln[1:]
        if mark == "+" and len(current.added) < MAX_LINES_PER_KIND:
            current.added.append(normalise(body))
        elif mark == "-" and len(current.removed) < MAX_LINES_PER_KIND:
            current.removed.append(normalise(body))
        elif mark == " " and len(current.context) < MAX_LINES_PER_KIND:
            current.context.append(normalise(body))


def needles(groups: list[HunkGroup]) -> list[str]:
    """Substrings the chunk read can `multiSearchAny` for: from the longest
    informative added and removed lines (≤ NEEDLE_LIMIT per kind), each
    line's longest whitespace-free run of ≥ MIN_NEEDLE_CHARS characters —
    the stored content keeps its own whitespace, a run between whitespace
    does not."""
    out: list[str] = []
    for kind in ("added", "removed"):
        cands: list[str] = []
        for g in groups:
            cands.extend(getattr(g, kind))
        cands = sorted({c for c in cands if is_informative(c)}, key=len, reverse=True)
        for c in cands[:NEEDLE_LIMIT]:
            frag = _needle_of(c)
            if frag and frag not in out:
                out.append(frag)
    return out


def _needle_of(norm: str) -> str:
    """The longest whitespace-free run of the normalised line (whitespace
    runs were collapsed, so a run between spaces is byte-identical in the
    source), when it is long enough to be selective."""
    runs = sorted(norm.split(" "), key=len, reverse=True)
    return runs[0] if runs and len(runs[0]) >= MIN_NEEDLE_CHARS else ""


def symbols_of(groups: list[HunkGroup]) -> list[str]:
    return sorted({g.symbol for g in groups if g.symbol})


def paths_of(groups: list[HunkGroup]) -> list[str]:
    out: list[str] = []
    for g in groups:
        if g.path not in out:
            out.append(g.path)
    return out


# ---- the branch's chunks ------------------------------------------------------------


def _scope_matches(scope: str, symbol: str) -> bool:
    return bool(symbol) and (scope == symbol or scope.endswith("::" + symbol))


def _lines_of(chunks: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for c in chunks:
        for ln in str(c.get("content") or "").split("\n"):
            n = normalise(ln)
            if n:
                out.add(n)
    return out


def tokens(text: str) -> Counter[str]:
    """The bag: identifiers, numbers and string literals as a multiset —
    a token-bag clone detector's unit, without operators (a refactor keeps the names)."""
    return Counter(_IDENT_RE.findall(text))


def containment(part: Counter[str], whole: Counter[str]) -> float:
    """Multiset containment |part ∩ whole| / |part|; 0 for an empty part."""
    total = sum(part.values())
    if not total:
        return 0.0
    return sum(min(n, whole.get(t, 0)) for t, n in part.items()) / total


# ---- the verdict ---------------------------------------------------------------------


@dataclass
class PatchPresence:
    mode: str = SYMBOL_ONLY
    verdict: str = ""  # present | absent | '' (undecided)
    added_total: int = 0
    added_present: int = 0
    removed_total: int = 0
    removed_gone: int = 0
    new_tokens: int = 0
    new_token_overlap: float = 0.0
    dropped_tokens: int = 0
    dropped_token_overlap: float = 0.0
    similar: bool = False  # what the token-bag θ would have said; disclosed only
    hunks: int = 0
    functions_matched: int = 0
    chunks_read: int = 0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode,
            "added_total": self.added_total,
            "added_present": self.added_present,
            "removed_total": self.removed_total,
            "removed_gone": self.removed_gone,
            "hunks": self.hunks,
            "functions_matched": self.functions_matched,
            "chunks_read": self.chunks_read,
        }
        if self.new_tokens or self.dropped_tokens:
            out["new_tokens"] = self.new_tokens
            out["new_token_overlap"] = round(self.new_token_overlap, 3)
            out["dropped_tokens"] = self.dropped_tokens
            out["dropped_token_overlap"] = round(self.dropped_token_overlap, 3)
            out["similar"] = self.similar
        if self.reason:
            out["reason"] = self.reason
        return out


def _tally(
    groups: list[HunkGroup], chunks: list[dict[str, Any]], pp: PatchPresence
) -> list[HunkGroup]:
    """Line counts over every group against every chunk read from its PATH —
    a hunk's header names the declaration the hunk starts in, and a hunk
    that runs past the function's end (a new function added after it) puts
    its lines in another chunk; returns the groups whose enclosing function
    the branch defines, for the token bag."""
    by_path: dict[str, list[dict[str, Any]]] = {}
    for c in chunks:
        by_path.setdefault(str(c.get("path") or ""), []).append(c)
    matched: list[HunkGroup] = []
    for g in groups:
        pp.hunks += g.hunks
        path_chunks = by_path.get(g.path, [])
        if any(_scope_matches(str(c.get("scope") or ""), g.symbol) for c in path_chunks):
            pp.functions_matched += 1
            matched.append(g)
        lines = _lines_of(path_chunks)
        added, removed = g.informative()
        pp.added_total += len(added)
        pp.added_present += sum(1 for x in added if x in lines)
        pp.removed_total += len(removed)
        pp.removed_gone += sum(1 for x in removed if x not in lines)
    return matched


def _token_bag(groups: list[HunkGroup], chunks: list[dict[str, Any]], pp: PatchPresence) -> None:
    """The token-bag overlap on the FRAGMENT, for disclosure: the fix's new
    tokens (added − removed − the hunks' context lines − structural keywords,
    a multiset) contained in the matched functions' chunks, and its dropped
    tokens (removed − added − context − keywords) likewise; `similar` is
    what θ = 0.7 on the new tokens would have said. Only over groups whose
    enclosing function the branch defines (a file-level bag says nothing)."""
    added: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    context: Counter[str] = Counter()
    body: Counter[str] = Counter()
    for g in groups:
        g_added, g_removed = g.informative()
        for ln in g_added:
            added.update(tokens(ln))
        for ln in g_removed:
            removed.update(tokens(ln))
        for ln in g.context:
            context.update(tokens(ln))
        for c in chunks:
            if str(c.get("path") or "") != g.path:
                continue
            if _scope_matches(str(c.get("scope") or ""), g.symbol):
                body.update(tokens(str(c.get("content") or "")))
    new, dropped = _signature(added - removed, context), _signature(removed - added, context)
    pp.new_tokens, pp.dropped_tokens = sum(new.values()), sum(dropped.values())
    pp.new_token_overlap = containment(new, body)
    pp.dropped_token_overlap = containment(dropped, body) if dropped else 0.0
    pp.similar = pp.new_tokens >= MIN_NEW_TOKENS and pp.new_token_overlap >= TOKEN_OVERLAP_MIN


def _signature(bag: Counter[str], context: Counter[str]) -> Counter[str]:
    rest = bag - context
    return Counter({t: n for t, n in rest.items() if t not in _KEYWORDS})


def decide(groups: list[HunkGroup], chunks: list[dict[str, Any]]) -> PatchPresence:
    """One branch's patch presence from the fix's hunk groups and the chunks
    read from that branch for the fix's paths (scope-matched functions and
    needle hits). Never a verdict when the lines and the bag both fail to
    decide, or when the read stopped at its cap — `symbol_only`, and the
    symbol-existence floor stands."""
    pp = PatchPresence(chunks_read=len(chunks))
    if not groups:
        pp.reason = "no_hunks"
        return pp
    if not chunks:
        pp.reason = "no_chunks"
        _tally(groups, [], pp)
        return pp
    matched = _tally(groups, chunks, pp)
    if pp.added_total + pp.removed_total < MIN_LINES:
        thin = bool(pp.added_total or pp.removed_total)
        pp.reason = "too_few_lines" if thin else "no_informative_lines"
        return pp
    if len(chunks) >= CHUNK_READ_LIMIT or len(paths_of(groups)) > PATH_READ_LIMIT:
        pp.reason = "read_truncated"
        return pp
    added_ratio = pp.added_present / pp.added_total if pp.added_total else None
    gone_ratio = pp.removed_gone / pp.removed_total if pp.removed_total else None
    if is_fix_present(added_ratio, gone_ratio, pp.removed_total):
        pp.mode, pp.verdict = EXACT, PRESENT
        return pp
    if is_fix_absent(added_ratio, gone_ratio, pp.removed_total):
        pp.mode, pp.verdict = EXACT, ABSENT
        return pp
    if matched:
        _token_bag(matched, chunks, pp)
    pp.reason = "undecided"
    return pp


def is_fix_present(added_ratio: float | None, gone_ratio: float | None, removed_total: int) -> bool:
    """The fix's state is on the branch: its added lines present and the
    pre-image gone — two or more removed lines, so a pure addition of lines
    that match by chance never counts."""
    if removed_total < 2 or gone_ratio is None or gone_ratio < REMOVED_GONE_MIN:
        return False
    return added_ratio is None or added_ratio >= ADDED_PRESENT_MIN


def is_fix_absent(added_ratio: float | None, gone_ratio: float | None, removed_total: int) -> bool:
    """The fix is not there: its added lines absent while the pre-image is
    still there (or the fix added lines only); or the pre-image intact (≥ 80% of two or more
    removed lines still present) with at most half of the added lines matching, whatever
    matched by chance. A branch whose function carries neither image was rewritten: no
    claim."""
    present = None if gone_ratio is None else 1.0 - gone_ratio
    if present is not None and removed_total >= 2 and present >= REMOVED_GONE_MIN:
        return added_ratio is None or added_ratio <= 0.5
    if added_ratio is None:  # a pure deletion still present
        return bool(present is not None and present >= REMOVED_PRESENT_MIN)
    if added_ratio > ADDED_ABSENT_MAX:
        return False
    return present is None or present >= REMOVED_PRESENT_MIN


def refine(v: Any, pp: PatchPresence, cherry_pick: dict[str, Any]) -> None:
    """Fold one branch's patch presence into its symbol-existence verdict (a
    `BranchVerdict`): the record's `covered` / `pending` and every `unknown`
    stand — the patch test refines only what presence floored (`gap`,
    `not_affected`). The fix `absent` with its pre-image there is `gap`,
    exact, basis `hunk`; the fix `present` where the record names no
    backport is `not_affected`, exact, basis `hunk` — the branch already
    holds the fixed state, whether it never carried the removed code (a
    fix that reverts a recent change) or a pick the record does not title
    as a backport; either way nothing is owed. The block is disclosed on
    every branch it was computed for."""
    from server.tracker.domain.backport import GAP, NOT_AFFECTED

    v.presence["patch"] = pp.as_dict()
    if v.verdict not in (GAP, NOT_AFFECTED) or not pp.verdict:
        return
    if pp.verdict == PRESENT:
        v.verdict, v.epistemic, v.basis = NOT_AFFECTED, "exact", "hunk"
        v.cherry_pick = {}
        v.evidence.append(
            f"the fix's state is already on {v.branch}: {pp.added_present}/{pp.added_total} added "
            f"lines present, {pp.removed_gone}/{pp.removed_total} removed lines gone; the record "
            "names no backport — the branch never carried the removed code, or the pick is not "
            "titled as a backport"
        )
    elif pp.verdict == ABSENT:
        v.verdict, v.epistemic, v.basis = GAP, "exact", "hunk"
        v.cherry_pick = dict(cherry_pick)
        v.evidence.append(
            f"the fix's lines are not on {v.branch}: {pp.added_present}/{pp.added_total} added "
            f"present, {pp.removed_total - pp.removed_gone}/{pp.removed_total} removed still there"
        )


__all__ = [
    "ABSENT",
    "EXACT",
    "PRESENT",
    "SYMBOL_ONLY",
    "HunkGroup",
    "PatchPresence",
    "containment",
    "decide",
    "is_test_path",
    "needles",
    "normalise",
    "parse_sections",
    "paths_of",
    "refine",
    "skip_pattern",
    "symbols_of",
    "tokens",
]
