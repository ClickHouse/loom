"""code.repo.add / code.repo.add_branch — a person adds a repository index.

The operator's LOOM_CODE_REPOS_JSON keeps working; these write the same entry as a row of
loom.code_repos, register the directory row (kind code, the join rule chosen, visibility
private), make the caller the first maintainer, and leave the ingest worker to clone and
build the first index a slice per pass (code/app/index/bootstrap.py).

Refusals are sentences a person can act on, never a 500: bad names, a Git remote that
will not answer with the credential given, a branch that does not exist, a namespace already
taken, an org past its quota.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from server.auth.context import current_caller
from server.code.api.models_repos import (
    CodeRepoAddBranchInput,
    CodeRepoAddInput,
    CodeRepoAddOutput,
    CodeRepoRemoteBranchesOutput,
    CodeRepoRemoveInput,
    CodeRepoRemoveOutput,
    CodeRepoSetAccessOutput,
    CodeRepoSetSourceOutput,
)
from server.code.app.index import repo_registry as reg
from server.code.domain.index.repos import registry_repos
from server.core.errors import AuthInsufficientError, ValidationError
from server.settings import settings


def _require_self_service() -> None:
    if not settings.code_self_service_enabled:
        raise ValidationError(
            "adding repositories from the page is off on this deployment "
            "(LOOM_CODE_SELF_SERVICE_ENABLED); ask the operator",
            code="SELF_SERVICE_OFF",
        )


async def _require_sandbox() -> None:
    """A repository someone adds from the page gets compiler tiers that run its
    own build code, and its clone talks to a host they chose: both need the
    repository-job sandbox (code/app/index/sandbox.py), so without it the add
    is refused. The first call probes (runs bwrap), so off the event loop."""
    from server.code.app.index.sandbox import sandbox_available

    if not await asyncio.to_thread(sandbox_available):
        message = (
            "adding repositories or branches from the page needs the repository-job "
            "sandbox, which this deployment cannot run (`loom doctor` says why); ask the "
            "operator"
        )
        # With the sandbox required, operator entries are skipped too.
        if not settings.code_sandbox_required:
            message += ", who can list the repository in LOOM_CODE_REPOS_JSON instead"
        raise ValidationError(message, code="SANDBOX_UNAVAILABLE")


def _require_same_org(org: str) -> None:
    from server.auth.authz import may_act_in_org
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is not None and not may_act_in_org(auth, org):
        raise AuthInsufficientError(f"principal cannot act in org {org!r}")


async def _namespace_taken(org: str, namespace: str) -> bool:
    from server.auth.default_namespace import auth_off_subject
    from server.code.domain.index.repos import repo_for_namespace
    from server.identity.app.registration import namespace_claimable

    if repo_for_namespace(org, namespace) is not None:
        return True
    subject = current_caller()[0] or auth_off_subject()
    return not await namespace_claimable(org, namespace, subject)


async def _make_maintainer(org: str, namespace: str, subject: str) -> None:
    from server.identity.app.registration import grant_self_admin

    await grant_self_admin(org, namespace, subject)
    # The repository's other maintainers get this branch and
    # the creator gets the repository's other branches.
    from server.code.app.index import maintainers

    await maintainers.sync_quietly(org, namespace)


async def _register(
    *,
    org: str,
    namespace: str,
    repo: str,
    git_remote: str,
    provider: str,
    branch: str,
    classification: str,
    join_policy: str,
    github_token: str,
    path_prefixes: list[str],
    exclude: list[str],
    remote_head: str,
    exclude_generated: bool = True,
    scip_producer: str = "",
) -> CodeRepoAddOutput:
    from server.auth.default_namespace import auth_off_subject
    from server.code.app.index.bootstrap import set_stage
    from server.code.app.index.registry import ensure_registered
    from server.code.store.index import CodeRepo

    subject = current_caller()[0] or auth_off_subject()
    await reg.insert_entry(
        org=org,
        namespace=namespace,
        repo=repo,
        git_remote=git_remote,
        branch=branch,
        classification=classification,
        join_policy=join_policy,
        github_token=github_token,
        path_prefixes=path_prefixes,
        exclude=exclude,
        created_by=subject,
        exclude_generated=exclude_generated,
        scip_producer=scip_producer,
    )
    await reg.refresh_db_repos(force=True)
    cfg = next(
        (c for c in registry_repos() if c.org == org and c.namespace == namespace),
        None,
    )
    if cfg is not None:
        await ensure_registered(cfg)
    if subject:
        await _make_maintainer(org, namespace, subject)
    code_repo = CodeRepo()
    await set_stage(code_repo, org, namespace, "queued", remote_head=remote_head[:12])
    return CodeRepoAddOutput(
        org=org,
        namespace=namespace,
        repo=repo,
        remote=git_remote,
        provider=provider,
        branch=branch,
        join_policy=join_policy,
        classification=classification,
        index_stage="queued",
        my_role="admin" if subject else "",
        remote_head=remote_head,
    )


def _indexed_branches(org: str, repo: str) -> list[dict[str, str]]:
    """The branches of `repo` already indexed in `org` (config + registry)."""
    from server.code.domain.index.repos import configured_repos

    out = [
        {"branch": c.git_branch, "namespace": c.namespace}
        for c in configured_repos()
        if c.org == org and c.repo.lower() == repo.lower()
    ]
    return sorted(out, key=lambda x: x["branch"])


async def handle_code_repo_remote_branches(input: Any) -> Any:
    """The branches a Git remote shows for a repository with the
    token given (none for a public one), the default branch, which look like
    release branches, and which are indexed here already — so the add dialog
    can offer the choice instead of one text field."""
    _require_self_service()
    await _require_sandbox()
    _require_same_org(input.org)
    try:
        repo, git_remote, provider = reg.normalize_remote(input.repo)
    except ValueError as e:
        raise ValidationError(str(e), code="BAD_REPO") from None
    token = input.access_token
    try:
        remote = await reg.remote_branches(repo, token, git_remote=git_remote)
    except ValueError as e:
        raise ValidationError(str(e), code="REMOTE_REFUSED") from None
    await reg.refresh_db_repos()
    indexed = _indexed_branches(input.org, repo)
    taken = {x["branch"] for x in indexed}
    ordered = remote["branches"]  # default, release newest-first, the rest
    q = str(getattr(input, "query", "") or "").strip().lower()
    limit = max(1, min(500, int(getattr(input, "limit", 60) or 60)))
    matching = [b for b in ordered if not q or q in b["name"].lower()]
    # the default and the indexed branches always ride, even past the limit
    # or outside the query, so the picker can show where the person stands
    keep = {remote["default_branch"], *taken}
    shown = [b for b in matching[:limit]]
    shown_names = {b["name"] for b in shown}
    shown += [b for b in ordered if b["name"] in keep and b["name"] not in shown_names]
    warnings = await _onboarding_warnings(provider)
    return CodeRepoRemoteBranchesOutput(
        org=input.org,
        repo=repo,
        remote=git_remote,
        provider=provider,
        warnings=warnings,
        default_branch=remote["default_branch"],
        branches=shown,
        suggested=[
            b["name"]
            for b in ordered
            if b["name"] != remote["default_branch"]
            and reg.RELEASE_BRANCH_RE.match(b["name"])
            and b["name"] not in taken
        ],
        indexed=indexed,
        cap=int(settings.code_self_service_max_repos) - await reg.active_count(input.org),
        total=len(ordered),
        matched=len(matching),
        truncated=len(matching) > len(shown),
        query=q,
    )


async def _onboarding_warnings(provider: str) -> list[str]:
    from server.code.app.index_status import _worker_passes_from_ledger

    try:
        passes = await _worker_passes_from_ledger()
    except Exception:  # noqa: BLE001 — settings still provide a conservative answer
        passes = {}
    warnings: list[str] = []
    if not settings.code_ingest_enabled and not (passes.get("code_ingest") or {}).get(
        "last_run_at"
    ):
        warnings.append(
            "No code_ingest worker is visible. The repository will stay queued until "
            "one is enabled."
        )
    if provider != "github":
        warnings.append(
            f"Source indexing works for {provider}; merged-PR history is currently GitHub-only."
        )
    return warnings


async def _add_extra_branches(
    input: CodeRepoAddInput,
    *,
    repo: str,
    git_remote: str,
    provider: str,
    primary: str,
    classification: str,
    join_policy: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The other branches chosen at add time, each its own
    index sharing the mirror. Refusals are per branch and never undo the
    ones already registered."""
    also: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for raw in input.branches:
        branch = raw.strip()
        if not branch or branch == primary or any(a["branch"] == branch for a in also):
            continue
        if not reg.valid_branch(branch):
            skipped.append({"branch": branch, "reason": "not a branch name git accepts"})
            continue
        namespace = reg.namespace_for(repo, branch)
        if await _namespace_taken(input.org, namespace):
            skipped.append({"branch": branch, "reason": f"already indexed as {namespace}"})
            continue
        if await reg.active_count(input.org) >= settings.code_self_service_max_repos:
            skipped.append({"branch": branch, "reason": "the org's repository quota is reached"})
            continue
        try:
            head = await reg.remote_has_branch(
                repo,
                branch,
                input.access_token,
                git_remote=git_remote,
            )
        except ValueError as e:
            skipped.append({"branch": branch, "reason": str(e)})
            continue
        out = await _register(
            org=input.org,
            namespace=namespace,
            repo=repo,
            git_remote=git_remote,
            provider=provider,
            branch=branch,
            classification=classification,
            join_policy=join_policy,
            github_token=input.access_token,
            path_prefixes=[p.strip() for p in input.path_prefixes if p.strip()],
            exclude=[p.strip() for p in input.exclude if p.strip()],
            remote_head=head,
            exclude_generated=bool(input.exclude_generated),
            scip_producer=str(input.scip_producer or ""),
        )
        also.append({"branch": branch, "namespace": out.namespace, "remote_head": out.remote_head})
    return also, skipped


