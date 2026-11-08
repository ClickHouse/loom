"""Branches are chosen at add time from what the Git remote shows.
`code.repo.remote_branches` lists a repository's heads (default first, then
release-shaped, then the rest), which are indexed here already and how many
more indexes the org may add; `code.repo.add` takes `branches` and registers
each as its own index sharing the mirror, refusing per branch without undoing
the ones registered."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import server.code.app.index.repo_registry as reg
import server.code.app.repo_admin as admin
import server.code.domain.index.repos as repos
from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo, Role
from server.code.api.models_repos import (
    CodeRepoAddInput,
    CodeRepoAddOutput,
    CodeRepoRemoteBranchesInput,
)
from server.code.api.ops import OPERATIONS
from server.core.errors import BackendTimeoutError, GrantBusyError, ValidationError
from server.settings import settings

ORG = "clickhouse"

LS_REMOTE = """ref: refs/heads/master\tHEAD
9a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b\tHEAD
1111111111111111111111111111111111111111\trefs/heads/26.7
2222222222222222222222222222222222222222\trefs/heads/26.8
3333333333333333333333333333333333333333\trefs/heads/feature/x
4444444444444444444444444444444444444444\trefs/heads/master
5555555555555555555555555555555555555555\trefs/heads/release/1.2
6666666666666666666666666666666666666666\trefs/heads/aaa
"""


# ---------------------------------------------------------------- the parse (pure)


def test_parse_ls_remote_orders_default_release_newest_first_rest_and_reads_the_symref() -> None:
    out = reg.parse_ls_remote(LS_REMOTE)
    assert out["default_branch"] == "master"
    # release branches newest first by the numbers in the name
    assert [b["name"] for b in out["branches"]] == [
        "master",
        "26.8",
        "26.7",
        "release/1.2",
        "aaa",
        "feature/x",
    ]
    assert out["branches"][0]["sha"].startswith("4444")
    assert reg.parse_ls_remote("") == {"default_branch": "", "branches": []}
    assert reg.version_key("26.10") > reg.version_key("26.9") > reg.version_key("19.18")
    many = "ref: refs/heads/master\tHEAD\n" + "\n".join(
        f"{'a' * 40}\trefs/heads/{n}"
        for n in ["19.10", "26.8", "20.1", "master", "26.10", "v1.2.3"]
    )
    assert [b["name"] for b in reg.parse_ls_remote(many)["branches"]] == [
        "master",
        "26.10",
        "26.8",
        "20.1",
        "19.10",
        "v1.2.3",
    ]


@pytest.mark.parametrize(
    "name,release",
    [
        ("26.8", True),
        ("v1.2", True),
        ("1.2.3", True),
        ("26.8.x", True),
        ("release/1.2", True),
        ("stable-2", True),
        ("master", False),
        ("feature/x", False),
        ("26", False),
        ("dev", False),
    ],
)
def test_release_branch_shape(name: str, release: bool) -> None:
    assert bool(reg.RELEASE_BRANCH_RE.match(name)) is release


# ---------------------------------------------------------------- the read op


def _cfg(namespace: str, branch: str, repo: str = "ClickHouse/ClickHouse") -> Any:
    return SimpleNamespace(
        org=ORG,
        namespace=namespace,
        repo=repo,
        git_remote="",
        git_branch=branch,
        classification="standard",
        public=True,
        github_token="",
        path_prefixes=(),
        exclude=(),
        source="config",
    )


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    from server.code.app.index import sandbox

    monkeypatch.setattr(sandbox, "_available", True)
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(settings, "code_self_service_max_repos", 10)
    token = current_auth_var.set(
        AuthInfo(
            subject="alice",
            org=ORG,
            namespaces=("code-clickhouse",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    yield
    current_auth_var.reset(token)


@pytest.mark.asyncio
async def test_remote_branches_names_default_suggested_indexed_and_cap(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def _remote(repo: str, token: str, **kw):
        seen.update(repo=repo, token=token)
        return reg.parse_ls_remote(LS_REMOTE)

    async def _refresh(**kw):
        return []

    async def _count(org: str, **kw):
        return 3

    async def _readable(org: str, ns: str, *a: Any, **k: Any) -> bool:
        return True  # the namespaces' scoping: test_code_repo_self_service.py

    monkeypatch.setattr(reg, "remote_branches", _remote)
    monkeypatch.setattr(reg, "refresh_db_repos", _refresh)
    monkeypatch.setattr(reg, "active_count", _count)
    monkeypatch.setattr("server.auth.authz.row_in_scope", _readable)
    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [
            _cfg("code-clickhouse", "master"),
            _cfg("code-clickhouse-26-8", "26.8"),
            _cfg("code-other", "main", repo="acme/other"),
        ],
    )
    out = await admin.handle_code_repo_remote_branches(
        CodeRepoRemoteBranchesInput(
            org=ORG, repo="https://github.com/ClickHouse/ClickHouse.git", access_token="ghp_x"
        )
    )
    assert seen == {"repo": "ClickHouse/ClickHouse", "token": "ghp_x"}
    assert out.repo == "ClickHouse/ClickHouse" and out.default_branch == "master"
    assert [b["name"] for b in out.branches][:3] == ["master", "26.8", "26.7"]
    # release-shaped, not the default, not already indexed — newest first
    assert out.suggested == ["26.7", "release/1.2"]
    assert out.indexed == [
        {"branch": "26.8", "namespace": "code-clickhouse-26-8"},
        {"branch": "master", "namespace": "code-clickhouse"},
    ]
    assert out.cap == 7
    assert out.total == 6 and out.matched == 6 and out.truncated is False and out.query == ""
    # a bounded page — the default and the indexed branches always ride
    page = await admin.handle_code_repo_remote_branches(
        CodeRepoRemoteBranchesInput(org=ORG, repo="ClickHouse/ClickHouse", limit=2)
    )
    assert [b["name"] for b in page.branches] == ["master", "26.8"]
    assert page.total == 6 and page.matched == 6 and page.truncated is True
    # a query narrows the page; default + indexed still ride so the picker can show standing
    found = await admin.handle_code_repo_remote_branches(
        CodeRepoRemoteBranchesInput(org=ORG, repo="ClickHouse/ClickHouse", query="FEAT")
    )
    assert [b["name"] for b in found.branches] == ["feature/x", "master", "26.8"]
    assert found.matched == 1 and found.truncated is False and found.query == "feat"


@pytest.mark.asyncio
async def test_non_github_remote_is_canonical_and_keeps_its_clone_url(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def _remote(repo: str, token: str, **kw):
        seen.update(repo=repo, token=token, **kw)
        return reg.parse_ls_remote(LS_REMOTE.replace("master", "main"))

    monkeypatch.setattr(reg, "remote_branches", _remote)
    monkeypatch.setattr(reg, "refresh_db_repos", AsyncMock(return_value=[]))
    monkeypatch.setattr(reg, "active_count", AsyncMock(return_value=0))
    monkeypatch.setattr(
        admin, "_onboarding_warnings", AsyncMock(return_value=["history unavailable"])
    )
    monkeypatch.setattr(repos, "configured_repos", lambda: [])
    out = await admin.handle_code_repo_remote_branches(
        CodeRepoRemoteBranchesInput(
            org=ORG,
            repo="https://gitlab.com/acme/platform/widget.git",
            access_token="secret",
        )
    )
    assert out.repo == "gitlab.com/acme/platform/widget"
    assert out.remote == "https://gitlab.com/acme/platform/widget.git"
    assert out.provider == "gitlab" and out.warnings == ["history unavailable"]
    assert seen["git_remote"] == out.remote and seen["token"] == "secret"


@pytest.mark.asyncio
async def test_add_without_branch_uses_remote_default(monkeypatch) -> None:
    registered: list[dict[str, Any]] = []
    monkeypatch.setattr(admin, "_namespace_taken", AsyncMock(return_value=False))
    monkeypatch.setattr(reg, "active_count", AsyncMock(return_value=0))
    monkeypatch.setattr(
        reg,
        "remote_branches",
        AsyncMock(return_value={"default_branch": "trunk", "branches": []}),
    )
    monkeypatch.setattr(reg, "remote_has_branch", AsyncMock(return_value="abc"))

    async def _register(**kw):
        registered.append(kw)
        return CodeRepoAddOutput(
            org=kw["org"],
            namespace=kw["namespace"],
            repo=kw["repo"],
            remote=kw["git_remote"],
            provider=kw["provider"],
            branch=kw["branch"],
            join_policy=kw["join_policy"],
            classification=kw["classification"],
        )

    monkeypatch.setattr(admin, "_register", _register)
    out = await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="https://git.example.com/acme/widget.git")
    )
    assert out.branch == "trunk" and out.namespace == "code-widget-trunk"
    assert registered[0]["git_remote"] == "https://git.example.com/acme/widget.git"


@pytest.mark.asyncio
async def test_remote_branches_refusals(monkeypatch) -> None:
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_remote_branches(
            CodeRepoRemoteBranchesInput(org=ORG, repo="not-a-repo")
        )
    assert ei.value.to_detail()["code"] == "BAD_REPO"

    async def _refused(repo: str, token: str, **kw):
        raise ValueError(
            "GitHub refused acme/private: the repository does not exist, or it is "
            "private and the token has no access to it"
        )

    monkeypatch.setattr(reg, "remote_branches", _refused)
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_remote_branches(
            CodeRepoRemoteBranchesInput(org=ORG, repo="acme/private")
        )
    assert ei.value.to_detail()["code"] == "REMOTE_REFUSED"
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_remote_branches(
            CodeRepoRemoteBranchesInput(org=ORG, repo="acme/app")
        )
    assert ei.value.to_detail()["code"] == "SELF_SERVICE_OFF"
    op = next(o for o in OPERATIONS if o.name == "code.repo.remote_branches")
    assert op.required_role == Role.VIEWER and op.skip_namespace_authz and op.idempotent


# ---------------------------------------------------------------- add with branches


@pytest.mark.asyncio
async def test_add_registers_every_chosen_branch_and_skips_per_branch(monkeypatch) -> None:
    registered: list[dict[str, Any]] = []
    taken = {"code-clickhouse-26-6"}
    count = {"n": 7}

    async def _taken(org: str, namespace: str) -> bool:
        return namespace in taken

    async def _count(org: str, **kw):
        return count["n"]

    async def _head(repo: str, branch: str, token: str, **kw):
        if branch == "ghost":
            raise ValueError("ClickHouse/ClickHouse has no branch named 'ghost'")
        return f"sha-{branch}"

    async def _register(**kw):
        if kw["branch"] == "26.5":
            raise GrantBusyError("another change to the account is running; retry shortly")
        if kw["branch"] == "26.3":
            raise BackendTimeoutError("a statement ran past its bound")
        if kw["branch"] == "26.4":
            raise ValidationError("code-clickhouse-26-4 is already in use", code="REPO_EXISTS")
        registered.append(kw)
        count["n"] += 1
        return CodeRepoAddOutput(
            org=kw["org"],
            namespace=kw["namespace"],
            repo=kw["repo"],
            branch=kw["branch"],
            join_policy=kw["join_policy"],
            classification=kw["classification"],
            index_stage="queued",
            remote_head=kw["remote_head"],
        )

    async def _refresh(**kw):
        return []

    monkeypatch.setattr(admin, "_namespace_taken", _taken)
    monkeypatch.setattr(admin, "_register", _register)
    monkeypatch.setattr(reg, "active_count", _count)
    monkeypatch.setattr(reg, "remote_has_branch", _head)
    monkeypatch.setattr(reg, "refresh_db_repos", _refresh)
    out = await admin.handle_code_repo_add(
        CodeRepoAddInput(
            org=ORG,
            repo="ClickHouse/ClickHouse",
            branch="master",
            join_policy="open",
            classification="standard",
            access_token="ghp_t",
            branches=["26.8", "26.5", "26.4", "26.3", "master", "26.6", "ghost", "..bad", "26.7"],
        )
    )
    # the primary, then 26.8 and 26.7; master is the primary (not twice);
    # 26.6 is indexed already; ghost is not on the remote; ..bad is not a branch
    assert out.branch == "master" and out.remote_head == "sha-master"
    assert [a["branch"] for a in out.also] == ["26.8", "26.7"]
    assert out.also[0] == {
        "branch": "26.8",
        "namespace": "code-clickhouse-26-8",
        "remote_head": "sha-26.8",
    }
    assert (
        {s["branch"]: s["reason"] for s in out.skipped}
        == {
            "26.6": "the namespace code-clickhouse-26-6 is taken",
            "26.5": "GRANT_BUSY: another change to the account is running; retry shortly",  # busy: a skip
            "26.4": "VALIDATION_FAILED: code-clickhouse-26-4 is already in use",  # taken during the probe
            # its claim's bound: it may be written, so it is not called a clean skip
            "26.3": (
                "code-clickhouse-26-3: BACKEND_TIMEOUT: a statement ran past its bound; it may be "
                "partly set up, check it before adding again"
            ),
            "ghost": "ClickHouse/ClickHouse has no branch named 'ghost'",
            "..bad": "not a branch name git accepts",
        }
    )
    assert [r["branch"] for r in registered] == ["master", "26.8", "26.7"]
    # every extra index inherits rule, token and paths from the add
    assert all(r["github_token"] == "ghp_t" and r["join_policy"] == "open" for r in registered)
    # the quota applies per branch: at 9 of 10 only the primary fits; every extra is refused
    count["n"] = 9
    out2 = await admin.handle_code_repo_add(
        CodeRepoAddInput(
            org=ORG,
            repo="acme/app",
            branch="main",
            join_policy="open",
            classification="standard",
            branches=["r1", "r2"],
        )
    )
    assert [a["branch"] for a in out2.also] == [] or len(out2.also) <= 1
    assert any("quota" in s["reason"] for s in out2.skipped)
