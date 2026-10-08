"""The branch's own tree and history for the fix's lines: the leg that
splits hunk-level patch presence's `undecided` rows.

`patch_presence` decides from the branch's INDEXED chunk bodies — the chunks
the read fetched for the fix's paths (the enclosing functions and the needle
hits). A row comes out `symbol_only(undecided)` when the fetched chunks carry
neither image cleanly, either because the lines
live in chunks the read did not fetch, or because the branch never carried
the removed lines (the code was restructured on master after the cut, or the
buggy lines landed later). The mirror answers both, bounded:

- the TIP is the ground truth for presence: ONE `git grep -E` per (branch,
  path) with every informative added and removed line of that path as a
  pattern (whitespace runs `[[:space:]]+`, so the normalised line matches the
  source's own indentation); then `patch_presence`'s rules on what the tip holds —
  the added lines present and the removed lines gone is the fixed state
  (`not_affected`), the removed lines there and the added ones absent is the
  bug (`gap`), both `exact` with basis `history`;
- HISTORY for the pre-image the tip lacks: when no removed line is at the
  tip and the lines did not decide, `git log -S<line> --pickaxe-regex
  --first-parent -n 1 -- <path>` for the ≤ 3 longest removed lines — no
  commit → the branch NEVER carried it (`restructured`); a commit → it was
  there and a later commit REMOVED it (`removed_on_branch`); both
  `not_affected`, `lower_bound` (the lines are gone; the bug may live in the
  branch's older shape in other lines, so the floor stays).

The path filter is what makes `-S` fit the budget: a release branch's whole
first-parent history takes tens of seconds, one path well under a second. A
path absent at the tip counts every line of it absent (a file the branch does not have holds
neither image); a deadline or an error decides nothing. `presence.history`
discloses the line counts, the probed lines and their states.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from server.tracker.domain.patch_presence import (
    EXACT,
    SYMBOL_ONLY,
    HunkGroup,
    is_fix_absent,
    is_fix_present,
    is_informative,
)

logger = logging.getLogger("loom.tracker.patch_history")

HISTORY = "history"
PRESENT = "present"  # the removed line is at the branch's tip
NEVER = "never"  # the branch's first-parent history never carried it
REMOVED = "removed"  # it was there and a later commit took it out
PATH_ABSENT = "path_absent"
PICKED = "picked"  # the commit that brought the fix's added lines names the fix
ADDED = "added"  # a commit brought the line, and it does not name the fix
PROBE_LINES = 3  # `-S` reads per branch
LINES_PER_PATH = 40  # per kind, the longest informative lines: one grep pattern each
MIN_LINES = 2
LINE_TIMEOUT_S = 3.0
_REAP_TIMEOUT_S = 2.0  # waiting for a killed git to exit
ADDED_ABSENT_MAX = 0.2  # the fix's lines are not there …
REMOVED_REMNANT_MAX = 0.5  # … and at most half of the pre-image is: restructured, by history
PICKED_ADDED_MIN = 0.8  # the fix's lines are there: which commit brought them?
_ERE_SPECIAL = set("\\^$.|?*+()[]{}")


@dataclass
class LineHistory:
    path: str
    line: str
    state: str  # present | never | removed | picked | added | path_absent | deadline | error:<type>
    commit: str = ""
    subject: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "line": self.line[:120], "state": self.state}
        if self.commit:
            out["commit"] = self.commit
        if self.subject:
            out["subject"] = self.subject[:120]
        return out


@dataclass
class HistoryPresence:
    mode: str = SYMBOL_ONLY  # exact when the tip or the history decided
    verdict: str = ""  # gap | not_affected | ''
    epistemic: str = ""
    reason: str = ""
    added_total: int = 0
    added_present: int = 0
    removed_total: int = 0
    removed_present: int = 0
    paths: int = 0
    paths_absent: int = 0
    lines: list[LineHistory] = field(default_factory=list)  # the `-S` probes
    ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode,
            "added_total": self.added_total,
            "added_present": self.added_present,
            "removed_total": self.removed_total,
            "removed_present": self.removed_present,
            "paths": self.paths,
            "paths_absent": self.paths_absent,
            "probes": len(self.lines),
            "never": sum(1 for x in self.lines if x.state == NEVER),
            "removed": sum(1 for x in self.lines if x.state == REMOVED),
            "lines": [x.as_dict() for x in self.lines],
            "ms": self.ms,
        }
        if self.verdict:
            out["verdict"] = self.verdict
        if self.reason:
            out["reason"] = self.reason
        return out


# ---- what to test ----------------------------------------------------------------------


def path_lines(
    groups: list[HunkGroup],
    *,
    limit: int = LINES_PER_PATH,
) -> dict[str, tuple[list[str], list[str]]]:
    """path → (added, removed) informative lines, a moved line on neither
    side, the longest first, at most `limit` per kind — the grep patterns."""
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    for g in groups:
        if not g.path:
            continue
        a, r = g.informative()
        added.setdefault(g.path, []).extend(x for x in a if is_informative(x))
        removed.setdefault(g.path, []).extend(x for x in r if is_informative(x))
    out: dict[str, tuple[list[str], list[str]]] = {}
    for p in added:
        a = sorted(set(added[p]), key=lambda s: (-len(s), s))[:limit]
        r = sorted(set(removed[p]), key=lambda s: (-len(s), s))[:limit]
        if a or r:
            out[p] = (a, r)
    return out


def line_regex(norm: str) -> str:
    """The normalised line as a POSIX ERE: metacharacters escaped, every
    whitespace run `[[:space:]]+` (the source keeps its own indentation and
    alignment; the normalised line collapsed them)."""
    parts: list[str] = []
    for tok in norm.split(" "):
        if not tok:
            continue
        parts.append("".join(("\\" + c) if c in _ERE_SPECIAL else c for c in tok))
    return "[[:space:]]+".join(parts)


def _py_regex(norm: str) -> re.Pattern[str]:
    return re.compile(r"\s+".join(re.escape(t) for t in norm.split(" ") if t))


# ---- the mirror ------------------------------------------------------------------------


class MirrorReadError(RuntimeError):
    """A git read of the mirror failed; the leg reports it, never a verdict."""


async def _git_rc(git_dir: str, *args: str, timeout_s: float) -> tuple[int, str]:
    """(rc, stdout) of a read-only git command; a timeout kills it and is
    reported as rc -1 — the leg reads it as `deadline`, never a verdict."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        git_dir,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        return -1, ""
    finally:
        # a deadline or the caller cancelling (code_backport) must not leave a
        # `git log -S` walking the branch's history behind it
        if proc.returncode is None:
            proc.kill()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S))
    return int(proc.returncode or 0), out.decode("utf-8", "replace")