async def handle_code_repo_add(input: CodeRepoAddInput) -> CodeRepoAddOutput:
    _require_self_service()
    await _require_sandbox()
    _require_same_org(input.org)
    try:
        repo, git_remote, provider = reg.normalize_remote(input.repo)
    except ValueError as e:
        raise ValidationError(str(e), code="BAD_REPO") from None
    token = input.access_token
    branch = input.branch.strip()
    if not branch:
        try:
            discovered = await reg.remote_branches(repo, token, git_remote=git_remote)
        except ValueError as e:
            raise ValidationError(str(e), code="REMOTE_REFUSED") from None
        branch = str(discovered.get("default_branch") or "")
        if not branch:
            raise ValidationError(
                "the remote did not advertise a default branch", code="BAD_BRANCH"
            )
    if not reg.valid_branch(branch):
        raise ValidationError(f"{branch!r} is not a branch name git accepts", code="BAD_BRANCH")
    classification = input.classification
    # A restricted repository is never open: a maintainer approves each reader.
    join_policy = "approval" if classification == "restricted" else input.join_policy
    namespace = reg.namespace_for(repo, branch)
    if await _namespace_taken(input.org, namespace):
        raise ValidationError(
            f"{repo} @ {branch} is already indexed here as {namespace}; join it instead",
            code="REPO_EXISTS",
        )
    if await reg.active_count(input.org) >= settings.code_self_service_max_repos:
        raise ValidationError(
            f"this org already holds {settings.code_self_service_max_repos} repository indexes; "
            "retire one or ask the operator to raise the limit",
            code="REPO_QUOTA",
        )
    try:
        head = await reg.remote_has_branch(repo, branch, token, git_remote=git_remote)
    except ValueError as e:
        raise ValidationError(str(e), code="REMOTE_REFUSED") from None
    out = await _register(
        org=input.org,
        namespace=namespace,
        repo=repo,
        git_remote=git_remote,
        provider=provider,
        branch=branch,
        classification=classification,
        join_policy=join_policy,
        github_token=token,
        path_prefixes=[p.strip() for p in input.path_prefixes if p.strip()],
        exclude=[p.strip() for p in input.exclude if p.strip()],
        remote_head=head,
        exclude_generated=bool(input.exclude_generated),
        scip_producer=str(input.scip_producer or ""),
    )
    if input.branches:
        out.also, out.skipped = await _add_extra_branches(
            input,
            repo=repo,
            git_remote=git_remote,
            provider=provider,
            primary=branch,
            classification=classification,
            join_policy=join_policy,
        )
    return out


