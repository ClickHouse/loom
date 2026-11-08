"""A person adds a repository index from the page
(code.repo.add / code.repo.add_branch); the registry merges with the
operator's config; the worker clones and builds the first index."""

from __future__ import annotations

import asyncio
import json
import os
import pwd
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import server.code.app.index.repo_registry as reg
import server.code.app.repo_admin as admin
import server.code.domain.index.repos as repos
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.code.api.models_repos import CodeRepoAddBranchInput, CodeRepoAddInput
from server.core.errors import ValidationError
from server.operations.base import Role

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

ORG = "clickhouse"


# --- names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "branch", "ns"),
    [
        ("ClickHouse/ClickHouse", "master", "code-clickhouse"),
        ("ClickHouse/ClickHouse", "26.8", "code-clickhouse-26-8"),
        ("ClickHouse/example-private", "main", "code-example-private"),
        ("acme/My.Repo_X", "feature/foo-bar", "code-my-repo-x-feature-foo-bar"),
    ],
)
def test_namespace_for_matches_the_operator_shape(repo: str, branch: str, ns: str) -> None:
    assert reg.namespace_for(repo, branch) == ns


def test_repo_and_branch_validation() -> None:
    assert reg.valid_repo("ClickHouse/ClickHouse")
    assert not reg.valid_repo("ClickHouse")
    assert not reg.valid_repo("../etc/passwd")
    assert not reg.valid_repo("a/b/c")
    assert reg.valid_branch("release/26.8")
    assert not reg.valid_branch("-flag")
    assert not reg.valid_branch("a..b")
    assert not reg.valid_branch("x.lock")


def test_git_dir_is_one_mirror_per_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_repos_root", "var/code_repos/self")
    assert (
        reg.git_dir_for("ClickHouse/ClickHouse") == "var/code_repos/self/clickhouse__clickhouse.git"
    )
    assert reg.git_dir_for("ClickHouse/ClickHouse") == reg.git_dir_for("clickhouse/clickhouse")


# --- token at rest -------------------------------------------------------------------


def test_token_round_trips_and_is_not_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    enc = reg.encrypt_token("ghp_abc123")
    assert enc and "ghp_abc123" not in enc
    assert reg.decrypt_token(enc) == "ghp_abc123"
    assert reg.encrypt_token("") == "" and reg.decrypt_token("") == ""


def test_rotated_secret_makes_the_token_unusable_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", "one")
    enc = reg.encrypt_token("ghp_abc123")
    monkeypatch.setattr(settings, "jwt_secret", "two")
    assert reg.decrypt_token(enc) == ""


# --- registry rows become RepoConfig, merged after the operator's entries -------------


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "org": ORG,
        "namespace": "code-acme",
        "repo": "acme/acme",
        "git_remote": "",
        "git_branch": "main",
        "path_prefixes": ["src/"],
        "exclude": [],
        "classification": "standard",
        "join_policy": "open",
        "github_token_enc": "",
    }
    base.update(kw)
    return base


def test_row_to_config_is_a_registry_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_repos_root", "var/code_repos/self")
    cfg = reg.row_to_config(_row())
    assert cfg.source == "registry" and cfg.public is True and cfg.org_readable is False
    assert cfg.git_remote == "https://github.com/acme/acme.git"
    assert cfg.git_dir == "var/code_repos/self/acme__acme.git"
    assert cfg.scip_checkout_dir.endswith(
        "/checkout-" + cfg.namespace
    )  # its own tree, never the operator's
    restricted = reg.row_to_config(_row(classification="restricted", join_policy="approval"))
    assert restricted.public is False and restricted.classification == "restricted"


def test_configured_repos_appends_registry_and_operator_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_repos_json", json.dumps([{"namespace": "code-acme"}]))
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    reg.reset_for_tests()
    monkeypatch.setattr(
        repos,
        "_registry_repos",
        [
            reg.row_to_config(_row()),
            reg.row_to_config(_row(namespace="code-other", repo="acme/other")),
        ],
    )
    got = {c.namespace: c for c in repos.configured_repos()}
    assert got["code-acme"].source == "config", "the operator's entry wins on a collision"
    assert got["code-other"].source == "registry"
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    assert [c.namespace for c in repos.configured_repos()] == ["code-acme"]
    reg.reset_for_tests()


# --- handlers ---------------------------------------------------------------------------