async def tip_presence(
    git_dir: str,
    branch: str,
    path: str,
    lines: list[str],
    *,
    timeout_s: float = LINE_TIMEOUT_S,
) -> dict[str, bool] | None:
    """line → at the branch's tip, from ONE `git grep` over the path with
    every line as a pattern; {} for a path the tip does not have (every line
    absent); None on a deadline. A git command that fails (exit status other
    than a match or a clean miss) raises `MirrorReadError`: an unread file is
    never a file whose lines are all absent."""
    if not lines:
        return {}
    # `ls-tree` answers a missing path with an empty listing and status 0, so
    # a failed read (status 128) is not mistaken for one; `cat-file -e` exits
    # 128 for both.
    rc, listed = await _git_rc(
        git_dir, "ls-tree", "--name-only", branch, "--", path, timeout_s=timeout_s
    )
    if rc == -1:
        return None
    if rc != 0:
        raise MirrorReadError(f"git ls-tree exited {rc} for {path}")
    if not listed.strip():
        return {}
    args = ["grep", "-h", "-I", "--no-color", "-E"]
    for ln in lines:
        args += ["-e", line_regex(ln)]
    rc, out = await _git_rc(git_dir, *args, branch, "--", path, timeout_s=timeout_s)
    if rc == -1:
        return None
    if rc not in (0, 1):  # 1: no line matched; anything else did not read the file
        raise MirrorReadError(f"git grep exited {rc} for {path}")
    hits = out.splitlines() if rc == 0 else []
    pats = {ln: _py_regex(ln) for ln in lines}
    return {ln: any(pat.search(h) for h in hits) for ln, pat in pats.items()}