def repo_add_audit_subject(input: CodeRepoAddInput, response: Any) -> dict[str, Any]:
    """What an add's audit row records: the request without the access token,
    whether one was given (`authenticated`), and what was registered (the branch
    a blank one resolved to, the effective join policy) with the namespaces the
    indexes got. `loom.audit_log` outlives the repository and must hold no secret."""
    subject = input.model_dump(mode="json", exclude={"access_token"})
    subject["authenticated"] = bool(input.access_token)
    for registered in ("repo", "branch", "join_policy", "classification", "namespace"):
        subject[registered] = response[registered]
    subject["also"] = [extra["namespace"] for extra in response.get("also", [])]
    return subject


async def handle_code_repo_remove(input: CodeRepoRemoveInput) -> CodeRepoRemoveOutput:
    """A maintainer retires a repository index. The transport's ADMIN gate on the namespace is
    who may call. Refuses an entry the operator's LOOM_CODE_REPOS_JSON owns — that one is
    retired by editing the deployment — and needs `confirm` equal to the namespace, so a
    mis-clicked op cannot purge an index. The rows leave through an erasure saga: a detached
    `namespace_list` purge over the one namespace, loom-only, checkpointed, resumed by
    erasure_recovery if the process dies, and proven by the residue census."""
    from server.code.app.index import retire
    from server.code.domain.index.repos import configured_repos, repo_for_namespace
    from server.core.ch import get_client
    from server.erasure.app.namespace_purge import erase_namespaces_detached
    from server.identity.api.models import NamespaceDeleteInput

    _require_same_org(input.org)
    if input.confirm.strip() != input.namespace:
        raise ValidationError(
            f"type the namespace ({input.namespace}) in `confirm` to retire it",
            code="CONFIRM_REQUIRED",
        )
    await reg.refresh_db_repos(force=True)
    cfg = repo_for_namespace(input.org, input.namespace)
    if cfg is None:
        raise ValidationError(f"{input.namespace} is not a repository index", code="NOT_A_REPO")
    if cfg.source != "registry":
        raise ValidationError(
            f"{input.namespace} comes from the deployment's LOOM_CODE_REPOS_JSON; remove the "
            "entry there and swap — the page cannot retire an operator-owned index",
            code="REPO_FROM_CONFIG",
        )
    ch = get_client()
    registry_off = await retire.deactivate_registry_row(ch, input.org, input.namespace)
    await reg.refresh_db_repos(force=True)
    from server.identity.app.namespace import handle_namespace_delete

    directory = await handle_namespace_delete(
        NamespaceDeleteInput(org=input.org, namespace=input.namespace)
    )
    tables = await retire.code_tables_keyed_by_namespace(ch)
    erasure_id = await erase_namespaces_detached(
        input.org,
        [input.namespace],
        requester=current_caller()[0] or "code.repo.remove",
        purge=True,
        external=False,
    )
    mirror_removed = await retire.remove_mirror_if_orphaned(cfg, configured_repos())
    return CodeRepoRemoveOutput(
        org=input.org,
        namespace=input.namespace,
        repo=cfg.repo,
        branch=cfg.git_branch,
        registry_deactivated=registry_off,
        directory_deactivated=bool(directory.deleted),
        purged_tables=len(tables),
        erasure_id=erasure_id,
        mirror_removed=mirror_removed,
    )


