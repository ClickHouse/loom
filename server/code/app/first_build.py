"""code.first_build: a merge names the first CI build that carried it.

Addressed at a CODE namespace. The subject is a commit sha or a PR number (the ledger maps
the PR to its merge commit and time; the mirror confirms the sha and its commit time). QA
Intelligence's `version_history` view, read through the tracker namespace bound to the code
namespace (one keyed read and one range read, both cached), supplies the CI runs: rows that
name the sha (as the built commit or as the base of the synthetic merge CI built) are
`exact`; otherwise the distinct base commits of the runs in the 72 h after the merge are
checked oldest first with `git merge-base --is-ancestor` on the bare mirror (≤ 10), and the
first descendant is the first build that carried the change (`lower_bound`). The mirror's
`git branch --contains` names the release-branch namespaces whose head holds the sha.

Verdicts: `built`, `not_yet_built` (every base in the window was checked and none descends),
`unknown` with causes `tracker_unbound | tracker_unavailable | sha_unknown |
candidates_exhausted`. `note` states what the view does not show: the numbered master builds
(`pull_request_number = 0`) and docker tags (empty upstream) are not in it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from server.code.api.models_history import CodeFirstBuildOutput
from server.code.app.blame import repo_siblings
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.index import gitrepo
from server.code.app.spans import code_span, set_code_span_attributes
from server.code.domain.index import first_build as fb
from server.code.domain.index.blame_range import sha_text
from server.code.domain.index.repos import default_git_dir
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError

logger = logging.getLogger(__name__)

VIEW_NOTE = (
    "version_history exposes PR runs only (pull_request_number > 0): a carrier is a PR "
    "run whose base contains the commit; numbered master builds and docker tags are not "
    "visible through the QA view"
)


@dataclass
class Subject:
    sha: str = ""
    pr_number: int = 0
    merged_at: datetime | None = None
    commit_time: datetime | None = None
    ledger_known: bool = False
    mirror_known: bool = False
    branches: list[str] = field(default_factory=list)

    @property
    def since(self) -> datetime | None:
        return self.merged_at or self.commit_time

    @property
    def known(self) -> bool:
        return self.ledger_known or self.mirror_known


def _naive(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v.astimezone(UTC).replace(tzinfo=None) if v.tzinfo is not None else v
    return None


async def _ledger(repo: CodeRepo, org: str, ns: str, sub: Subject) -> None:
    """The PR ledger's merge commit and time, by number or by sha."""
    try:
        row = await repo.pr_merge_facts(
            org,
            ns,
            pr_number=sub.pr_number,
            merge_sha=sub.sha if (not sub.pr_number and len(sub.sha) == 40) else "",
        )
    except Exception as e:  # noqa: BLE001 — the mirror is the other witness
        logger.info("first_build: ledger read failed (%s)", e)
        return
    if not row:
        return
    sub.ledger_known = True
    sub.pr_number = sub.pr_number or int(row.get("pr_number") or 0)
    sub.sha = sub.sha or sha_text(row.get("merge_sha"))
    sub.merged_at = _naive(row.get("merged_at"))


async def _mirror(git_dir: str, sub: Subject) -> None:
    """The mirror's word on the sha: its full form, commit time and the
    branches containing it. A sha the mirror does not hold leaves the
    subject to the ledger and the tracker record."""
    if not sub.sha or not git_dir:
        return
    try:
        out = await gitrepo.run(git_dir, "log", "-1", "--format=%H %ct", sub.sha)
    except gitrepo.GitError:
        return
    parts = out.split()
    if len(parts) < 2:
        return
    sub.sha, sub.mirror_known = parts[0], True
    sub.commit_time = datetime.fromtimestamp(int(parts[1]), tz=UTC).replace(tzinfo=None)
    try:
        sub.branches = await gitrepo.branches_containing(git_dir, sub.sha)
    except gitrepo.GitError as e:
        logger.debug("first_build: branch --contains failed: %s", e)


async def _resolve(repo: CodeRepo, org: str, ns: str, git_dir: str, sub: Subject) -> None:
    """Ledger first when the subject is a PR (the sha comes from it); mirror
    first when the sha is abbreviated (the ledger is keyed by the full
    sha); both witnesses together for a full sha."""
    if sub.pr_number and not sub.sha:
        await _ledger(repo, org, ns, sub)
        await _mirror(git_dir, sub)
        return
    if len(sub.sha) < 40:
        await _mirror(git_dir, sub)
        await _ledger(repo, org, ns, sub)
        return
    await asyncio.gather(_ledger(repo, org, ns, sub), _mirror(git_dir, sub))


