"""Self-service repositories: loom.code_repos as a second source of ``RepoConfig``
beside LOOM_CODE_REPOS_JSON.

The environment variable is the operator's; this table is everyone's. A
person adds a repository (or a branch of one) from the Repositories page,
becomes its first maintainer, and the ingest worker clones and indexes it.
``configured_repos()`` stays synchronous — it is on the authz hot path — so
this module keeps an in-process snapshot refreshed by ``refresh_db_repos``
(boot and every ``code_repos_refresh_seconds`` on every process, every ingest
pass, every code.repos call, and at once after an add).

A private remote's token is stored encrypted (Fernet keyed from the JWT
secret with a domain separator) and never returned over the wire.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from server.code.domain.index.remote import parse_remote
from server.code.domain.index.repos import (
    RepoConfig,
    registry_repos,
    reset_skipped_for_tests,
    set_registry_repos,
)
from server.core.secrets_at_rest import seal, unseal
from server.settings import settings

logger = logging.getLogger("loom.code_index.repo_registry")

_LOADED_AT: float = -1e12
# A read of loom.code_repos has succeeded since the process started (or the last reset).
_EVER_LOADED = False
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,120}$")
_NS_MAX = 63


def valid_repo(repo: str) -> bool:
    try:
        parse_remote(repo)
    except ValueError:
        return False
    return True


def normalize_remote(repo: str) -> tuple[str, str, str]:
    spec = parse_remote(repo)
    return spec.repo, spec.url, spec.provider


def valid_branch(branch: str) -> bool:
    b = branch or ""
    return bool(_BRANCH_RE.match(b)) and ".." not in b and not b.endswith(".lock")


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def namespace_for(repo: str, branch: str) -> str:
    """``code-<name>`` for the repository's main line, ``code-<name>-<branch>``
    otherwise — the shape the operator's entries already use
    (code-clickhouse, code-clickhouse-26-8)."""
    name = _slug(repo.rsplit("/", 1)[-1])
    ns = f"code-{name}" if branch in ("master", "main") else f"code-{name}-{_slug(branch)}"
    return ns[:_NS_MAX].rstrip("-")


def git_dir_for(repo: str) -> str:
    """One bare mirror per repository, shared by its branches."""
    parts = repo.split("/")
    key = "__".join(_slug(part) for part in parts)
    return os.path.join(settings.code_repos_root, f"{key}.git")


def checkout_dir_for(namespace: str) -> str:
    """The entry's own compiler-tier working tree, beside the mirrors. An entry
    without one would fall back to `code_scip_checkout_dir`, the operator entry's
    checkout, and a tier pass would check this repository out over it."""
    return os.path.join(settings.code_repos_root, f"checkout-{namespace}")


def remote_for(repo: str) -> str:
    return parse_remote(repo).url


# --- token at rest -----------------------------------------------------------


_TOKEN_PURPOSE = "code_repos.token"


def encrypt_token(plain: str) -> str:
    return seal(_TOKEN_PURPOSE, plain)


def decrypt_token(enc: str) -> str:
    return unseal(_TOKEN_PURPOSE, enc)


# --- snapshot ---------------------------------------------------------------


def row_to_config(r: dict[str, Any]) -> RepoConfig:
    classification = str(r.get("classification") or "standard")
    restricted = classification == "restricted"
    policy = str(r.get("join_policy") or ("approval" if restricted else "open"))
    repo = str(r.get("repo") or "")
    namespace = str(r.get("namespace") or "")
    return RepoConfig(
        org=str(r.get("org") or ""),
        namespace=namespace,
        repo=repo,
        git_dir=git_dir_for(repo),
        git_remote=str(r.get("git_remote") or remote_for(repo)),
        git_branch=str(r.get("git_branch") or "master"),
        path_prefixes=tuple(str(p) for p in (r.get("path_prefixes") or ()) if str(p)),
        github_token=decrypt_token(str(r.get("github_token_enc") or "")),
        org_readable=False,
        scip_checkout_dir=checkout_dir_for(namespace),
        exclude=tuple(str(p) for p in (r.get("exclude") or ()) if str(p)),
        classification=classification,
        public=(policy == "open") and not restricted,
        source="registry",
        # The two indexing knobs; '' producer = inferred
        exclude_generated=bool(r.get("exclude_generated", False)),
        scip_producer=str(r.get("scip_producer") or ""),
        row_kind=str(r.get("row_kind") or ""),
    )


async def refresh_db_repos(*, force: bool = False, ch: Any | None = None) -> list[RepoConfig]:
    """Re-read loom.code_repos when the snapshot is older than
    ``code_repos_refresh_seconds`` (or ``force``) into the domain's registry
    snapshot. Never raises: a directory that cannot be read keeps the last snapshot."""
    if not settings.code_self_service_enabled:
        return []
    if force or time.monotonic() - _LOADED_AT >= settings.code_repos_refresh_seconds:
        await read_registry(ch)
    return registry_repos()


async def read_registry(ch: Any | None = None) -> bool:
    """Read loom.code_repos into the snapshot now. False when that read failed
    and the last snapshot was kept; True with self-service off."""
    global _LOADED_AT, _EVER_LOADED
    if not settings.code_self_service_enabled:
        return True
    try:
        from server.code.store.index import CodeRepo

        rows = await CodeRepo(ch).registry_rows()
        page = frozenset(
            (str(r["org"]), str(r["namespace"])) for r in rows if r.get("row_kind") == "page"
        )
        active = [r for r in rows if r.get("active") and r.get("namespace") and r.get("repo")]
        set_registry_repos([row_to_config(r) for r in active], page)
        _LOADED_AT = time.monotonic()
        _EVER_LOADED = True
        return True
    except Exception as e:  # noqa: BLE001 — keep the last snapshot
        logger.warning(
            "code_repos: refresh failed, keeping %d entries: %s", len(registry_repos()), e
        )
        return False


def registry_read() -> bool:
    """The snapshot holds what loom.code_repos held at some read, or there is no registry
    (self-service off): False while every read since the process started failed."""
    return not settings.code_self_service_enabled or _EVER_LOADED


async def start_refresher(ch: Any | None = None) -> asyncio.Task[None]:
    """Every process loads the registry before it serves and re-reads it every
    ``code_repos_refresh_seconds``, so an added repository or a new restriction
    reaches every replica within that time, whichever ops it serves. The same timer
    re-reads the failing code-index tiers the health problem reports."""
    from server.code.app.index import tier_health

    await refresh_db_repos(force=True, ch=ch)
    await tier_health.refresh_failing(ch)

    async def _loop() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("code.repos_refresh")
        while True:
            await asyncio.sleep(max(1.0, float(settings.code_repos_refresh_seconds)))
            await refresh_db_repos(force=True, ch=ch)  # never raises
            await tier_health.refresh_failing(ch)  # never raises

    return asyncio.create_task(_loop(), name="loom-code-repos-refresh")


def reset_for_tests() -> None:
    global _LOADED_AT, _EVER_LOADED
    set_registry_repos([], frozenset())
    reset_skipped_for_tests()
    _LOADED_AT = -1e12
    _EVER_LOADED = False


async def row_active(org: str, namespace: str, ch: Any | None = None) -> bool:
    """Whether the namespace's loom.code_repos row is active, read now (not the snapshot)."""
    from server.code.store.index import CodeRepo
    from server.core.ch import sequential_reads

    with sequential_reads():
        row = await CodeRepo(ch).code_repo_row(org, namespace)
    return bool(row and row.get("active"))