async def handle_code_repo_add_branch(input: CodeRepoAddBranchInput) -> CodeRepoAddOutput:
    _require_self_service()
    await _require_sandbox()
    _require_same_org(input.org)
    from server.code.domain.index.repos import repo_for_namespace

    await reg.refresh_db_repos()
    src: Any = repo_for_namespace(input.org, input.namespace)
    if src is None:
        raise ValidationError(f"{input.namespace} is not a repository index", code="NOT_A_REPO")
    branch = input.branch.strip()
    if not reg.valid_branch(branch):
        raise ValidationError(f"{branch!r} is not a branch name git accepts", code="BAD_BRANCH")
    namespace = reg.namespace_for(src.repo, branch)
    if await _namespace_taken(input.org, namespace):
        raise ValidationError(
            f"{src.repo} @ {branch} is already indexed here as {namespace}", code="REPO_EXISTS"
        )
    if await reg.active_count(input.org) >= settings.code_self_service_max_repos:
        raise ValidationError(
            f"this org already holds {settings.code_self_service_max_repos} repository indexes",
            code="REPO_QUOTA",
        )
    try:
        head = await reg.remote_has_branch(
            src.repo, branch, src.github_token, git_remote=src.git_remote
        )
    except ValueError as e:
        raise ValidationError(str(e), code="REMOTE_REFUSED") from None
    join_policy = (
        "approval"
        if src.classification == "restricted"
        else ("open" if getattr(src, "public", False) else "approval")
    )
    return await _register(
        org=input.org,
        namespace=namespace,
        repo=src.repo,
        git_remote=src.git_remote,
        provider=reg.normalize_remote(src.git_remote)[2],
        branch=branch,
        classification=src.classification,
        join_policy=join_policy,
        github_token=src.github_token,
        path_prefixes=list(src.path_prefixes),
        exclude=list(src.exclude),
        remote_head=head,
        exclude_generated=bool(getattr(src, "exclude_generated", True)),
        scip_producer=str(getattr(src, "scip_producer", "") or ""),
    )