class _CH:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []
        self.taken: set[str] = set()
        self.count = 0
        self.heads: dict[str, str] = {"code-clickhouse": "a" * 40}  # namespace -> git_head

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        params = params or {}
        if "AS in_use" in sql:  # the in-use read: a name taken is in use
            return {"in_use": 1} if params.get("ns") in self.taken else None
        if "loom.code_repos" in sql:
            return {"n": self.count}
        if "loom.code_index_state" in sql:
            head = self.heads.get(str(params.get("ns"))) if params.get("key") == "git_head" else ""
            return {"state_value": head} if head else None
        if "loom.namespaces" in sql:
            return {"one": 1} if params.get("ns") in self.taken else None
        return None

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "loom.namespaces" in sql:  # the restricted directory rows
            return [
                r
                for t, rows in self.inserted
                for r in rows
                if t == "loom.namespaces" and r["classification"] == "restricted"
            ]
        return [
            dict(r, **{"classification": "standard"})
            for _t, rows in self.inserted
            for r in rows
            if _t == "loom.code_repos"
        ]

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserted.append((table, rows))


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch):
    import server.core.ch as ch_mod
    from server.identity.store import grants as grants_repo
    from server.settings import settings

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"namespace": "code-clickhouse", "repo": "ClickHouse/ClickHouse"}]),
    )
    from server.code.app.index import sandbox

    monkeypatch.setattr(sandbox, "_available", True)
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(settings, "code_self_service_max_repos", 3)
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    reg.reset_for_tests()

    async def _remote_ok(repo: str, branch: str, token: str, **_: Any) -> str:
        if token == "no-access":
            raise ValueError(f"{repo} is not readable with this token")
        if branch == "nope":
            raise ValueError(f"{repo} has no branch named {branch!r}")
        return "abc123def456" + "0" * 28

    monkeypatch.setattr(reg, "remote_has_branch", _remote_ok)
    grants: list[dict[str, Any]] = []

    async def _insert_grant(row: dict[str, Any], **_: Any) -> None:
        grants.append(row)

    monkeypatch.setattr(grants_repo, "insert_row", _insert_grant)

    async def _registered(cfg: Any, **_: Any) -> bool:
        ch.inserted.append(
            (
                "loom.namespaces",
                [
                    {
                        "namespace": cfg.namespace,
                        "join_policy": "open" if cfg.public else "approval",
                        "classification": cfg.classification,
                    }
                ],
            )
        )
        return True

    import server.code.app.index.registry as registry

    monkeypatch.setattr(registry, "ensure_registered", _registered)

    import server.code.store.index as code_repo_mod

    class _CodeRepo(code_repo_mod.CodeRepo):
        """The real store (its registry statements run on `ch`), state writes recorded."""

        states: list[tuple[str, str, str, str]] = []
        # (org, namespace, key) -> value, what the state reads answer
        stored: dict[tuple[str, str, str], str] = {}

        async def set_state(
            self, org: str, ns: str, key: str, value: str, detail: str = ""
        ) -> None:
            _CodeRepo.states.append((org, ns, key, value))
            _CodeRepo.stored[(org, ns, key)] = value

        async def get_state_or_none(self, org: str, ns: str, key: str) -> str | None:
            if (org, ns, key) in _CodeRepo.stored:
                return _CodeRepo.stored[(org, ns, key)]
            return await super().get_state_or_none(org, ns, key)  # `ch`: the git_head rows

    monkeypatch.setattr(code_repo_mod, "CodeRepo", _CodeRepo)
    _CodeRepo.states.clear()
    _CodeRepo.stored.clear()
    set_current_auth(
        AuthInfo(
            subject="alice@x",
            org=ORG,
            namespaces=("personal",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    yield SimpleNamespace(ch=ch, grants=grants, states=_CodeRepo.states, stored=_CodeRepo.stored)
    set_current_auth(None)
    reg.reset_for_tests()


@pytest.mark.asyncio
async def test_code_repo_add_registers_and_makes_the_caller_maintainer(plane) -> None:
    out = await admin.handle_code_repo_add(
        CodeRepoAddInput(
            org=ORG, repo="https://github.com/acme/widgets.git", branch="main", join_policy="open"
        )
    )
    assert (
        out.namespace == "code-widgets" and out.index_stage == "queued" and out.my_role == "admin"
    )
    tables = [t for t, _ in plane.ch.inserted]
    assert "loom.code_repos" in tables and "loom.namespaces" in tables
    row = next(r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows)
    assert (
        row["repo"] == "acme/widgets"
        and row["created_by"] == "alice@x"
        and row["github_token_enc"] == ""
    )
    assert (
        plane.grants
        and plane.grants[0]["role"] == "admin"
        and plane.grants[0]["subject"] == "alice@x"
    )
    assert (ORG, "code-widgets", "bootstrap_stage", "queued") in plane.states
    # the registry snapshot now carries it, so configured_repos() does too
    assert any(
        c.namespace == "code-widgets" and c.source == "registry" for c in repos.configured_repos()
    )


@pytest.mark.asyncio
async def test_code_repo_add_restricted_is_never_open_and_stores_the_token_encrypted(plane) -> None:
    out = await admin.handle_code_repo_add(
        CodeRepoAddInput(
            org=ORG,
            repo="acme/secret",
            branch="main",
            join_policy="open",
            classification="restricted",
            access_token="ghp_zzz",
        )
    )
    assert out.join_policy == "approval" and out.classification == "restricted"
    row = next(r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows)
    assert row["github_token_enc"] and "ghp_zzz" not in row["github_token_enc"]


@pytest.mark.asyncio
async def test_code_repo_add_refusals_are_sentences(plane, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(CodeRepoAddInput(org=ORG, repo="not-a-repo"))
    assert ei.value.to_detail()["code"] == "BAD_REPO"
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="nope")
        )
    assert ei.value.to_detail()["code"] == "REMOTE_REFUSED"
    with pytest.raises(ValidationError) as ei:  # already the operator's entry
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="ClickHouse/ClickHouse", branch="master")
        )
    assert ei.value.to_detail()["code"] == "REPO_EXISTS"
    plane.ch.taken.add("code-taken")
    with pytest.raises(ValidationError) as ei:  # a memory namespace of that name
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="acme/taken", branch="main")
        )
    assert ei.value.to_detail()["code"] == "REPO_EXISTS"
    plane.ch.count = 3

    async def no_remote_job(*_a: Any, **_k: Any) -> str:
        raise AssertionError("a full org is refused before any remote job")

    monkeypatch.setattr(reg, "remote_has_branch", no_remote_job)
    monkeypatch.setattr(reg, "remote_branches", no_remote_job)
    for branch in ("main", ""):  # a blank branch would ask the remote for its default
        with pytest.raises(ValidationError) as ei:
            await admin.handle_code_repo_add(
                CodeRepoAddInput(org=ORG, repo="acme/fourth", branch=branch, branches=["dev"])
            )
        assert ei.value.to_detail()["code"] == "REPO_QUOTA"


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True], ids=["sandbox-optional", "sandbox-required"])
async def test_code_repo_add_is_refused_without_the_sandbox(plane, monkeypatch, required) -> None:
    """A self-registered repository's build tiers run its own code, so without the
    repository-job sandbox nothing is registered and the remote is never asked. The
    refusal offers LOOM_CODE_REPOS_JSON only where operator entries still index."""
    from server.code.app.index import sandbox
    from server.settings import settings

    monkeypatch.setattr(sandbox, "_available", False)
    monkeypatch.setattr(settings, "code_sandbox_required", required)
    for call in (
        admin.handle_code_repo_add(CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main")),
        admin.handle_code_repo_remote_branches(CodeRepoAddInput(org=ORG, repo="acme/widgets")),
    ):
        with pytest.raises(ValidationError) as ei:
            await call
        detail = ei.value.to_detail()
        assert detail["code"] == "SANDBOX_UNAVAILABLE"
        assert "ask the operator" in detail["message"]
        assert ("LOOM_CODE_REPOS_JSON" in detail["message"]) == (not required)
    assert plane.ch.inserted == []


@pytest.mark.asyncio
async def test_the_sandbox_probe_runs_off_the_event_loop(plane, monkeypatch) -> None:
    """The first probe runs bwrap for up to 20 s; on the loop it would stall every
    request the process serves."""
    import threading

    from server.code.app.index import sandbox

    threads: list[threading.Thread] = []

    def probe() -> bool:
        threads.append(threading.current_thread())
        return False

    monkeypatch.setattr(sandbox, "sandbox_available", probe)
    with pytest.raises(ValidationError):
        await admin.handle_code_repo_add(CodeRepoAddInput(org=ORG, repo="acme/w", branch="main"))
    assert threads and threads[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_code_repo_add_refuses_another_org(plane) -> None:
    from server.core.errors import AuthInsufficientError

    with pytest.raises(AuthInsufficientError):
        await admin.handle_code_repo_add(CodeRepoAddInput(org="acme", repo="acme/widgets"))


@pytest.mark.asyncio
async def test_code_repo_add_branch_inherits_the_repository(plane) -> None:
    await admin.handle_code_repo_add(
        CodeRepoAddInput(
            org=ORG, repo="acme/widgets", branch="main", join_policy="open", path_prefixes=["src/"]
        )
    )
    out = await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.2")
    )
    assert out.namespace == "code-widgets-release-1-2" and out.join_policy == "open"
    rows = [r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows]
    assert rows[-1]["path_prefixes"] == ["src/"] and rows[-1]["git_branch"] == "release/1.2"
    assert [r["row_kind"] for r in rows] == ["page", "page"]
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add_branch(
            CodeRepoAddBranchInput(org=ORG, namespace="not-a-repo", branch="x")
        )
    assert ei.value.to_detail()["code"] == "NOT_A_REPO"


@pytest.mark.asyncio
async def test_a_branch_added_from_an_operator_entry_is_recorded_as_one(plane) -> None:
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    (row,) = [r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows]
    assert row["row_kind"] == "operator"


def _link_rows(plane: Any) -> list[tuple[str, str, str, str]]:
    from server.tracker.app import binding

    keys = (binding.OVERRIDE_KEY, binding.BRANCH_OF_KEY)
    return [r for r in plane.states if r[2] in keys]


@pytest.fixture
def links(monkeypatch):
    from server.tracker.app import binding

    async def _no_refresh(**_: Any) -> None:
        return None

    monkeypatch.setattr(binding, "refresh_overrides", _no_refresh)
    binding.reset_overrides_for_tests()
    yield binding
    binding.reset_overrides_for_tests()


@pytest.mark.asyncio
async def test_a_branch_of_an_operator_repository_follows_the_operator_map(
    plane, links, monkeypatch
) -> None:
    """The operator's glob links only operator entries; a branch added through
    self-service is a registry entry that names its source, and reads the source's
    links when read, so a glob the operator removes stops linking it too."""
    from server.settings import settings

    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-clickhouse*": "tracker-ch"}'
    )
    out = await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    assert out.namespace == "code-clickhouse-26-8"
    rows = _link_rows(plane)
    assert rows == [(ORG, "code-clickhouse-26-8", links.BRANCH_OF_KEY, "code-clickhouse")]
    branch = "code-clickhouse-26-8"
    assert links.tracker_namespaces_for(ORG, branch) == []  # the glob alone does not reach it
    links.reset_overrides_for_tests(branches={(o, n): v for o, n, _k, v in rows})
    assert links.tracker_namespaces_for(ORG, branch) == ["tracker-ch"]
    assert links.tracker_namespaces_for("other-org", branch) == []
    monkeypatch.setattr(settings, "tracker_code_namespace_bindings_json", "")
    assert links.tracker_namespaces_for(ORG, branch) == []