async def active_count(org: str, ch: Any | None = None) -> int:
    from server.code.store.index import CodeRepo

    return await CodeRepo(ch).active_code_repo_count(org)


async def insert_entry(
    *,
    org: str,
    namespace: str,
    repo: str,
    branch: str,
    classification: str,
    join_policy: str,
    github_token: str,
    path_prefixes: list[str],
    exclude: list[str],
    created_by: str,
    git_remote: str = "",
    exclude_generated: bool = True,
    scip_producer: str = "",
    ch: Any | None = None,
) -> None:
    from datetime import UTC, datetime

    from server.code.store.index import CodeRepo

    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    await CodeRepo(ch).insert_code_repo_row(
        {
            "org": org,
            "namespace": namespace,
            "repo": repo,
            "git_remote": git_remote or remote_for(repo),
            "git_branch": branch,
            "path_prefixes": list(path_prefixes),
            "exclude": list(exclude),
            "exclude_generated": bool(exclude_generated),
            "scip_producer": str(scip_producer or ""),
            "row_kind": "page",
            "classification": classification,
            "join_policy": join_policy,
            "github_token_enc": encrypt_token(github_token),
            "created_by": created_by,
            "active": True,
            "created_at": now,
            "updated_at": now,
            "version": time.time_ns(),
        }
    )


async def upsert_access(
    cfg: Any,
    *,
    classification: str,
    join_policy: str,
    created_by: str,
    ch: Any | None = None,
) -> None:
    """A maintainer's access row for `cfg`'s namespace — a new version of the registry
    row carrying the chosen classification and join policy. An existing registry row is
    copied whole (every column, including ones added by later migrations) with only
    those two fields, `created_by` and the stamps changed, so a member's add stays one.
    An operator entry with no active row gets an access row built from the repository
    as configured, which `_with_registry` lets win over the entry."""
    from datetime import UTC, datetime

    from server.code.store.index import CodeRepo
    from server.core.ch import sequential_reads

    repo = CodeRepo(ch)
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    with sequential_reads():  # a lagging read must not replace a row with a new one
        current = await repo.code_repo_row(cfg.org, cfg.namespace)
    # An inactive row (a removed branch) is not brought back to life.
    if current and current.get("active"):
        row = dict(current)
    elif cfg.source != "config" or (current or {}).get("row_kind") == "page":
        # A removed branch added from the page: an operator entry is never listed for
        # its namespace, where that member keeps their grants.
        from server.core.errors import ValidationError

        raise ValidationError(f"{cfg.namespace} was removed", code="NOT_A_REPO")
    else:
        row = {
            "org": cfg.org,
            "namespace": cfg.namespace,
            "repo": cfg.repo,
            "git_remote": cfg.git_remote or remote_for(cfg.repo),
            "git_branch": cfg.git_branch,
            "path_prefixes": list(cfg.path_prefixes),
            "exclude": list(cfg.exclude),
            "github_token_enc": encrypt_token(cfg.github_token),
            "created_at": now,
            "row_kind": "access",
        }
    row.update(
        {
            "classification": classification,
            "join_policy": join_policy,
            "created_by": created_by,
            "active": True,
            "updated_at": now,
            "version": time.time_ns(),
        }
    )
    await repo.insert_code_repo_row(row)