# ---------------------------------------------------------------------------
# A maintainer sets a repository's access and its knowledge source
# ---------------------------------------------------------------------------

_CLASSIFICATIONS = ("standard", "restricted")
_JOIN_POLICIES = ("open", "approval", "closed")


async def _directory_row(ch: Any, org: str, namespace: str) -> dict[str, Any] | None:
    from server.identity.app.registration import namespace_row

    return await namespace_row(org, namespace, ch=ch)


async def handle_code_repo_set_access(input: Any) -> Any:
    """The repository's maintainer (the transport's ADMIN gate
    on the namespace) sets its classification and join policy. The decision
    lands as a registry row that wins over the deployment's entry
    (`repos._with_registry`) and as a new version of the directory row, so
    both authorization inputs agree; the classification cache is dropped so
    this process enforces at once, and every other process re-reads the row
    within `code_repos_refresh_seconds`. Widening restricted → standard
    needs `confirm` = namespace; a restricted index is never `open`."""
    from server.code.domain.index.repos import repo_for_namespace
    from server.core.ch import get_client
    from server.identity.app import namespace_classification as nc

    _require_same_org(input.org)
    cls = str(input.classification or "").strip().lower()
    pol = str(input.join_policy or "").strip().lower()
    if cls not in _CLASSIFICATIONS:
        raise ValidationError(
            f"classification must be one of {_CLASSIFICATIONS}", code="BAD_CLASSIFICATION"
        )
    if pol not in _JOIN_POLICIES:
        raise ValidationError(
            f"join_policy must be one of {_JOIN_POLICIES}", code="BAD_JOIN_POLICY"
        )
    if cls == "restricted" and pol == "open":
        raise ValidationError(
            "a restricted repository is never open — choose approval or closed",
            code="RESTRICTED_NOT_OPEN",
        )
    await reg.refresh_db_repos(force=True)
    cfg: Any = repo_for_namespace(input.org, input.namespace)
    if cfg is None:
        raise ValidationError(f"{input.namespace} is not a repository index", code="NOT_A_REPO")
    widening = str(cfg.classification) == "restricted" and cls == "standard"
    if widening and input.confirm.strip() != input.namespace:
        raise ValidationError(
            f"opening a restricted repository: type the namespace ({input.namespace}) in `confirm`",
            code="CONFIRM_REQUIRED",
        )
    ch = get_client()
    row = await _directory_row(ch, input.org, input.namespace)
    previous_policy = str((row or {}).get("join_policy") or ("open" if cfg.public else "approval"))
    subject, _org = current_caller()
    await reg.upsert_access(
        cfg, classification=cls, join_policy=pol, created_by=subject or "maintainer", ch=ch
    )
    await reg.refresh_db_repos(force=True, ch=ch)
    directory_updated = False
    if row is not None:
        new_row = dict(row)
        new_row.update(
            {
                "classification": cls,
                "join_policy": pol,
                "visibility": "private",
                "updated_at": datetime.now(UTC).replace(microsecond=0, tzinfo=None),
            }
        )
        await ch.insert("loom.namespaces", [new_row])
        directory_updated = True
    nc.reset()
    if cls == "restricted":
        nc.note_restricted(input.org, input.namespace)
    return CodeRepoSetAccessOutput(
        org=input.org,
        namespace=input.namespace,
        repo=str(cfg.repo),
        classification=cls,
        join_policy=pol,
        previous_classification=str(cfg.classification),
        previous_join_policy=previous_policy,
        directory_updated=directory_updated,
        effective_within_s=int(settings.code_repos_refresh_seconds),
    )