@pytest.mark.asyncio
async def test_a_branch_copies_its_self_service_source_s_maintainer_link(plane, links) -> None:
    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )
    plane.stored[(ORG, "code-widgets", links.OVERRIDE_KEY)] = "tracker-w,tracker-desk"
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.2")
    )
    assert _link_rows(plane) == [
        (ORG, "code-widgets-release-1-2", links.OVERRIDE_KEY, "tracker-w,tracker-desk"),
    ]  # no `qa_branch_of`: a self-service root is never followed


@pytest.mark.asyncio
async def test_a_branch_of_a_self_service_source_ignores_an_operator_entry_of_its_name(
    plane, links, monkeypatch
) -> None:
    """The operator may later configure the self-service source's name; its branch, whose
    admin never needed the operator entry's admin, must not start reading the map."""
    from server.settings import settings

    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.2")
    )
    assert _link_rows(plane) == []
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-w"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG, "namespace": "code-widgets", "repo": "acme/widgets"}]),
    )
    # The entry is skipped: the member's namespace stays theirs and reads no map.
    assert links.tracker_namespaces_for(ORG, "code-widgets") == []
    assert links.tracker_namespaces_for(ORG, "code-widgets-release-1-2") == []


@pytest.mark.asyncio
async def test_a_branch_of_a_cleared_operator_source_copies_no_qa_binding(plane, links) -> None:
    plane.stored[(ORG, "code-clickhouse", links.OVERRIDE_KEY)] = ""
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    assert [r[2] for r in _link_rows(plane)] == [links.BRANCH_OF_KEY]
    # the branch reads the source, whose cleared row closes the glob for both
    links.reset_overrides_for_tests(
        {(ORG, "code-clickhouse"): ""}, branches={(ORG, "code-clickhouse-26-8"): "code-clickhouse"}
    )
    assert links.tracker_namespaces_for(ORG, "code-clickhouse-26-8") == []


