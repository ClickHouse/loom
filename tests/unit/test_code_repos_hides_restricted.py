"""code.repos: an entry the gate refused is
listed to a same-org caller as a request-only row (readable=False) whenever
its join rule lets that caller ask — open or approval — including a
restricted repository whose rule is approval. Closed entries and callers from
another org see nothing (flat ADMIN still sees a standard closed entry
flagged unreadable, the adopt path)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.code.app import index_status
from server.code.app import repos as repos_mod
from server.operations.base import Role
from tests.unit.code_fixtures import repo_config

ORG = "clickhouse"


def _cfg(ns: str, classification: str = "standard"):
    return repo_config(
        org=ORG,
        namespace=ns,
        repo="ClickHouse/x",
        git_branch="master",
        classification=classification,
        org_readable=False,
    )


@pytest.fixture
def plane(monkeypatch):
    import server.code.domain.index.repos as repos

    entries = [_cfg("code-clickhouse"), _cfg("code-example-private", "restricted")]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    monkeypatch.setattr(index_status, "require_code_intel", lambda: None)

    async def _status(cfg):
        return SimpleNamespace(
            org=cfg.org,
            namespace=cfg.namespace,
            repo=cfg.repo,
            branch=cfg.git_branch,
            git_head="h",
            clone_head="h",
            fast_tier_lag=0,
            scip_lag=0,
            scip_py_lag=0,
            tier_health=[],
            state={},
            last_refresh_at="",
            counts={},
        )

    monkeypatch.setattr(index_status, "_repo_status", _status)
    index_status.reset_repo_status_cache()  # Statuses are cached per (org, ns)

    async def _deny_all(auth, role, org=None, namespace=None, **_kw):
        raise PermissionError("cannot access")

    monkeypatch.setattr(authz, "check_authorized", _deny_all)
    policies = {
        ("clickhouse", "code-clickhouse"): "open",
        ("clickhouse", "code-example-private"): "approval",
    }

    async def _state(auth):
        return policies, {}, {}, set()

    monkeypatch.setattr(repos_mod, "_repo_directory_state", _state)
    nc.reset()
    yield entries, policies
    nc.reset()
    set_current_auth(None)


def _repos(out):
    return [(r.namespace, r.readable, r.join_policy) for r in out.repos]


@pytest.mark.asyncio
async def test_same_org_viewer_sees_both_as_request_only(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    set_current_auth(
        AuthInfo(
            subject="v@x",
            org=ORG,
            namespaces=("personal",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    assert _repos(out) == [
        ("code-clickhouse", False, "open"),
        ("code-example-private", False, "approval"),
    ]
    assert all(r.counts == {} and r.git_head == "" for r in out.repos), "request-only: no contents"
    # the class rides every row, so the Code view's pill never infers "open"
    # from the viewer's own standing
    assert [r.classification for r in out.repos] == ["standard", "restricted"]


@pytest.mark.asyncio
async def test_closed_restricted_entry_is_hidden_even_from_admin(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    _entries, policies = plane
    policies[("clickhouse", "code-example-private")] = "closed"
    set_current_auth(
        AuthInfo(
            subject="ops@x",
            org=ORG,
            namespaces=("*",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    assert _repos(out) == [("code-clickhouse", False, "open")]


@pytest.mark.asyncio
async def test_closed_standard_entry_is_hidden_from_a_viewer_but_flagged_for_admin(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    _entries, policies = plane
    policies[("clickhouse", "code-clickhouse")] = "closed"
    policies[("clickhouse", "code-example-private")] = "closed"
    set_current_auth(
        AuthInfo(
            subject="v@x",
            org=ORG,
            namespaces=("personal",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    assert (await repos_mod.handle_code_repos(CodeReposInput(org=ORG))).repos == []
    set_current_auth(
        AuthInfo(
            subject="ops@x",
            org=ORG,
            namespaces=("*",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    assert _repos(out) == [("code-clickhouse", False, "closed")]


@pytest.mark.asyncio
async def test_another_org_sees_nothing(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    set_current_auth(
        AuthInfo(
            subject="v@acme",
            org="acme",
            namespaces=("*",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    assert (await repos_mod.handle_code_repos(CodeReposInput(org=ORG))).repos == []


@pytest.mark.asyncio
async def test_a_refused_entry_computes_no_status(plane, monkeypatch) -> None:
    """The gate runs before the status warm-up: request-only and hidden entries
    never cost the git calls, counts reads and snapshot writes of a status."""
    from server.code.api.models_repos import CodeReposInput

    computed: list[str] = []

    async def _status(cfg):
        computed.append(cfg.namespace)
        raise AssertionError("a refused entry's status was computed")

    monkeypatch.setattr(index_status, "_repo_status", _status)
    for org in (ORG, "acme"):  # request-only rows, then another org (all hidden)
        set_current_auth(
            AuthInfo(
                subject="v@x",
                org=org,
                namespaces=("personal",),
                roles=(Role.VIEWER,),
                is_service_account=False,
            )
        )
        await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    assert computed == []


@pytest.mark.asyncio
async def test_directory_restricted_entry_with_approval_rule_is_listed_for_requests(plane) -> None:
    """The row, not the config, says restricted: same outcome as the config."""
    from server.code.api.models_repos import CodeReposInput

    entries, _policies = plane
    entries[1] = _cfg("code-example-private")
    nc._cache[ORG] = (1e12, frozenset({"code-example-private"}))
    set_current_auth(
        AuthInfo(
            subject="ops@x",
            org=ORG,
            namespaces=("*",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    assert [r.namespace for r in out.repos] == ["code-clickhouse", "code-example-private"]


def test_code_repos_is_authorized_at_org_level_not_on_the_default_code_namespace() -> None:
    """A principal who has not joined master must still be able to list what
    it may join: the transport gate on the default code namespace must not
    refuse the whole page."""
    from server.operations.registry import ALL_OPERATIONS

    op = next(o for o in ALL_OPERATIONS if o.name == "code.repos")
    assert op.skip_namespace_authz is True