async def handle_code_repo_set_source(input: Any) -> Any:
    """The maintainer sets the trackers this repository reads, primary first
    (`CodeRepoSetSourceInput`). The links are the `tracker_binding` state row of the code
    namespace (comma-joined); `tracker_namespaces_for` reads it before the deployment's map."""
    from server.code.domain.index.repos import repo_for_namespace
    from server.code.store.index import CodeRepo
    from server.tracker.app import binding

    _require_same_org(input.org)
    await reg.refresh_db_repos(force=True)
    cfg = repo_for_namespace(input.org, input.namespace)
    if cfg is None:
        raise ValidationError(f"{input.namespace} is not a repository index", code="NOT_A_REPO")
    await binding.refresh_overrides(force=True)
    previous = binding.tracker_namespaces_for(input.namespace)
    wanted = binding.parse_binding(input.tracker_namespaces)
    targets = [await _tracker_or_refuse(input.org, qa) for qa in wanted]
    # A record reads only the repositories it names (`splitByChar('#', source_id)[1] IN
    # repos`), so a link to one that does not name this repository answers nothing — said, not
    # refused (a fork may mean to read its upstream's record)
    repo = cfg.repo
    missing = [
        (qa, t)
        for qa, t in zip(wanted, targets, strict=True)
        if repo not in tuple(getattr(t, "repos", ()) or ())
    ]
    subject, _org = current_caller()
    await CodeRepo().set_state(
        input.org,
        input.namespace,
        binding.OVERRIDE_KEY,
        binding.encode_binding(wanted),
        detail=f"tracker links set by {subject or 'maintainer'}",
    )
    await binding.refresh_overrides(force=True)
    return CodeRepoSetSourceOutput(
        org=input.org,
        namespace=input.namespace,
        tracker_namespaces=wanted,
        previous_tracker_namespaces=previous,
        effective_within_s=int(settings.code_repos_refresh_seconds),
        not_covering=[qa for qa, _ in missing],
        warning="; ".join(
            f"{qa} names {', '.join(getattr(t, 'repos', ()) or ()) or 'no repository'}, "
            f"not {repo}: its reads will not return {repo}'s issues and PRs"
            for qa, t in missing
        ),
    )


async def _tracker_or_refuse(org: str, qa: str) -> Any:
    """The tracker namespace `qa` names, or NOT_A_TRACKER_NAMESPACE."""
    from server.tracker.domain.namespaces import tracker_namespace

    if tracker_namespace(org, qa) is None:
        # A source registered a moment ago on another process is in the registry before this
        # process's snapshot (refresh cadence tracker_sources_refresh_seconds) — read it before
        # refusing
        from server.tracker.app import registry_sources

        await registry_sources.refresh_db_sources(force=True)
    target = tracker_namespace(org, qa)
    if target is None:
        raise ValidationError(
            f"{qa} is not a tracker namespace of {org} — register its source first "
            "(tracker.source.add)",
            code="NOT_A_TRACKER_NAMESPACE",
        )
    return target