@pytest.mark.asyncio
async def test_a_failed_link_write_leaves_the_branch_registered(plane, links, monkeypatch) -> None:
    import server.code.store.index as code_repo_mod

    # a branch of the operator root with links of its own, then a branch of that branch
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    plane.stored[(ORG, "code-clickhouse-26-8", links.OVERRIDE_KEY)] = "tracker-w"
    real = code_repo_mod.CodeRepo.set_state

    async def _boom(self, org, ns, key, value, detail=""):
        if key == links.OVERRIDE_KEY:
            raise RuntimeError("down")
        return await real(self, org, ns, key, value, detail)

    monkeypatch.setattr(code_repo_mod.CodeRepo, "set_state", _boom)
    out = await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse-26-8", branch="26.9")
    )
    assert out.namespace == "code-clickhouse-26-9"
    # the copy failed, so no qa_branch_of after it: the branch does not follow the root
    assert [r for r in _link_rows(plane) if r[1] == "code-clickhouse-26-9"] == []


@pytest.mark.asyncio
async def test_a_failed_source_read_leaves_the_branch_off_the_operator_root(
    plane, links, monkeypatch
) -> None:
    """B1 is a branch of an operator repository; reading B1's own row fails while B2 is
    added from it. qa_branch_of is written last, so B2 does not follow the root."""
    import server.code.store.index as code_repo_mod
    from server.settings import settings

    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-clickhouse*": "tracker-ch"}'
    )
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )

    real = code_repo_mod.CodeRepo.get_state_or_none

    async def _boom(self, org: str, ns: str, key: str) -> str | None:
        if ns == "code-clickhouse-26-8" and key == links.OVERRIDE_KEY:
            raise RuntimeError("down")  # B1's own row, read for B2
        return await real(self, org, ns, key)

    monkeypatch.setattr(code_repo_mod.CodeRepo, "get_state_or_none", _boom)
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse-26-8", branch="26.9")
    )
    rows = _link_rows(plane)
    assert [r for r in rows if r[1] == "code-clickhouse-26-9"] == []
    links.reset_overrides_for_tests(branches={(o, n): v for o, n, k, v in rows})
    assert links.tracker_namespaces_for(ORG, "code-clickhouse-26-9") == []


@pytest.mark.asyncio
async def test_a_cleared_self_service_source_copies_its_empty_row(plane, links) -> None:
    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )
    plane.stored[(ORG, "code-widgets", links.OVERRIDE_KEY)] = ""
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.2")
    )
    assert _link_rows(plane) == [
        (ORG, "code-widgets-release-1-2", links.OVERRIDE_KEY, ""),
    ]  # no `qa_branch_of`: a self-service root is never followed


@pytest.mark.asyncio
async def test_a_branch_of_an_unlinked_repository_copies_no_qa_binding(
    plane, links, monkeypatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "tracker_code_namespace_bindings_json", "")
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    assert [r[2] for r in _link_rows(plane)] == [links.BRANCH_OF_KEY]
    links.reset_overrides_for_tests(branches={(ORG, "code-clickhouse-26-8"): "code-clickhouse"})
    assert links.tracker_namespaces_for(ORG, "code-clickhouse-26-8") == []


