"""Merge commit shas for candidate PRs: the ledger's stored value first, else the mirror.

A candidate PR carries the commit that landed it on the target branch, so a caller can
diff, bisect or cherry-pick it without a GitHub round trip. `code_pr_history` stores
`merge_commit_sha` for the PRs it ingested; for the rest one bounded `git log
--first-parent` over the target branch finds the `Merge pull request #N` merge or the
squash commit whose subject ends `(#N)`, for every missing PR in a single call.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from server.code.app.index import gitrepo
from server.code.domain.index.blame_range import landed_pr_of, sha_text
from server.settings import settings

logger = logging.getLogger("loom.code_index.merge_shas")

SOURCE_STORED = "stored"
SOURCE_MIRROR = "mirror"

# Slack before the oldest candidate's merge time, for a ledger clock that drifts from the
# committer time
SINCE_SLACK_S = 7 * 86400

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_landed(log_text: str, wanted: set[int]) -> dict[int, str]:
    """`git log --format=%H%x09%s` → {pr_number: sha} for the wanted PRs a subject lands,
    newest first (a PR landed twice, e.g. after a revert, keeps its latest landing)."""
    out: dict[int, str] = {}
    for line in (log_text or "").splitlines():
        sha, tab, subject = line.partition("\t")
        if not tab or not _SHA40_RE.match(sha):
            continue
        n = landed_pr_of(subject)
        if n in wanted and n not in out:
            out[n] = sha
    return out


def grep_pattern(numbers: list[int]) -> str:
    """One extended regex matching a message line that names any of the PR numbers; the
    subject parse decides whether it landed the PR."""
    return "#(" + "|".join(str(n) for n in sorted(set(numbers))) + ")([^0-9]|$)"


async def mirror_landed(
    git_dir: str, ref: str, numbers: list[int], *, since_unix: int = 0
) -> dict[int, str]:
    """{pr_number: sha} for the PRs `ref`'s first-parent history lands, in one git call.
    Empty on any git failure: the mirror is a fallback, never the answer's gate."""
    wanted = {int(n) for n in numbers if int(n) > 0}
    if not wanted or not git_dir or not ref:
        return {}
    args = [
        "log",
        "--first-parent",
        "--format=%H%x09%s",
        "-E",
        f"--grep={grep_pattern(sorted(wanted))}",
        f"--max-count={settings.code_merge_shas_max_log_commits}",
    ]
    if since_unix > SINCE_SLACK_S:
        args.append(f"--since={since_unix - SINCE_SLACK_S}")
    args += [ref, "--"]
    try:
        text = await gitrepo.run(git_dir, *args, timeout_s=settings.code_merge_shas_log_timeout_s)
    except gitrepo.GitError as e:
        logger.info("merge shas: mirror search failed (%s)", e)
        return {}
    return parse_landed(text, wanted)


async def resolve(
    git_dir: str,
    ref: str,
    numbers: list[int],
    stored: dict[int, Any],
    *,
    since_unix: int = 0,
) -> dict[int, tuple[str, str]]:
    """{pr_number: (sha, source)} for every number: the stored sha (`stored`), else the
    mirror's (`mirror`), else ("", "")."""
    out: dict[int, tuple[str, str]] = {}
    missing: list[int] = []
    for n in dict.fromkeys(int(x) for x in numbers):
        sha = sha_text(stored.get(n))
        if sha:
            out[n] = (sha, SOURCE_STORED)
        else:
            missing.append(n)
    found = await mirror_landed(git_dir, ref, missing, since_unix=since_unix) if missing else {}
    for n in missing:
        sha = found.get(n, "")
        out[n] = (sha, SOURCE_MIRROR if sha else "")
    return out


__all__ = [
    "SOURCE_MIRROR",
    "SOURCE_STORED",
    "grep_pattern",
    "mirror_landed",
    "parse_landed",
    "resolve",
]