async def _first_carrier(
    git_dir: str, sha: str, candidates: list[fb.BaseCandidate]
) -> tuple[fb.BaseCandidate | None, int, bool]:
    """(the earliest base descending from `sha`, bases checked, whether
    unchecked bases remained). The sha itself as a base is a descendant."""
    checked = 0
    for c in candidates[: fb.MAX_CANDIDATES]:
        checked += 1
        if c.base_sha == sha:
            return c, checked, False
        try:
            ok = await gitrepo.is_ancestor(git_dir, sha, c.base_sha)
        except gitrepo.GitError as e:
            logger.info("first_build: is-ancestor failed (%s)", e)
            ok = None
        if ok:
            return c, checked, False
    return None, checked, len(candidates) > fb.MAX_CANDIDATES


async def _tracker_reads(
    source: Any, org: str, tracker_ns: str, sub: Subject
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """(rows naming the subject, base aggregates after the merge, qa_ok)."""
    from server.tracker.domain.errors import TrackerUnavailable

    async def _after() -> list[dict[str, Any]]:
        if sub.since is None:
            return []
        return list(await source.builds_after(org, tracker_ns, sub.since, fb.WINDOW_HOURS))

    try:
        naming, after = await asyncio.gather(
            source.builds_naming(org, tracker_ns, sub.sha, sub.pr_number), _after()
        )
    except TrackerUnavailable as e:
        logger.info("first_build: qa unavailable (%s)", e)
        return [], [], False
    return list(naming), list(after), True


async def handle_code_first_build(input: Any) -> Any:
    from server.tracker.app.binding import authorized_tracker_namespace
    from server.tracker.app.source import tracker_source

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    cfg, siblings = repo_siblings(org, ns)
    git_dir = (cfg.git_dir if cfg is not None else "") or default_git_dir()
    sub = Subject(sha=fb.normalize_sha(input.sha), pr_number=int(input.pr_number or 0))
    # Shape complaints come after the gate (the transport already ran it on org/namespace),
    # never before it, so a refused principal learns nothing about the input.
    if str(input.sha or "").strip() and not sub.sha:
        raise ValidationError("sha must be 7–40 hex characters")
    if not sub.sha and not sub.pr_number:
        raise ValidationError("sha or pr_number is required")
    with code_span("first_build.resolve"):
        await _resolve(repo, org, ns, git_dir, sub)
    tracker_ns = await authorized_tracker_namespace(org, ns)
    source = tracker_source(org, tracker_ns) if tracker_ns else None
    naming: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    qa_ok = False
    if source is not None and (sub.sha or sub.pr_number):
        with code_span("first_build.qa"):
            naming, after, qa_ok = await _tracker_reads(source, org, tracker_ns, sub)
    named, own = fb.split_named(naming, sub.sha, sub.pr_number)
    candidates = fb.candidates_of(after)
    carrier: fb.BaseCandidate | None = None
    checked, exhausted = 0, False
    if qa_ok and not named and sub.sha and sub.mirror_known and candidates:
        with code_span("first_build.descend", seeds=len(candidates)):
            carrier, checked, exhausted = await _first_carrier(git_dir, sub.sha, candidates)
    verdict, causes, epistemic = fb.verdict_of(
        qa_ok=qa_ok,
        subject_known=sub.known or bool(named or own),
        named=named,
        own=own,
        carrier=carrier,
        checked=checked,
        exhausted=exhausted,
        merge_known=bool(sub.sha and (sub.ledger_known or sub.mirror_known)),
    )
    if not tracker_ns and "tracker_unavailable" in causes:
        causes = ["tracker_unbound" if c == "tracker_unavailable" else c for c in causes]
    first: fb.Build | None = named[0] if named else (carrier.first if carrier else None)
    if first is None and own and verdict == fb.BUILT:
        first = own[0]
    later = fb.later_builds(candidates, first) if first is not None else 0
    contained, nearest = fb.nearest_namespace(sub.branches, siblings, ns)
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.first_build",
        query=sub.sha or f"#{sub.pr_number}",
        route="first_build",
        returned_ids=[first.version] if first else [],
        returned_paths=[],
        returned_scores=[],
        top_k=1,
        tokens_returned=0,
        abstain=verdict == fb.UNKNOWN,
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(
        route="first_build", results=1 if first else 0, consumer=input.consumer
    )
    return CodeFirstBuildOutput(
        verdict=verdict,
        causes=causes,
        epistemic=epistemic,
        sha=sub.sha,
        pr_number=sub.pr_number,
        merged_at=fb.iso(sub.merged_at),
        commit_time=fb.iso(sub.commit_time),
        ledger_known=sub.ledger_known,
        mirror_known=sub.mirror_known,
        first_build=first.as_dict() if first else {},
        lag_hours=fb.lag_hours(sub.since, first.check_start_time) if first else None,
        later_builds=later,
        window_hours=fb.WINDOW_HOURS,
        candidates_checked=checked,
        candidates_total=len(candidates),
        own_builds=[b.as_dict() for b in own[-8:]],
        containing_branches=sub.branches,
        containing_namespaces=contained,
        nearest_namespace=nearest,
        note=VIEW_NOTE if qa_ok else "",
        tracker_namespace=tracker_ns,
        tracker_unavailable=bool(tracker_ns) and not qa_ok,
        trace_run_id=trace_run_id,
    )
