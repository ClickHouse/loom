"""A person adds a repository index from the page
(code.repo.add / code.repo.add_branch); the registry merges with the
operator's config; the worker clones and builds the first index."""

from __future__ import annotations

import json
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

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        params = params or {}
        if "loom.code_repos" in sql:
            return {"n": self.count}
        if "loom.namespaces" in sql:
            return {"one": 1} if params.get("ns") in self.taken else None
        return None

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
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
        # (org, namespace, key) -> value, what get_state answers
        stored: dict[tuple[str, str, str], str] = {}

        async def set_state(
            self, org: str, ns: str, key: str, value: str, detail: str = ""
        ) -> None:
            _CodeRepo.states.append((org, ns, key, value))
            _CodeRepo.stored[(org, ns, key)] = value

        async def get_state(self, org: str, ns: str, key: str) -> str:
            return _CodeRepo.stored.get((org, ns, key), "")

        async def get_state_or_none(self, org: str, ns: str, key: str) -> str | None:
            return _CodeRepo.stored.get((org, ns, key))

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
async def test_code_repo_add_refusals_are_sentences(plane) -> None:
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
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add(
            CodeRepoAddInput(org=ORG, repo="acme/fourth", branch="main")
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
    with pytest.raises(ValidationError) as ei:
        await admin.handle_code_repo_add_branch(
            CodeRepoAddBranchInput(org=ORG, namespace="not-a-repo", branch="x")
        )
    assert ei.value.to_detail()["code"] == "NOT_A_REPO"


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
    assert links.tracker_namespaces_for(ORG, "code-widgets") == ["tracker-w"]
    assert links.tracker_namespaces_for(ORG, "code-widgets-release-1-2") == []


@pytest.mark.asyncio
async def test_a_branch_of_a_cleared_source_copies_no_qa_binding(plane, links) -> None:
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

    await admin.handle_code_repo_add(
        CodeRepoAddInput(org=ORG, repo="acme/widgets", branch="main", join_policy="open")
    )
    plane.stored[(ORG, "code-widgets", links.OVERRIDE_KEY)] = "tracker-w"
    real = code_repo_mod.CodeRepo.set_state

    async def _boom(self, org, ns, key, value, detail=""):
        if key == links.OVERRIDE_KEY:
            raise RuntimeError("down")
        return await real(self, org, ns, key, value, detail)

    monkeypatch.setattr(code_repo_mod.CodeRepo, "set_state", _boom)
    out = await admin.handle_code_repo_add_branch(
        CodeRepoAddBranchInput(org=ORG, namespace="code-widgets", branch="release/1.2")
    )
    assert out.namespace == "code-widgets-release-1-2"
    assert _link_rows(plane) == []  # the link write failed, so no qa_branch_of after it


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
