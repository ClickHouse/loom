"""Pure helpers for `code.first_build`: which CI build first carried a commit, read from
the tracker's `version_history` view and checked against loom's bare mirror.

What the view holds (checked against the upstream the view proxies): one row per CI check run that
produced a version. A PR run's `commit_sha` is the PR's head commit and `parent_commits_sha` is
`[base branch head at run time, PR head]` — the parents of the synthetic merge commit CI built; its
`version` is the branch's generic stamp (`26.9.1.1` on master). The view keeps `pull_request_number
> 0` only, so the numbered master and release-branch builds (`26.9.1.1571`, git_ref `master`) are
NOT visible through it, and `docker_tag` is empty upstream. The helpers here therefore reason about
three kinds of build:

* `named`  — a row whose `commit_sha` or `parent_commits_sha` IS the sha:
  the build contains that exact commit (`exact`).
* `own`    — the PR's own head builds (`pull_request_number = N`): the
  binaries CI built from the PR before merge (`exact`).
* `carrier` — the earliest run after the merge whose base commit descends
  from the sha (`git merge-base --is-ancestor` on the mirror); it contains
  the merge plus the base's later commits plus that PR's own change
  (`lower_bound`: the earliest of ≤ 10 checked bases).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

BUILT = "built"
NOT_YET_BUILT = "not_yet_built"
UNKNOWN = "unknown"

KIND_NAMED = "named"
KIND_OWN = "own"
KIND_CARRIER = "carrier"

MAX_CANDIDATES = 10
WINDOW_HOURS = 72

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_RELEASE_BRANCH = re.compile(r"^(\d+)\.(\d+)$")


def normalize_sha(text: str) -> str:
    """A 7–40 hex sha, lower-cased; "" for anything else."""
    s = str(text or "").strip().lower()
    return s if _SHA.match(s) else ""


def sha_matches(full: str, given: str) -> bool:
    """`given` (possibly abbreviated) names `full`."""
    return bool(full and given) and full.startswith(given)


@dataclass
class Build:
    kind: str
    check_start_time: datetime | None
    version: str = ""
    docker_tag: str = ""
    minor_version: str = ""
    git_ref: str = ""
    pull_request_number: int = 0
    commit_sha: str = ""
    base_sha: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "check_start_time": iso(self.check_start_time),
            "version": self.version,
            "docker_tag": self.docker_tag,
            "minor_version": self.minor_version,
            "git_ref": self.git_ref,
            "pull_request_number": self.pull_request_number,
            "commit_sha": self.commit_sha,
            "base_sha": self.base_sha,
        }


@dataclass
class BaseCandidate:
    """One distinct base commit the runs after the merge were built on,
    with the earliest run's facts (the `builds_after` aggregate row)."""

    base_sha: str
    first_seen: datetime | None
    builds: int
    first: Build = field(default_factory=lambda: Build(kind=KIND_CARRIER, check_start_time=None))


def iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat()


def _dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v.astimezone(UTC).replace(tzinfo=None) if v.tzinfo is not None else v
    return None


def _build_from_row(r: dict[str, Any], kind: str) -> Build:
    parents = [str(p) for p in (r.get("parent_commits_sha") or [])]
    commit = str(r.get("commit_sha") or "")
    base = next((p for p in parents if p != commit), parents[0] if parents else "")
    return Build(
        kind=kind,
        check_start_time=_dt(r.get("check_start_time")),
        version=str(r.get("version") or ""),
        docker_tag=str(r.get("docker_tag") or ""),
        minor_version=str(r.get("minor_version") or ""),
        git_ref=str(r.get("git_ref") or ""),
        pull_request_number=int(r.get("pull_request_number") or 0),
        commit_sha=commit,
        base_sha=base,
    )


def split_named(
    rows: list[dict[str, Any]], sha: str, pr_number: int
) -> tuple[list[Build], list[Build]]:
    """(builds naming `sha`, the PR's own head builds), each oldest first.
    A row can be both (the PR's head IS the sha); it is listed in both."""
    named: list[Build] = []
    own: list[Build] = []
    for r in rows:
        commit = str(r.get("commit_sha") or "")
        parents = [str(p) for p in (r.get("parent_commits_sha") or [])]
        if sha and (sha_matches(commit, sha) or any(sha_matches(p, sha) for p in parents)):
            named.append(_build_from_row(r, KIND_NAMED))
        if pr_number and int(r.get("pull_request_number") or 0) == pr_number:
            own.append(_build_from_row(r, KIND_OWN))
    key = lambda b: b.check_start_time or datetime.min  # noqa: E731
    return sorted(named, key=key), sorted(own, key=key)