RELEASE_BRANCH_RE = re.compile(r"^(v?\d+\.\d+(\.\d+)?(\.x)?|(release|releases|stable|rel)[/-].+)$")


def version_key(name: str) -> tuple[int, ...]:
    """The numbers in a branch name, for a newest-first order of release
    branches (`26.8` > `26.7` > `19.10`; `release/1.2.3` by 1, 2, 3)."""
    return tuple(int(x) for x in re.findall(r"\d+", name))


def parse_ls_remote(text: str) -> dict[str, Any]:
    """`git ls-remote --symref <remote> HEAD 'refs/heads/*'` → the default branch (from
    the `ref: refs/heads/<b>\tHEAD` symref line) and every head as {name, sha}: default
    first, then release-shaped NEWEST first (by the numbers in the name — 26.8 before
    19.10), then the rest alphabetical (a large repository has thousands of heads)."""
    default = ""
    heads: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("ref: "):
            m = re.match(r"^ref: refs/heads/(\S+)\s+HEAD$", line)
            if m:
                default = m.group(1)
            continue
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            heads[parts[1][len("refs/heads/") :]] = parts[0]

    def rank(name: str) -> tuple[int, Any, str]:
        if name == default:
            return (0, (), name)
        if RELEASE_BRANCH_RE.match(name):
            # newest first: negate each number so the tuple sorts ascending
            return (1, tuple(-x for x in version_key(name)), name)
        return (2, (), name)

    branches = [{"name": n, "sha": heads[n]} for n in sorted(heads, key=rank)]
    return {"default_branch": default, "branches": branches}


async def _ls_remote(
    repo: str,
    token: str,
    flag: str,
    *refs: str,
    timeout_s: float,
    git_remote: str = "",
) -> str:
    """`git ls-remote <flag> <remote> <refs>` against the repository's remote: its
    stdout, or ValueError with a sentence a person can act on. Authenticates through the
    environment (the token never reaches argv); the child never outlives the call."""
    from server.code.app.index.gitrepo import auth_env, remote_access
    from server.code.app.index.sandbox import run_job

    remote = git_remote or remote_for(repo)
    provider = parse_remote(remote).provider
    try:
        done = await run_job(
            ["git", "ls-remote", flag, remote, *refs],
            env=auth_env(token, remote),
            timeout_s=timeout_s,
            keep_stdout=True,
            **remote_access(remote),
        )
    except TimeoutError:
        raise ValueError(f"{provider} remote did not answer in time; try again") from None
    out, err = done.stdout, done.stderr
    if done.returncode != 0:
        text = err.decode(errors="replace").lower()
        if "not found" in text or "authentication" in text or "could not read" in text:
            raise ValueError(
                f"{provider} remote refused {repo}: the repository does not exist, "
                "or it is private "
                "and the token has no access to it"
            )
        raise ValueError(f"git ls-remote failed for {repo}")
    return out.decode(errors="replace")


async def remote_branches(
    repo: str, token: str, *, git_remote: str = "", timeout_s: float = 25.0
) -> dict[str, Any]:
    """Every branch of the remote with its head sha, and the default branch (chosen at
    add time). Raises ValueError with a sentence a person can act on; the token never
    reaches argv."""
    out = await _ls_remote(
        repo,
        token,
        "--symref",
        "HEAD",
        "refs/heads/*",
        timeout_s=timeout_s,
        git_remote=git_remote,
    )
    parsed = parse_ls_remote(out)
    if not parsed["branches"]:
        raise ValueError(f"{repo} has no branches")
    return parsed


async def remote_has_branch(
    repo: str,
    branch: str,
    token: str,
    *,
    git_remote: str = "",
    timeout_s: float = 25.0,
) -> str:
    """Return the branch's remote sha, or raise ValueError with a sentence a person can
    act on. Authenticates through the environment: the token never reaches argv."""
    out = await _ls_remote(
        repo,
        token,
        "--heads",
        f"refs/heads/{branch}",
        timeout_s=timeout_s,
        git_remote=git_remote,
    )
    line = out.strip().splitlines()
    if not line:
        raise ValueError(f"{repo} has no branch named {branch!r}")
    return line[0].split()[0]