@pytest.mark.asyncio
async def test_a_clear_on_the_operator_source_reaches_its_branch(plane, links) -> None:
    """An operator source's own link is followed, never copied: a copy would win over
    following, and a later clear on the source would not reach the branch."""
    plane.stored[(ORG, "code-clickhouse", links.OVERRIDE_KEY)] = "tracker-x"
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    branch = "code-clickhouse-26-8"
    assert [r[2] for r in _link_rows(plane)] == [links.BRANCH_OF_KEY]
    rows = {(ORG, branch): "code-clickhouse"}
    links.reset_overrides_for_tests({(ORG, "code-clickhouse"): "tracker-x"}, branches=rows)
    assert links.tracker_namespaces_for(ORG, branch) == ["tracker-x"]
    links.reset_overrides_for_tests({(ORG, "code-clickhouse"): ""}, branches=rows)
    assert links.tracker_namespaces_for(ORG, branch) == []


@pytest.mark.asyncio
async def test_a_branch_of_a_branch_names_the_root_repository(plane, links) -> None:
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse-26-8", branch="26.9")
    )
    assert [r for r in _link_rows(plane) if r[2] == links.BRANCH_OF_KEY] == [
        (ORG, "code-clickhouse-26-8", links.BRANCH_OF_KEY, "code-clickhouse"),
        (ORG, "code-clickhouse-26-9", links.BRANCH_OF_KEY, "code-clickhouse"),
    ]


@pytest.mark.asyncio
async def test_a_branch_of_a_cleared_branch_stays_cleared(plane, links, monkeypatch) -> None:
    """B1 follows the operator root; its maintainer clears it; B2 added from B1 keeps
    B1's cleared row instead of following the root again."""
    from server.settings import settings

    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-clickhouse*": "tracker-ch"}'
    )
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    plane.stored[(ORG, "code-clickhouse-26-8", links.OVERRIDE_KEY)] = ""
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse-26-8", branch="26.9")
    )
    b2 = [r for r in _link_rows(plane) if r[1] == "code-clickhouse-26-9"]
    assert b2 == [
        (ORG, "code-clickhouse-26-9", links.OVERRIDE_KEY, ""),
        (ORG, "code-clickhouse-26-9", links.BRANCH_OF_KEY, "code-clickhouse"),
    ]
    links.reset_overrides_for_tests(
        {(ORG, "code-clickhouse-26-9"): ""},
        branches={(ORG, "code-clickhouse-26-9"): "code-clickhouse"},
    )
    assert links.tracker_namespaces_for(ORG, "code-clickhouse-26-9") == []


@pytest.mark.asyncio
async def test_an_operator_source_s_own_branch_row_does_not_pick_the_root(plane, links) -> None:
    plane.stored[(ORG, "code-clickhouse", links.BRANCH_OF_KEY)] = "code-elsewhere"
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    assert _link_rows(plane) == [
        (ORG, "code-clickhouse-26-8", links.BRANCH_OF_KEY, "code-clickhouse")
    ]


def _last_rows(plane: Any, namespace: str) -> tuple[dict[Any, str], dict[Any, str]]:
    """The snapshot the store would hold for `namespace`: the last row per key."""
    from server.tracker.app import binding

    over: dict[Any, str] = {}
    branches: dict[Any, str] = {}
    for org, ns, key, value in plane.states:
        if ns != namespace:
            continue
        if key == binding.OVERRIDE_KEY:
            over[(org, ns)] = value
        elif key == binding.BRANCH_OF_KEY:
            branches[(org, ns)] = value
    return over, branches


@pytest.mark.asyncio
async def test_a_branch_re_added_under_a_removed_name_drops_the_old_rows(
    plane, links, monkeypatch
) -> None:
    """A removal on an earlier build writes no clear, and the purge is detached: the old
    namespace's rows are still there when the same name is registered again."""
    from server.settings import settings

    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-clickhouse*": "tracker-ch"}'
    )
    branch = "code-clickhouse-26-8"
    plane.stored[(ORG, branch, links.BRANCH_OF_KEY)] = "code-old"
    plane.stored[(ORG, branch, links.OVERRIDE_KEY)] = "tracker-old"
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
    )
    over, branches = _last_rows(plane, branch)
    assert over == {(ORG, branch): ""} and branches == {(ORG, branch): "code-clickhouse"}
    links.reset_overrides_for_tests(
        {(ORG, branch): "tracker-old"} | over, branches={(ORG, branch): "code-old"} | branches
    )
    assert links.tracker_namespaces_for(ORG, branch) == []  # never the old link


@pytest.mark.asyncio
async def test_a_repository_re_added_under_a_removed_name_drops_the_old_rows(plane, links) -> None:
    plane.stored[(ORG, "code-widgets", links.BRANCH_OF_KEY)] = "code-clickhouse"
    plane.stored[(ORG, "code-widgets", links.OVERRIDE_KEY)] = "tracker-old"
    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )
    over, branches = _last_rows(plane, "code-widgets")
    assert over == {(ORG, "code-widgets"): ""} and branches == {(ORG, "code-widgets"): ""}


@pytest.mark.asyncio
async def test_a_fresh_name_writes_no_clear(plane, links) -> None:
    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )
    assert _link_rows(plane) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["", None])
