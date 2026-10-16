"""Handlers for `code.coverage.watch` / `code.coverage.unwatch`.

Two sides are authorized: viewer on the code namespace (the transport's
gate, asked again here for the MCP path) and writer on the inbox's owner
namespace, the rule `/v1/task/watch` applies to an inbox stream.
"""

from __future__ import annotations

import re
from typing import Any

from server.code.api.models_coverage_watch import (
    CodeCoverageUnwatchInput,
    CodeCoverageUnwatchOutput,
    CodeCoverageWatchInput,
    CodeCoverageWatchOutput,
)
from server.code.api.models_status import CodeCommitCoverage
from server.code.app import watches as svc
from server.code.app.common import require_code_intel, resolve_code_identity
from server.code.store import coverage_watches as repo
from server.core.errors import AuthInsufficientError, NotFoundError, ValidationError

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHORT_SHA = re.compile(r"^[0-9a-f]{7,39}$")


async def _authorize(org: str, ns: str, inbox: str) -> dict[str, Any]:
    """The inbox row, after both sides' checks."""
    from server.auth.authz import row_in_scope
    from server.operations.base import Role
    from server.tasks.app.inboxes import fetch_inbox

    if not await row_in_scope(org, ns, Role.VIEWER):
        raise AuthInsufficientError(f"not a viewer of code namespace {ns!r}")
    row = await fetch_inbox(org, inbox)
    # Someone who cannot even read the owner namespace gets the unknown-inbox answer,
    # so the refusal does not confirm the inbox exists; a reader short of writer
    # already sees the inbox and gets the 403.
    if row is None or not await row_in_scope(org, str(row["owner_namespace"]), Role.VIEWER):
        raise NotFoundError(f"inbox {inbox!r} is not registered")
    if not await row_in_scope(org, str(row["owner_namespace"]), Role.WRITER):
        raise AuthInsufficientError("not a writer on the inbox's owner namespace")
    return row


def _repo_cfg(org: str, ns: str) -> Any:
    from server.code.domain.index.repos import repo_for_namespace

    cfg = repo_for_namespace(org, ns)
    if cfg is None:
        raise NotFoundError(f"{org}/{ns} is not a configured code namespace")
    return cfg


async def _full_sha(cfg: Any, commit: str) -> str:
    """A 40-hex SHA as given; an abbreviation resolved through the mirror."""
    from server.code.app.index import gitrepo

    sha = commit.strip().lower()
    if _FULL_SHA.match(sha):
        return sha
    if not _SHORT_SHA.match(sha):
        raise ValidationError("commit must be 7..40 hex characters")
    from server.code.app.index.mirrors import history_dir

    git_dir, gap = await history_dir(cfg)
    if gap:
        raise ValidationError(f"commit {commit!r} not resolved ({gap}); pass the full 40-hex SHA")
    try:
        return await gitrepo.resolve_commit(git_dir, sha)
    except (ValueError, gitrepo.GitError) as e:
        raise ValidationError(
            f"commit {commit!r} is not in the mirror yet; pass the full 40-hex SHA"
        ) from e


def _caller_subject() -> str:
    from server.auth.context import current_auth

    auth = current_auth()
    return str(auth.subject) if auth is not None and auth.subject else ""


async def handle_code_coverage_watch(input: CodeCoverageWatchInput) -> CodeCoverageWatchOutput:
    """Ask for a task-inbox message when the index covers a commit, instead of polling for it."""
    from server.code.store.index import CodeRepo

    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    await _authorize(org, ns, input.inbox)
    cfg = _repo_cfg(org, ns)
    sha = await _full_sha(cfg, input.commit)
    watch = await repo.fetch_watch(org, ns, sha, input.inbox)
    if watch is None:
        watch = svc.new_watch(org, ns, sha, input.inbox, _caller_subject())
        await repo.insert_watch_version(watch)
    coverage = await svc.coverage_for(cfg, sha, await CodeRepo().all_state(org, ns))
    emitted = False
    if coverage.get("ready") and watch.delivered_at is None:
        emitted, watch = await svc.deliver(watch, coverage)
    return CodeCoverageWatchOutput(
        org=org,
        namespace=ns,
        commit=sha,
        inbox=input.inbox,
        ready=bool(coverage.get("ready")),
        emitted=emitted,
        delivered_at=watch.delivered_at,
        event_id=repo.event_id(org, ns, sha, input.inbox),
        commit_coverage=CodeCommitCoverage(**coverage),
    )


async def handle_code_coverage_unwatch(
    input: CodeCoverageUnwatchInput,
) -> CodeCoverageUnwatchOutput:
    """Cancel a coverage watch on a commit."""
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    await _authorize(org, ns, input.inbox)
    sha = await _full_sha(_repo_cfg(org, ns), input.commit)
    watch = await repo.fetch_watch(org, ns, sha, input.inbox)
    if watch is None:
        return CodeCoverageUnwatchOutput(commit=sha, inbox=input.inbox, removed=False)
    await repo.insert_watch_version(svc.next_watch_version(watch, is_deleted=1))
    return CodeCoverageUnwatchOutput(commit=sha, inbox=input.inbox, removed=True)