def candidates_of(rows: list[dict[str, Any]]) -> list[BaseCandidate]:
    """The `builds_after` aggregate rows as candidates, earliest base first."""
    out: list[BaseCandidate] = []
    for r in rows:
        base = str(r.get("base_sha") or "")
        if not base:
            continue
        first = Build(
            kind=KIND_CARRIER,
            check_start_time=_dt(r.get("first_seen")),
            version=str(r.get("first_version") or ""),
            docker_tag=str(r.get("first_docker_tag") or ""),
            minor_version=str(r.get("first_minor_version") or ""),
            git_ref=str(r.get("first_git_ref") or ""),
            pull_request_number=int(r.get("first_pr") or 0),
            commit_sha=str(r.get("first_commit") or ""),
            base_sha=base,
        )
        out.append(
            BaseCandidate(
                base_sha=base,
                first_seen=first.check_start_time,
                builds=int(r.get("builds") or 0),
                first=first,
            )
        )
    out.sort(key=lambda c: c.first_seen or datetime.min)
    return out


def later_builds(candidates: list[BaseCandidate], first: Build) -> int:
    """Runs in the window started after the first carrying run: the rest
    of that run's base plus every run on a base first seen later (later
    bases are later branch heads; not individually checked)."""
    if first.check_start_time is None:
        return 0
    n = 0
    for c in candidates:
        if c.first_seen is None:
            continue
        if c.first_seen > first.check_start_time:
            n += c.builds
        elif c.base_sha == first.base_sha and c.first_seen == first.check_start_time:
            n += max(0, c.builds - 1)
    return n


def lag_hours(since: datetime | None, first: datetime | None) -> float | None:
    if since is None or first is None:
        return None
    return round((first - since).total_seconds() / 3600.0, 2)


def verdict_of(
    *,
    qa_ok: bool,
    subject_known: bool,
    named: list[Build],
    own: list[Build],
    carrier: BaseCandidate | None,
    checked: int,
    exhausted: bool,
    merge_known: bool,
) -> tuple[str, list[str], str]:
    """(verdict, causes, epistemic). `built` when a build contains the
    subject's code — a row naming the sha, a descendant base, or (a PR the
    ledger holds no merge for) the PR's own head builds. `not_yet_built`
    only when every base in the window was checked and none descends.
    `unknown` names why: the tracker record could not be read, the subject is
    unknown everywhere, or the ≤ 10 checked bases were all stale while
    more remained."""
    if not qa_ok:
        return UNKNOWN, ["tracker_unavailable"], "lower_bound"
    if named:
        return BUILT, [], "exact"
    if carrier is not None:
        return BUILT, [], "lower_bound"
    if own:
        causes = [] if merge_known else ["pr_not_merged"]
        return BUILT, causes, "exact"
    if not subject_known:
        return UNKNOWN, ["sha_unknown"], "lower_bound"
    if exhausted:
        return UNKNOWN, ["candidates_exhausted"], "lower_bound"
    causes = [] if merge_known else ["pr_not_merged"]
    return NOT_YET_BUILT, causes, "exact" if checked or merge_known else "lower_bound"


def release_key(branch: str) -> tuple[int, int] | None:
    m = _RELEASE_BRANCH.match(branch or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def nearest_namespace(
    branches: list[str], siblings: list[Any], own_namespace: str
) -> tuple[list[str], str]:
    """(namespaces whose configured branch contains the sha, the nearest):
    the lowest release branch (`26.6` before `26.8`) that contains it,
    else the namespace's own branch when it does, else the first match."""
    contained: list[tuple[Any, str]] = []
    for cfg in siblings:
        branch = cfg.git_branch
        if branch and branch in branches:
            contained.append((cfg, branch))
    names = [str(cfg.namespace) for cfg, _ in contained]
    releases = sorted(
        ((release_key(b), str(cfg.namespace)) for cfg, b in contained if release_key(b)),
        key=lambda x: x[0] or (0, 0),
    )
    if releases:
        return names, releases[0][1]
    if own_namespace in names:
        return names, own_namespace
    return names, names[0] if names else ""


__all__ = [
    "BUILT",
    "KIND_CARRIER",
    "KIND_NAMED",
    "KIND_OWN",
    "MAX_CANDIDATES",
    "NOT_YET_BUILT",
    "UNKNOWN",
    "WINDOW_HOURS",
    "BaseCandidate",
    "Build",
    "candidates_of",
    "iso",
    "lag_hours",
    "later_builds",
    "nearest_namespace",
    "normalize_sha",
    "release_key",
    "sha_matches",
    "split_named",
    "verdict_of",
]