async def test_a_re_add_after_a_removal_on_another_replica_drops_the_stale_snapshot_rows(
    plane, stored
) -> None:
    """Another replica removed the name: its rows are emptied ("") or already purged
    (None) in the store, but this replica's snapshot still holds the old link and the
    operator root. Registering the name drops both from the snapshot."""
    from server.tracker.app import binding

    binding.reset_overrides_for_tests(
        {(ORG, "code-widgets"): "tracker-old"},
        branches={(ORG, "code-widgets"): "code-clickhouse"},
    )
    if stored is not None:
        plane.stored[(ORG, "code-widgets", binding.BRANCH_OF_KEY)] = stored
        plane.stored[(ORG, "code-widgets", binding.OVERRIDE_KEY)] = stored
    try:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
        )
        assert _link_rows(plane) == []  # nothing holds a value, so nothing is written
        assert binding.override_for(ORG, "code-widgets") is None
        assert (ORG, "code-widgets") not in binding._BRANCH_OF
    finally:
        binding.reset_overrides_for_tests()


@pytest.mark.asyncio
async def test_the_link_reads_around_a_registration_read_their_own_writes(
    plane, links, monkeypatch
) -> None:
    """A link the source's maintainer cleared a moment ago must not come back from a
    lagging ClickHouse replica: the stale-row check and the copy read with sequential
    consistency."""
    import server.code.store.index as code_repo_mod
    from server.core import ch as ch_mod

    seen: list[tuple[str, bool]] = []
    repo_cls = code_repo_mod.CodeRepo
    real_get, real_or_none = repo_cls.get_state, repo_cls.get_state_or_none

    async def _get(self: Any, org: str, ns: str, key: str) -> str:
        seen.append((key, ch_mod.sequential_reads_active()))
        return await real_get(self, org, ns, key)

    async def _or_none(self: Any, org: str, ns: str, key: str) -> str | None:
        seen.append((key, ch_mod.sequential_reads_active()))
        return await real_or_none(self, org, ns, key)

    monkeypatch.setattr(repo_cls, "get_state", _get)
    monkeypatch.setattr(repo_cls, "get_state_or_none", _or_none)
    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )
    await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.2")
    )
    link_reads = [on for key, on in seen if key in (links.OVERRIDE_KEY, links.BRANCH_OF_KEY)]
    assert link_reads and all(link_reads)


@pytest.mark.asyncio
async def test_a_branch_of_an_entry_restricted_by_its_directory_row_is_restricted(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry's own classification says standard and it is public, but its
    directory row restricts it: the new branch is restricted and not open."""
    from server.auth.directory import ClassificationUnavailable
    from server.core.errors import BackendUnavailableError
    from server.identity.app import namespace_classification as nc

    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )
    directory = {"state": "restricted"}

    async def is_restricted(org: str, namespace: str) -> bool:
        if directory["state"] == "unreadable":
            raise ClassificationUnavailable(org)
        return directory["state"] == "restricted" and namespace == "code-widgets"

    monkeypatch.setattr(nc, "is_restricted", is_restricted)
    out = await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.2")
    )
    assert out.join_policy == "approval"
    rows = [r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows]
    assert rows[-1]["classification"] == "restricted"
    assert rows[-1]["join_policy"] == "approval"
    # restricted by a row newer than this process's snapshot
    directory["state"] = "stale"

    async def fresh_row(_ch: Any, org: str, namespace: str) -> dict[str, Any]:
        return {"org": org, "namespace": namespace, "classification": "restricted"}

    monkeypatch.setattr(admin, "_directory_row", fresh_row)
    out = await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.4")
    )
    rows = [r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows]
    assert out.join_policy == "approval" and rows[-1]["classification"] == "restricted"
    directory["state"] = "unreadable"

    async def no_remote_call(*_a: Any, **_k: Any) -> str:
        raise AssertionError("an unreadable classification refuses before the remote")

    monkeypatch.setattr(reg, "remote_has_branch", no_remote_call)
    with pytest.raises(BackendUnavailableError):
        await admin.handle_code_repo_add_branch(
            CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.3")
        )
    # an unreadable directory row refuses the same way
    directory["state"] = "stale"

    async def unreadable_row(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise BackendUnavailableError("directory down")

    monkeypatch.setattr(admin, "_directory_row", unreadable_row)
    registered = len(plane.ch.inserted)
    with pytest.raises(BackendUnavailableError):
        await admin.handle_code_repo_add_branch(
            CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.5")
        )
    assert len(plane.ch.inserted) == registered  # nothing registered


@pytest.mark.asyncio
async def test_a_branch_of_a_closed_entry_is_closed(plane, monkeypatch: pytest.MonkeyPatch) -> None:
    """A maintainer closed the repository with `code.repo.set_access`: its new
    branch takes no join requests either."""
    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )

    async def closed_row(_ch: Any, org: str, namespace: str) -> dict[str, Any]:
        return {"org": org, "namespace": namespace, "join_policy": "closed"}

    monkeypatch.setattr(admin, "_directory_row", closed_row)
    out = await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.2")
    )
    assert out.join_policy == "closed"
    rows = [r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows]
    assert rows[-1]["join_policy"] == "closed"


@pytest.mark.asyncio
async def test_an_extra_branch_past_the_quota_runs_no_remote_job(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary branch takes the org's last slot; the extra branch is skipped for
    the quota before any remote job is spent on it."""

    async def active_count(org: str) -> int:
        # 0 before the primary branch is registered, the quota once it is
        registered = [r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows]
        return 3 if registered else 0

    asked: list[str] = []

    async def remote_ok(repo: str, branch: str, token: str, **_: Any) -> str:
        asked.append(branch)
        return "abc123def456" + "0" * 28

    monkeypatch.setattr(reg, "active_count", active_count)
    monkeypatch.setattr(reg, "remote_has_branch", remote_ok)
    out = await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", branches=["dev"])
    )
    assert asked == ["main"]
    assert out.skipped == [{"branch": "dev", "reason": "the org's repository quota is reached"}]