async def _last_count_change(
    git_dir: str,
    branch: str,
    path: str,
    norm: str,
    *,
    timeout_s: float,
) -> tuple[int, str, str, str]:
    """(rc, sha, subject, body) of the newest first-parent commit of `branch`
    that changed how many times the line occurs in `path`; sha '' when none."""
    rc, out = await _git_rc(
        git_dir,
        "log",
        f"-S{line_regex(norm)}",
        "--pickaxe-regex",
        "--first-parent",
        "-n",
        "1",
        "--format=%H%x00%s%x00%b",
        branch,
        "--",
        path,
        timeout_s=timeout_s,
    )
    if rc != 0 or not out.strip():
        return rc, "", "", ""
    parts = out.strip("\n").split("\x00", 2)
    subject = parts[1] if len(parts) > 1 else ""
    body = parts[2] if len(parts) > 2 else ""
    return rc, parts[0].strip(), subject, body


async def line_history(
    git_dir: str,
    branch: str,
    path: str,
    norm: str,
    *,
    timeout_s: float = LINE_TIMEOUT_S,
) -> LineHistory:
    """One removed line the tip lacks: never in the branch's first-parent
    history for that path (`never`), or there once and removed since
    (`removed`, with the commit that changed its count)."""
    rc, sha, _, _ = await _last_count_change(git_dir, branch, path, norm, timeout_s=timeout_s)
    if rc == -1:
        return LineHistory(path, norm, "deadline")
    if rc != 0:
        return LineHistory(path, norm, "error:git")
    return LineHistory(path, norm, REMOVED if sha else NEVER, commit=sha[:12])


def names_fix(subject: str, body: str, *, pr_number: int, merge_sha: str) -> bool:
    """Whether a commit message names the fix: its PR number as `#N` (the
    automation's "Backport #N to X", a merge's "(#N)", a hand-written
    "cherry-pick of #N") or its merge commit ("cherry picked from commit …")."""
    text = f"{subject}\n{body}"
    if pr_number and re.search(rf"(?<![\w])#{int(pr_number)}(?!\d)", text):
        return True
    sha = (merge_sha or "").strip().lower()
    return bool(sha and len(sha) >= 7 and sha[:7] in text.lower())


async def line_origin(
    git_dir: str,
    branch: str,
    path: str,
    norm: str,
    *,
    pr_number: int,
    merge_sha: str,
    timeout_s: float = LINE_TIMEOUT_S,
) -> LineHistory:
    """One added line the tip HAS: the commit that brought it, and whether
    that commit names the fix (`picked`) or not (`added`: the line may be
    generic, or the pick was squashed into something unnamed)."""
    rc, sha, subject, body = await _last_count_change(
        git_dir,
        branch,
        path,
        norm,
        timeout_s=timeout_s,
    )
    if rc == -1:
        return LineHistory(path, norm, "deadline")
    if rc != 0 or not sha:
        return LineHistory(path, norm, "error:git" if rc != 0 else NEVER)
    state = PICKED if names_fix(subject, body, pr_number=pr_number, merge_sha=merge_sha) else ADDED
    return LineHistory(path, norm, state, commit=sha[:12], subject=subject.strip())


async def _unreadable_ref(git_dir: str, branch: str, *, timeout_s: float) -> str:
    """Why `branch` has no tip to read ("no_branch_ref", "deadline"), or "" when it
    does. A path read against a ref the mirror lacks, or a mirror that is not
    there, fails like any other read, so only a resolved tip may read as
    `path_absent`."""
    if not branch or not git_dir:
        return "no_branch_ref"
    rc, _ = await _git_rc(
        git_dir, "rev-parse", "--verify", "-q", f"{branch}^{{commit}}", timeout_s=timeout_s
    )
    return "" if rc == 0 else "deadline" if rc == -1 else "no_branch_ref"


