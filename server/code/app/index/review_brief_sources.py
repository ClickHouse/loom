"""Where a review brief's PR facts come from when the ledger lacks the merge: the
mirror's merge and commit records, the tracker's PR record, and the provenance the
brief states."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from server.code.app.index.review_brief import FAILED, NOT_BOUND, RESTRICTED, SERVED
from server.settings import settings

logger = logging.getLogger(__name__)


# `provenance.source` of a brief built without a ledger row; a ledger-backed build
# carries no provenance key at all.
BUILT_FROM_MIRROR = "built_from_mirror"
LEDGER_ABSENT = "absent"
# Candidates one first-parent grep may return: `--grep` matches the whole
# message, so a merge whose BODY cites `(#N)` can precede the merge OF #N.
_MIRROR_LOG_CANDIDATES = 8
_MIRROR_CACHE_MAX = 4096
_mirror_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any] | None]] = {}
_MERGE_SUBJECT_RE = re.compile(r"^Merge pull request #\d+ from \S+\s*$")


def merge_title(subject: str, body: str) -> str:
    """The PR's title as the merge commit carries it: a squash subject
    `Title (#N)` IS the title; a `Merge pull request #N from …` subject
    carries none, and the title is the body's first non-empty line."""
    subject = (subject or "").strip()
    if _MERGE_SUBJECT_RE.match(subject):
        for line in (body or "").splitlines():
            if line.strip():
                return line.strip()[:200]
    return subject[:200]


def _parse_log_records(text: str) -> list[tuple[str, str, str]]:
    """(sha, subject, body) per `--format=%H%x00%s%x00%b%x1e` record."""
    out: list[tuple[str, str, str]] = []
    for rec in text.split("\x1e"):
        parts = rec.lstrip("\n").split("\x00")
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{40}", parts[0].strip()):
            out.append((parts[0].strip(), parts[1], parts[2] if len(parts) > 2 else ""))
    return out


async def _commit_facts(git_dir: str, sha: str, subject: str, body: str) -> dict[str, Any]:
    from server.code.app.index import gitrepo
    from server.code.domain.index.blame_range import pr_number_of

    entries = await gitrepo.diff_files(git_dir, f"{sha}^1", sha)
    return {
        "pr_number": pr_number_of(subject),
        "merge_sha": sha,
        "title": merge_title(subject, body),
        "subject": subject.strip()[:300],
        "files": [e.path for e in entries],
    }


async def mirror_merge_facts(
    git_dir: str,
    branch: str,
    pr_number: int,
    *,
    timeout_s: float = 20.0,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """The merge commit of PR `pr_number` on the mirror's first-parent chain of `branch`, with the
    PR's title and changed files — what the ledger would have said, for a PR merged before the
    ledger's horizon. One bounded `git log --first-parent --grep` per PR, cached per (mirror,
    branch, PR): a hit forever, a miss for a while. None when no first-parent merge names the PR in
    its subject."""
    from server.code.app.index import gitrepo
    from server.code.domain.index.blame_range import pr_number_of

    n = int(pr_number)
    key = (git_dir, branch, n)
    hit = _mirror_cache.get(key)
    if hit is not None and (
        hit[1] is not None or now() - hit[0] < settings.code_review_brief_mirror_miss_ttl_s
    ):
        return hit[1]
    facts: dict[str, Any] | None = None
    if n > 0:
        text = await gitrepo.run(
            git_dir,
            "log",
            "--first-parent",
            f"-{_MIRROR_LOG_CANDIDATES}",
            f"--grep=Merge pull request #{n} from",
            f"--grep=(#{n})",
            "--format=%H%x00%s%x00%b%x1e",
            branch,
            timeout_s=timeout_s,
        )
        for sha, subject, body in _parse_log_records(text):
            if pr_number_of(subject) == n:
                facts = await _commit_facts(git_dir, sha, subject, body)
                break
    if len(_mirror_cache) >= _MIRROR_CACHE_MAX:
        _mirror_cache.pop(next(iter(_mirror_cache)))
    _mirror_cache[key] = (now(), facts)
    return facts


async def mirror_commit_facts(
    git_dir: str,
    sha: str,
    *,
    timeout_s: float = 20.0,
) -> dict[str, Any] | None:
    """The same facts for a merge commit named by sha (a `merge_sha`-only
    read with no ledger row): None when the sha is not a commit or its
    subject names no PR."""
    from server.code.app.index import gitrepo

    text = await gitrepo.run(
        git_dir,
        "log",
        "-1",
        "--format=%H%x00%s%x00%b%x1e",
        sha,
        timeout_s=timeout_s,
    )
    for full, subject, body in _parse_log_records(text):
        facts = await _commit_facts(git_dir, full, subject, body)
        return facts if facts["pr_number"] else None
    return None


async def tracker_pr_facts(org: str, ns: str, pr_number: int) -> tuple[str, dict[str, Any]]:
    """(status, row) of the tracker record at one PR through the same gate as the
    brief's `qa` section: `not_bound`, `restricted`, `no_record` or `served` with the
    PR's title, reviewers, has_test and backport flag — the reviewer facts a ledger row
    would not carry either."""
    from server.identity.app.namespace_classification import is_restricted
    from server.tracker.app.binding import refresh_overrides, tracker_namespace_for
    from server.tracker.app.source import tracker_source

    await refresh_overrides()  # the links as of the refresh interval, branches included
    tracker_ns = tracker_namespace_for(org, ns)
    if not tracker_ns:
        return NOT_BOUND, {}
    if await is_restricted(org, tracker_ns):
        return RESTRICTED, {}
    rows = await tracker_source(org, tracker_ns).items_by_number(org, tracker_ns, [int(pr_number)])
    row = rows.get(int(pr_number))
    if not row:
        return "no_record", {}
    return SERVED, {
        "title": str(row.get("title") or ""),
        "reviewers": [str(x) for x in row.get("reviewers") or []],
        "author": str(row.get("author") or ""),
        "has_test": int(row.get("has_test", -1)),
        "is_backport_pr": int(row.get("is_backport_pr") or 0),
    }


async def mirror_provenance(org: str, ns: str, facts: dict[str, Any]) -> dict[str, Any]:
    """The disclosure a mirror-built brief carries in its payload: where the
    merge commit, files and title came from, the reviewers when the QA
    record has them. The QA title, when there is one, replaces the merge
    commit's in `facts` (the body's first line is the title as the author
    typed it at merge time; QA carries the PR's current one). QA failing is
    a fact on the record, never a failed brief."""
    try:
        tracker_status, tracker_row = await tracker_pr_facts(org, ns, int(facts["pr_number"]))
    except Exception as e:  # noqa: BLE001 — the brief stands without the reviewers
        logger.info("review_brief: qa facts for PR %s failed: %s", facts.get("pr_number"), e)
        tracker_status, tracker_row = FAILED, {}
    title_from = "tracker" if tracker_row.get("title") else "mirror"
    if title_from == "tracker":
        facts["title"] = str(tracker_row["title"])[:200]
    return {
        "source": BUILT_FROM_MIRROR,
        "ledger": LEDGER_ABSENT,
        "merge_commit": "mirror_first_parent",
        "files": "mirror_diff",
        "title": title_from,
        "tracker": tracker_status,
        "reviewers": list(tracker_row.get("reviewers") or []),
        "author": str(tracker_row.get("author") or ""),
        "has_test": int(tracker_row.get("has_test", -1)),
        "is_backport_pr": int(tracker_row.get("is_backport_pr") or 0),
    }