@pytest.mark.asyncio
async def test_add_sees_an_entry_another_replica_registered_moments_ago(plane) -> None:
    """The duplicate check reads the registry afresh: an entry written by another
    replica inside the refresh interval is found under another spelling."""
    await admin.handle_code_repo_add(CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main"))
    plane.ch.inserted.append(
        ("loom.code_repos", [_row(namespace="code-gadget", repo="acme/gadget")])
    )
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="https://www.github.com/acme/gadget", branch="main")
        )
    assert ei.value.to_detail()["code"] == "REPO_EXISTS"


@pytest.mark.asyncio
async def test_add_branch_of_a_configured_entry_waits_for_its_first_index(plane) -> None:
    """Before the backfill the operator may still restrict the entry; a branch added
    then would be a standard self-service index of the same repository."""
    plane.ch.heads.clear()
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add_branch(
            CodeRepoAddBranchInput(org=ORG, namespace="code-clickhouse", branch="26.8")
        )
    assert ei.value.to_detail()["code"] == "REPO_NOT_INDEXED"
    assert not [t for t, _ in plane.ch.inserted if t == "loom.code_repos"]


@pytest.mark.asyncio
async def test_a_branch_already_indexed_under_another_name_is_refused(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    entry = {"namespace": "code-ch", "repo": "ClickHouse/ClickHouse", "git_branch": "master"}
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    reg.reset_for_tests()
    plane.ch.heads["code-ch"] = "a" * 40
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add_branch(
            CodeRepoAddBranchInput(org=ORG, namespace="code-ch", branch="master")
        )
    # the namespace that holds it may be restricted: the refusal does not name it
    assert ei.value.to_detail()["code"] == "REPO_EXISTS" and "code-ch" not in str(ei.value)
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="clickhouse/clickhouse", branch="master")
        )
    assert ei.value.to_detail()["code"] == "REPO_EXISTS" and "code-ch" not in str(ei.value)
    out = await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="ClickHouse/ClickHouse", branch="26.8", branches=["master"])
    )
    assert out.skipped == [{"branch": "master", "reason": "already indexed here"}]
    rows = [r for t, rows in plane.ch.inserted if t == "loom.code_repos" for r in rows]
    assert [r["git_branch"] for r in rows] == ["26.8"]


@pytest.mark.asyncio
async def test_a_configured_entry_on_another_host_is_matched_by_its_remote(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    entry = {
        "namespace": "code-internal-tool",
        "repo": "group/tool",
        "git_remote": "https://gitlab.com/group/tool.git",
        "git_branch": "main",
    }
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    reg.reset_for_tests()
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="https://gitlab.com/group/tool", branch="main")
        )
    assert ei.value.to_detail()["code"] == "REPO_EXISTS"


def _two_spellings(monkeypatch: pytest.MonkeyPatch, plane: Any) -> None:
    """Two operator entries of one repository, the second named freely."""
    from server.settings import settings

    entries = [
        {
            "namespace": "code-ch",
            "repo": "ClickHouse/ClickHouse",
            "git_remote": "https://github.com/ClickHouse/ClickHouse.git",
            "git_branch": "master",
        },
        {
            "namespace": "code-ch-lts",
            "repo": "clickhouse-lts",
            "git_remote": "https://www.github.com/clickhouse/clickhouse",
            "git_branch": "26.3",
        },
    ]
    monkeypatch.setattr(settings, "code_repos_json", json.dumps(entries))
    reg.reset_for_tests()
    plane.ch.heads.update({"code-ch": "a" * 40, "code-ch-lts": "b" * 40})


