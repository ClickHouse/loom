"""Has every index tier reached this commit?

A consumer that reviews a merged PR wants the compiler tiers' exact symbols and call
graph for the PR's merge commit, not the tree-sitter approximation the fast tier serves
minutes earlier. The watermarks alone cannot answer that: `scip_lag` counts commits
between a watermark and the branch tip, and on a busy branch new merges land faster than
the tier converges, so "lag 0" may never hold while every older merge is long covered.

The question is per commit: is the merge commit an ancestor of (or equal
to) each tier's watermark? One `git merge-base --is-ancestor` per tier on the
local mirror answers it. The answer for a (commit, watermark) pair never
changes, so it is memoized. A commit the mirror does not hold (not fetched
yet, or wrong repository) is `None` and never "covered".
"""

from __future__ import annotations

import re
from typing import Any

SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# (git_dir, commit, watermark) -> bool | None; bounded, positive answers only
# are permanent (a watermark only moves forward), negatives expire with it.
_MEMO: dict[tuple[str, str, str], bool] = {}
_MEMO_MAX = 4096


# How far the index has moved past the commit. Immutable history, so memoized per pair:
# (git_dir, since, until) -> count / paths.
_COUNT_MEMO: dict[tuple[str, str, str], int] = {}
_PATHS_MEMO: dict[tuple[str, str, str], list[str]] = {}
_PATHS_MEMO_MAX = 512
PATHS_CAP = 300


async def _commits_since(git_dir: str, commit: str, watermark: str) -> int:
    from server.code.app.index import gitrepo

    if commit.lower() == watermark.lower():
        return 0
    if not git_dir:
        return -1
    key = (git_dir, commit.lower(), watermark.lower())
    if key in _COUNT_MEMO:
        return _COUNT_MEMO[key]
    try:
        count = int(await gitrepo.commit_count_between(git_dir, commit, watermark))
    except Exception:  # noqa: BLE001 — unknown sha: -1, never a guess
        return -1
    if len(_COUNT_MEMO) >= _MEMO_MAX:
        _COUNT_MEMO.clear()
    _COUNT_MEMO[key] = count
    return count


async def _paths_since(git_dir: str, commit: str, head: str) -> list[str] | None:
    from server.code.app.index import gitrepo

    if commit.lower() == head.lower():
        return []
    if not git_dir:
        return None
    key = (git_dir, commit.lower(), head.lower())
    if key in _PATHS_MEMO:
        return _PATHS_MEMO[key]
    try:
        paths = await gitrepo.diff_name_only(git_dir, commit, head, limit=100_000)
    except Exception:  # noqa: BLE001 — an unknown sha or a failed diff: None, never a guess
        return None
    if len(_PATHS_MEMO) >= _PATHS_MEMO_MAX:
        _PATHS_MEMO.clear()
    _PATHS_MEMO[key] = paths
    return paths


def valid_commit(commit: str) -> bool:
    return bool(SHA_RE.match((commit or "").strip()))


async def _covered(git_dir: str, commit: str, watermark: str) -> bool | None:
    """Whether `commit` is reachable from `watermark`; None when either is
    unknown to the mirror or the watermark is unset."""
    from server.code.app.index import gitrepo

    if not watermark:
        return None
    if commit.lower() == watermark.lower():
        return True
    if not git_dir:
        return None  # no mirror here to walk the history with
    key = (git_dir, commit.lower(), watermark.lower())
    if key in _MEMO:
        return _MEMO[key]
    try:
        got = await gitrepo.is_ancestor(git_dir, commit, watermark)
    except Exception:  # noqa: BLE001 — a timeout or a missing mirror is "unknown"
        return None
    if got is not None:
        if len(_MEMO) >= _MEMO_MAX:
            _MEMO.clear()
        _MEMO[key] = got
    return got


async def commit_coverage(
    git_dir: str,
    commit: str,
    git_head: str,
    tiers: list[dict[str, Any]],
    paths: list[str] | None = None,
    gap: str = "",
) -> dict[str, Any]:
    """The coverage block `code.index_status` returns for `commit`.

    `tiers` are the status's compiler tiers (producer, label, watermark_key,
    commit). Every tier whose watermark is set is required: `ready` is true
    only when the fast tier and all of them reach the commit. `waiting_on`
    names the tiers that do not yet, in the status's order. An empty `git_dir`
    is a host without the mirror; `gap` says why (`mirrors.history_dir`)."""
    commit = (commit or "").strip()
    paths_asked = [p for p in (paths or []) if isinstance(p, str) and p][:2000]
    out: dict[str, Any] = {
        "commit": commit,
        "in_mirror": False,
        "fast": None,
        "tiers": [],
        "ready": False,
        "waiting_on": [],
        "commits_since": -1,
        "paths_changed_since": [],
        "paths_changed_count": -1,
        "paths_truncated": False,
        "paths_changed_overlap": [],
    }
    if not valid_commit(commit):
        out["waiting_on"] = ["invalid commit"]
        return out
    fast = await _covered(git_dir, commit, git_head)
    out["fast"] = fast
    if fast:
        # The drift a reviewer of this commit has to know, up to the fast tier's head.
        # A compiler tier may run ahead of it (it follows the mirror's branch head),
        # but `ready` still needs the fast tier, so its head is the drift that matters.
        out["commits_since"] = await _commits_since(git_dir, commit, git_head)
        paths = await _paths_since(git_dir, commit, git_head)
        if paths is not None:
            out["paths_changed_count"] = len(paths)
            out["paths_changed_since"] = paths[:PATHS_CAP]
            out["paths_truncated"] = len(paths) > PATHS_CAP
            if paths_asked:
                # the reviewer's own files that a later merge changed (usually none)
                moved = set(paths)
                out["paths_changed_overlap"] = [p for p in paths_asked if p in moved]
    waiting: list[str] = [] if fast else ["fast tier"]
    known = fast is not None
    for t in tiers:
        wm = str(t.get("commit") or "")
        cov = await _covered(git_dir, commit, wm) if wm else None
        known = known or cov is not None
        out["tiers"].append(
            {
                "producer": t.get("producer", ""),
                "label": t.get("label", ""),
                "watermark_key": t.get("watermark_key", ""),
                "watermark": wm,
                "covered": cov,
                "commits_since": await _commits_since(git_dir, commit, wm) if cov else -1,
            }
        )
        if wm and not cov:
            waiting.append(str(t.get("label") or t.get("producer") or t.get("watermark_key")))
    out["in_mirror"] = known
    if not known:
        waiting = [
            f"commit not checked: {gap or 'no git mirror on this host'}"
            if not git_dir
            else "commit not in the index's mirror yet"
        ]
    out["waiting_on"] = waiting
    out["ready"] = known and not waiting
    return out


def reset() -> None:
    """Test seam."""
    _MEMO.clear()
    _COUNT_MEMO.clear()
    _PATHS_MEMO.clear()