async def branch_history(
    git_dir: str,
    branch: str,
    groups: list[HunkGroup],
    *,
    timeout_s: float = LINE_TIMEOUT_S,
    gate: asyncio.Semaphore | None = None,
    pr_number: int = 0,
    merge_sha: str = "",
) -> HistoryPresence:
    """One branch: the tip's presence of every informative line of every
    path (one grep per path), `patch_presence`'s rules on it, then — when the lines did
    not decide — the history of ≤ PROBE_LINES lines: the removed lines the
    tip lacks when the fix's lines are absent and the pre-image mostly gone
    (`restructured` / `removed_on_branch`), or the commits that brought the
    fix's added lines when they are all there (`picked` when one names the
    fix). Git processes run under `gate`."""
    started = time.monotonic()
    hp = HistoryPresence()
    per_path = path_lines(groups)
    hp.paths = len(per_path)
    if not per_path:
        hp.reason = "no_informative_lines"
        return hp
    if reason := await _unreadable_ref(git_dir, branch, timeout_s=timeout_s):
        hp.reason = reason
        return hp

    async def gated(coro: Any) -> Any:
        if gate is None:
            return await coro
        async with gate:
            return await coro

    tips = await asyncio.gather(
        *(
            gated(tip_presence(git_dir, branch, p, [*a, *r], timeout_s=timeout_s))
            for p, (a, r) in per_path.items()
        ),
        return_exceptions=True,
    )
    absent_removed: list[tuple[str, str]] = []
    present_added: list[tuple[str, str]] = []
    for (p, (a, r)), tip in zip(per_path.items(), tips, strict=True):
        if isinstance(tip, BaseException):
            hp.reason = f"error:{type(tip).__name__}"
            hp.ms = int((time.monotonic() - started) * 1000)
            return hp
        if tip is None:
            hp.reason = "deadline"
            hp.ms = int((time.monotonic() - started) * 1000)
            return hp
        if not tip and (a or r):
            hp.paths_absent += 1
        hp.added_total += len(a)
        hp.added_present += sum(1 for x in a if tip.get(x))
        hp.removed_total += len(r)
        hp.removed_present += sum(1 for x in r if tip.get(x))
        absent_removed.extend((p, x) for x in r if not tip.get(x))
        present_added.extend((p, x) for x in a if tip.get(x))
    if hp.paths_absent == hp.paths:
        # a file the branch does not have holds neither image: nothing to pick
        hp.mode, hp.verdict = EXACT, "not_affected"
        hp.epistemic, hp.reason = "lower_bound", PATH_ABSENT
        hp.ms = int((time.monotonic() - started) * 1000)
        return hp
    decide_tip(hp)
    leg = history_leg(hp) if not hp.verdict else ""
    if leg == "removed":
        probes = sorted(absent_removed, key=lambda t: (-len(t[1]), t[0], t[1]))[:PROBE_LINES]
        coros = [gated(line_history(git_dir, branch, p, x, timeout_s=timeout_s)) for p, x in probes]
    elif leg == "added":
        probes = sorted(present_added, key=lambda t: (-len(t[1]), t[0], t[1]))[:PROBE_LINES]
        coros = [
            gated(
                line_origin(
                    git_dir,
                    branch,
                    p,
                    x,
                    pr_number=pr_number,
                    merge_sha=merge_sha,
                    timeout_s=timeout_s,
                )
            )
            for p, x in probes
        ]
    else:
        probes, coros = [], []
    if probes:
        results = await asyncio.gather(*coros, return_exceptions=True)
        for (p, x), res in zip(probes, results, strict=True):
            hp.lines.append(
                LineHistory(p, x, f"error:{type(res).__name__}")
                if isinstance(res, BaseException)
                else res
            )
        decide_history(hp) if leg == "removed" else decide_origin(hp)
    hp.ms = int((time.monotonic() - started) * 1000)
    return hp