@pytest.mark.asyncio
async def test_add_branch_finds_a_branch_held_by_an_entry_spelled_differently(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_spellings(monkeypatch, plane)
    for ns, branch in (("code-ch", "26.3"), ("code-ch-lts", "master")):
        with pytest.raises(ValidationError) as ei:
            await admin.handle_code_repo_add_branch(
                CodeRepoAddBranchInput(org=ORG, namespace=ns, branch=branch)
            )
        assert ei.value.to_detail()["code"] == "REPO_EXISTS"
    assert not [t for t, _ in plane.ch.inserted if t == "loom.code_repos"]


@pytest.mark.asyncio
async def test_an_entry_is_known_by_its_remote_not_its_free_name(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry named `group/tool` whose remote is on another host is not the GitHub
    repository of that name."""
    from server.settings import settings

    entry = {
        "namespace": "code-internal-tool",
        "repo": "group/tool",
        "git_remote": "https://gitlab.com/group/tool.git",
        "git_branch": "main",
    }
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    reg.reset_for_tests()
    out = await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="group/tool", branch="main")
    )
    assert out.namespace == "code-tool"


@pytest.mark.asyncio
async def test_a_caller_who_cannot_read_the_remote_learns_nothing_of_the_index(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_spellings(monkeypatch, plane)
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(
                org=ORG, repo="ClickHouse/ClickHouse", branch="master", access_token="no-access"
            )
        )
    assert ei.value.to_detail()["code"] == "REMOTE_REFUSED"
    out = await admin.handle_code_repo_add(
        CodeRepoAddInput(
            org=ORG,
            repo="ClickHouse/ClickHouse",
            branch="26.8",
            branches=["master", "26.3", "26.7"],
            access_token="",
        )
    )
    held = [b for b in out.skipped if b["reason"] == "already indexed here"]
    assert [b["branch"] for b in held] == ["master", "26.3"]


@pytest.mark.asyncio
async def test_remote_branches_lists_a_branch_held_under_another_name_without_its_namespace(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker learns the branch is taken; the namespace that holds it is named only
    to a caller who may read it."""
    from types import SimpleNamespace as NS

    import server.auth.authz as authz

    _two_spellings(monkeypatch, plane)

    async def _branches(repo: str, token: str, **_: Any) -> dict[str, Any]:
        names = ["master", "26.3", "26.8"]
        return {"default_branch": "master", "branches": [{"name": n} for n in names]}

    monkeypatch.setattr(reg, "remote_branches", _branches)

    async def _in_scope(org: str, ns: str, *a: Any, **k: Any) -> bool:
        return ns == "code-ch"

    monkeypatch.setattr(authz, "row_in_scope", _in_scope)
    out = await admin.handle_code_repo_remote_branches(
        NS(org=ORG, repo="https://github.com/clickhouse/ClickHouse.git", access_token="")
    )
    assert out.indexed == [
        {"branch": "26.3", "namespace": ""},  # code-ch-lts: not readable by this caller
        {"branch": "master", "namespace": "code-ch"},
    ]
    assert "26.3" not in out.suggested


@pytest.mark.asyncio
async def test_the_entry_not_the_namespace_name_is_what_refuses(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`code-ch` is not the name `namespace_for` gives ClickHouse@master (`code-clickhouse`),
    so the derived name is free and only the entry check can refuse; its remote is
    spelled with a dot segment as well."""
    from server.settings import settings

    entry = {
        "namespace": "code-ch",
        "repo": "ch",
        "git_remote": "https://github.com/ClickHouse/./ClickHouse.git",
        "git_branch": "master",
    }
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    reg.reset_for_tests()
    plane.ch.heads["code-ch"] = "a" * 40
    assert not await admin._namespace_taken(
        ORG, reg.namespace_for("ClickHouse/ClickHouse", "master")
    )
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="ClickHouse/ClickHouse", branch="master")
        )
    assert "is already indexed here; join it instead" in str(ei.value)
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add_branch(
            CodeRepoAddBranchInput(org=ORG, namespace="code-ch", branch="master")
        )
    assert str(ei.value).endswith("is already indexed here")


@pytest.mark.asyncio
async def test_a_remote_check_runs_in_the_sandbox_without_the_servers_ssh_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sandbox builds the job: an empty HOME, no agent socket, and no mount of the
    server's HOME or a directory above it, so an ssh remote answers only to what the
    caller can reach, never to a key of the server's."""
    from server.code.app.index import sandbox
    from server.code.app.index.sandbox import JobResult

    seen: dict[str, Any] = {}

    async def _exec(*argv: Any, env: Any = None, **_: Any) -> Any:
        seen.update(argv=list(argv), env=dict(env))
        return SimpleNamespace()

    async def _result(proc: Any, job: Any) -> Any:
        return JobResult(0, b"abc123\trefs/heads/main\n", b"")

    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "_seccomp_fd", lambda: os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(sandbox, "_result", _result)
    remote = "ssh://git@github.com/acme/widget.git"
    assert await reg.remote_has_branch("acme/widget", "main", "", git_remote=remote) == "abc123"
    argv, env = seen["argv"], seen["env"]
    assert "ls-remote" in argv and remote in argv
    assert env["HOME"] == "/tmp/home"
    assert not {"SSH_AUTH_SOCK", "GIT_SSH", "SSH_ASKPASS"} & set(env)
    assert "ProxyCommand" in env["GIT_SSH_COMMAND"] and "-i " not in env["GIT_SSH_COMMAND"]
    assert any(argv[i : i + 2] == ["--dir", "/tmp/home"] for i in range(len(argv)))
    home = Path.home().absolute()
    sources = [Path(argv[i + 1]) for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
    # a python under HOME (a uv-managed one) may be mounted, never HOME or ~/.ssh; ssh
    # finds ~/.ssh through the user database, so its home counts as well
    passwd_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert sources
    for ssh in {home / ".ssh", passwd_home / ".ssh"}:
        assert not [src for src in sources if ssh.is_relative_to(src)]


@pytest.mark.asyncio
async def test_an_add_without_the_sandbox_is_refused(
    plane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index import sandbox

    monkeypatch.setattr(sandbox, "sandbox_available", lambda: False)
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main")
        )
    assert ei.value.to_detail()["code"] == "SANDBOX_UNAVAILABLE"
    assert not [t for t, _ in plane.ch.inserted if t == "loom.code_repos"]