def history_leg(hp: HistoryPresence) -> str:
    """Which history read the tip's counts call for: `removed` (the fix's
    lines absent, at most half of the pre-image left — restructured, or
    removed since?), `added` (the fix's lines all there — which commit
    brought them?), or '' (a genuine mix: no read decides it)."""
    added_ratio = hp.added_present / hp.added_total if hp.added_total else None
    removed_ratio = hp.removed_present / hp.removed_total if hp.removed_total else None
    fix_absent = added_ratio is None or added_ratio <= ADDED_ABSENT_MAX
    if (
        fix_absent
        and hp.removed_total
        and hp.removed_present < hp.removed_total
        and (removed_ratio is not None and removed_ratio <= REMOVED_REMNANT_MAX)
    ):
        return "removed"
    if added_ratio is not None and added_ratio >= PICKED_ADDED_MIN and hp.added_total >= MIN_LINES:
        return "added"
    return ""


# ---- the verdict -----------------------------------------------------------------------


def decide_tip(hp: HistoryPresence) -> None:
    """`patch_presence`'s line rules on what the branch's TIP holds: the fixed state
    (`not_affected`) or the bug (`gap`), both exact; otherwise no verdict yet
    (`undecided` unless too few lines)."""
    if hp.added_total + hp.removed_total < MIN_LINES:
        hp.reason = "too_few_lines"
        return
    added_ratio = hp.added_present / hp.added_total if hp.added_total else None
    gone_ratio = (
        (hp.removed_total - hp.removed_present) / hp.removed_total if hp.removed_total else None
    )
    if is_fix_present(added_ratio, gone_ratio, hp.removed_total):
        hp.mode, hp.verdict = EXACT, "not_affected"
        hp.epistemic, hp.reason = "exact", "fixed_at_tip"
        return
    if is_fix_absent(added_ratio, gone_ratio, hp.removed_total):
        hp.mode, hp.verdict, hp.epistemic = EXACT, "gap", "exact"
        return
    hp.reason = "undecided"


def decide_history(hp: HistoryPresence) -> None:
    """The pre-image the tip lacks, by history: every probed line never on
    the branch → `restructured`; one or more removed by a later commit →
    `removed_on_branch`; both `not_affected`, `lower_bound`. A deadline or
    an error on any probe decides nothing."""
    states = [x.state for x in hp.lines]
    if not states:
        hp.reason = "no_probes"
        return
    bad = [s for s in states if s not in (NEVER, REMOVED)]
    if bad:
        hp.reason = "deadline" if "deadline" in bad else bad[0]
        return
    hp.mode, hp.verdict, hp.epistemic = EXACT, "not_affected", "lower_bound"
    if all(s == NEVER for s in states):
        hp.reason = "restructured" if not hp.removed_present else "restructured_partial"
    else:
        hp.reason = "removed_on_branch"


def decide_origin(hp: HistoryPresence) -> None:
    """The fix's added lines are all at the tip: when the commit that brought
    one of them names the fix (its PR number or merge sha), the branch holds
    a pick the record did not title — `not_affected`, exact, `picked`. A
    commit that names nothing decides nothing (generic lines match by
    chance); a deadline or an error decides nothing."""
    states = [x.state for x in hp.lines]
    if not states:
        hp.reason = "no_probes"
        return
    if any(s == PICKED for s in states):
        hp.mode, hp.verdict, hp.epistemic, hp.reason = EXACT, "not_affected", "exact", PICKED
        return
    bad = [s for s in states if s not in (ADDED, NEVER)]
    hp.reason = ("deadline" if "deadline" in bad else bad[0]) if bad else "unnamed_origin"


def refine(v: Any, hp: HistoryPresence, cherry_pick: dict[str, Any]) -> None:
    """Fold one branch's tip-and-history answer into its verdict (a
    `BranchVerdict`): only a row presence floored and the chunk lines left
    `undecided` moves — `gap` / `not_affected` with the patch leg's
    `symbol_only(undecided)`. The record's `covered` / `pending`, every
    `unknown` and every row the chunk lines already decided stand. The block
    is disclosed wherever the leg ran."""
    from server.tracker.domain.backport import GAP, NOT_AFFECTED

    v.presence["history"] = hp.as_dict()
    patch = v.presence.get("patch") or {}
    floored = v.verdict in (GAP, NOT_AFFECTED) and v.epistemic == "lower_bound"
    if not floored or patch.get("reason") != "undecided" or not hp.verdict:
        return
    if hp.verdict == GAP:
        v.verdict, v.epistemic, v.basis, v.reason = GAP, "exact", HISTORY, ""
        v.cherry_pick = dict(cherry_pick)
        v.evidence.append(
            f"at {v.branch}'s tip (git grep on the mirror) {hp.removed_present}/{hp.removed_total} "
            f"of the fix's removed lines are present and {hp.added_present}/{hp.added_total} added "
            "lines: the pre-image is on the branch"
        )
        return
    v.verdict, v.epistemic, v.basis = NOT_AFFECTED, hp.epistemic, HISTORY
    v.reason = "" if hp.reason in ("fixed_at_tip", PICKED) else hp.reason
    v.cherry_pick = {}
    if hp.reason == "fixed_at_tip":
        v.evidence.append(
            f"at {v.branch}'s tip {hp.added_present}/{hp.added_total} of the fix's added lines "
            f"are present and {hp.removed_total - hp.removed_present}/{hp.removed_total} removed "
            "lines gone: the branch holds the fixed state; the record names no backport"
        )
    elif hp.reason == PATH_ABSENT:
        v.evidence.append(
            f"none of the fix's {hp.paths} source files exists at {v.branch}'s tip: the branch "
            "does not have the code the fix changes"
        )
    elif hp.reason == PICKED:
        picks = sorted({f"{x.commit} {x.subject[:60]}" for x in hp.lines if x.state == PICKED})
        v.evidence.append(
            f"the fix's added lines are at {v.branch}'s tip and the commit that brought them "
            f"names the fix ({'; '.join(picks[:2])}): a pick the record does not title as a "
            "backport"
        )
    elif hp.reason == "restructured_partial":
        v.evidence.append(
            f"{hp.removed_present}/{hp.removed_total} of the fix's removed lines are at "
            f"{v.branch}'s tip and {len(hp.lines)} probed absent lines never existed on the "
            "branch (git log -S over its first-parent history): the branch carries a remnant of "
            "the pre-image in a shape the fix does not apply to — the bug may exist there in "
            "another shape"
        )
    elif hp.reason == "restructured":
        v.evidence.append(
            f"none of the fix's {hp.removed_total} removed lines is at {v.branch}'s tip and "
            f"{len(hp.lines)} probed never existed on the branch (git log -S over its "
            "first-parent history): the branch never carried the removed code — the bug may "
            "exist there in another shape"
        )
    else:
        gone = sorted({x.commit for x in hp.lines if x.commit})
        v.evidence.append(
            f"the fix's removed lines were on {v.branch} and a later commit removed them "
            f"({', '.join(gone[:3])}): the pre-image is gone; the record names no backport"
        )


__all__ = [
    "ADDED",
    "HISTORY",
    "LINES_PER_PATH",
    "MIN_LINES",
    "NEVER",
    "PATH_ABSENT",
    "PICKED",
    "PRESENT",
    "PROBE_LINES",
    "REMOVED",
    "HistoryPresence",
    "LineHistory",
    "MirrorReadError",
    "branch_history",
    "decide_history",
    "decide_origin",
    "decide_tip",
    "history_leg",
    "line_history",
    "line_origin",
    "line_regex",
    "names_fix",
    "path_lines",
    "refine",
    "tip_presence",
]
