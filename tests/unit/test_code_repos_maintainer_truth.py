"""The repository row tells the truth about who maintains it.

`readable=False` does not mean "nobody maintains it": an org admin whose
password-login token names only their own memory namespaces cannot read a
restricted entry (a restricted namespace must be NAMED by the token) and yet
is its maintainer. Every row carries `has_maintainer` (any active admin grant
on the entry, fail-open True), the unreadable row carries `my_role`, and
`unreadable_reason` says why the gate refused in the vocabulary the Join /
Request buttons speak (token_scope | not_member | closed)."""

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
ME = "avery@x"
PRIVATE = "code-example-private"
MASTER = "code-clickhouse"
BRANCH = "code-clickhouse-26-1"


def _cfg(ns: str, classification: str = "standard"):
    return repo_config(
        org=ORG,
        namespace=ns,
        repo="ClickHouse/ClickHouse",
        git_branch="master",
        classification=classification,
        org_readable=False,
    )


@pytest.fixture
def plane(monkeypatch):
    """Three entries: master (admin = ME), the
    restricted private mirror (admin = ME), a release branch with viewers
    only. The gate is the real decision: master readable; private refused
    because the token does not name it; 26-1 refused because no grant."""
    import server.code.domain.index.repos as repos

    entries = [_cfg(MASTER), _cfg(PRIVATE, "restricted"), _cfg(BRANCH)]
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
            chunks_total=0,
            chunks_embedded=0,
            embedding_coverage=1.0,
        )

    monkeypatch.setattr(index_status, "_repo_status", _status)
    index_status.reset_repo_status_cache()  # statuses are cached per (org, ns)

    async def _gate(auth, role, org=None, namespace=None, **_kw):
        if namespace != MASTER:
            raise PermissionError("cannot access")

    monkeypatch.setattr(authz, "check_authorized", _gate)
    policies = {(ORG, MASTER): "open", (ORG, PRIVATE): "approval", (ORG, BRANCH): "open"}
    state = {
        "policies": policies,
        "standing": {(ORG, MASTER): "member", (ORG, PRIVATE): "member"},
        "roles": {(ORG, MASTER): "admin", (ORG, PRIVATE): "admin"},
        "maintained": {(ORG, MASTER), (ORG, PRIVATE)},
    }

    async def _state(auth):
        return state["policies"], state["standing"], state["roles"], state["maintained"]

    monkeypatch.setattr(repos_mod, "_repo_directory_state", _state)
    nc.reset()
    yield state
    nc.reset()
    set_current_auth(None)


def _login_token(namespaces=("u-avery",)) -> AuthInfo:
    """A password login: flat ADMIN, names only the user's own namespaces."""
    return AuthInfo(
        subject=ME,
        org=ORG,
        namespaces=tuple(namespaces),
        roles=(Role.ADMIN,),
        is_service_account=False,
    )


@pytest.mark.asyncio
async def test_unreadable_restricted_row_says_maintained_by_me_under_token_scope(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    set_current_auth(_login_token())
    by = {
        r.namespace: r for r in (await repos_mod.handle_code_repos(CodeReposInput(org=ORG))).repos
    }
    private = by[PRIVATE]
    assert private.readable is False
    assert private.has_maintainer is True, "an admin exists: never offer adopt"
    assert private.my_role == "admin", "the unreadable row names the caller's own role"
    assert private.unreadable_reason == "token_scope"
    assert private.git_head == "" and private.counts == {}, "still request-only: no contents"
    master = by[MASTER]
    assert master.readable and master.has_maintainer and master.unreadable_reason == ""


@pytest.mark.asyncio
async def test_branch_without_any_admin_is_the_only_ownerless_row(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    set_current_auth(_login_token())
    by = {
        r.namespace: r for r in (await repos_mod.handle_code_repos(CodeReposInput(org=ORG))).repos
    }
    branch = by[BRANCH]
    assert branch.readable is False and branch.has_maintainer is False
    assert branch.my_role == "" and branch.unreadable_reason == "not_member"
    assert [r.namespace for r in by.values() if not r.has_maintainer] == [BRANCH]


@pytest.mark.asyncio
async def test_token_naming_the_restricted_namespace_is_not_token_scope(plane) -> None:
    """Same grant, a token that names the namespace, gate still refusing
    (e.g. LOOM_RESTRICTED_SERVE=false): the reason is `closed`, not a
    re-sign-in hint the caller cannot act on."""
    from server.code.api.models_repos import CodeReposInput

    set_current_auth(_login_token(namespaces=("u-avery", PRIVATE)))
    by = {
        r.namespace: r for r in (await repos_mod.handle_code_repos(CodeReposInput(org=ORG))).repos
    }
    assert by[PRIVATE].unreadable_reason == "closed" and by[PRIVATE].my_role == "admin"


@pytest.mark.asyncio
async def test_failed_grants_read_fails_open_to_maintained(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    plane["maintained"] = None
    set_current_auth(_login_token())
    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    assert out.repos and all(r.has_maintainer for r in out.repos)


@pytest.mark.asyncio
async def test_directory_state_reads_admin_grants_for_all_keys_once(monkeypatch) -> None:
    """The fourth element is one grouped read over the grants table for every
    configured key: the entries with an active admin, whoever holds it."""
    import server.code.domain.index.repos as repos
    import server.core.ch as ch_mod

    entries = [_cfg(MASTER), _cfg(PRIVATE, "restricted"), _cfg(BRANCH)]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    statements: list[str] = []

    class _CH:
        async def query_all(self, sql, params=None):
            statements.append(" ".join(sql.split()))
            if "contact_email" in sql:  # the alias pair
                return [{"owner_email": ME, "contact_email": "me@example.com"}]
            if "FROM loom.namespaces FINAL" in sql:
                return [
                    {
                        "org": ORG,
                        "namespace": ns,
                        "visibility": "private",
                        "requires_approval": False,
                        "kind": "code",
                        "join_policy": "open",
                    }
                    for ns in (MASTER, PRIVATE, BRANCH)
                ]
            if "GROUP BY org, namespace" in sql:
                assert "role = 'admin'" in sql and "revoked_at IS NULL" in sql
                assert set(params["keys"]) == {(ORG, MASTER), (ORG, PRIVATE), (ORG, BRANCH)}
                return [{"org": ORG, "namespace": MASTER}, {"org": ORG, "namespace": PRIVATE}]
            assert "subject IN {subs:Array(String)}" in sql
            assert params["subs"] == [ME, "me@example.com"]
            return [{"org": ORG, "namespace": PRIVATE, "status": "active", "role": "admin"}]

    from server.auth import aliases as _al

    _al._CACHE.clear()
    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    policies, standing, roles, maintained = await repos_mod._repo_directory_state(_login_token())
    assert maintained == {(ORG, MASTER), (ORG, PRIVATE)}
    assert roles == {(ORG, PRIVATE): "admin"} and standing == {(ORG, PRIVATE): "member"}
    assert len(policies) == 3 and sum("GROUP BY org, namespace" in s for s in statements) == 1


@pytest.mark.asyncio
async def test_directory_state_grants_read_failure_yields_none(monkeypatch) -> None:
    import server.code.domain.index.repos as repos
    import server.core.ch as ch_mod

    monkeypatch.setattr(repos, "configured_repos", lambda: [_cfg(MASTER)])

    class _CH:
        async def query_all(self, sql, params=None):
            if "FROM loom.namespaces FINAL" in sql:
                return []
            raise RuntimeError("grants table unreachable")

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    _p, _s, _r, maintained = await repos_mod._repo_directory_state(_login_token())
    assert maintained is None


@pytest.mark.asyncio
async def test_directory_state_caller_grants_failure_leaves_the_gate_its_own_read(
    monkeypatch,
) -> None:
    """The maintainers read landed but the caller's grants read failed: `maintained`
    stays None, so the gate is not told "this caller holds no grant" and reads each
    grant itself instead of refusing every grant-only entry."""
    import server.code.domain.index.repos as repos
    import server.core.ch as ch_mod

    monkeypatch.setattr(repos, "configured_repos", lambda: [_cfg(MASTER)])

    class _CH:
        async def query_all(self, sql, params=None):
            if "FROM loom.namespaces FINAL" in sql:
                return []
            if "GROUP BY org, namespace" in sql:
                return [{"org": ORG, "namespace": MASTER}]
            if "subject IN" not in sql:
                return []
            raise RuntimeError("grants read timed out")

    from server.auth import aliases as _al

    _al._CACHE.clear()
    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    _p, _s, roles, maintained = await repos_mod._repo_directory_state(_login_token())
    assert maintained is None and roles == {}
    assert repos_mod._known_grant_kw(roles, (ORG, MASTER), grants_read=maintained is not None) == {}


def test_new_fields_default_so_older_rows_are_unchanged() -> None:
    from server.code.api.models_repos import CodeRepoSummary

    row = CodeRepoSummary(org=ORG, namespace=MASTER, repo="r", branch="b")
    assert row.has_maintainer is True and row.unreadable_reason == "" and row.my_role == ""


@pytest.mark.asyncio
async def test_a_readable_row_carries_the_tier_records_and_an_unreadable_one_none(
    plane, monkeypatch
) -> None:
    from server.code.api.models_repos import CodeReposInput
    from server.code.api.models_status import CodeTierHealth

    failing = CodeTierHealth(tier="clang", state="failing", kind="credential", lag_commits=413)

    async def _status(cfg):
        return SimpleNamespace(
            org=cfg.org,
            namespace=cfg.namespace,
            repo=cfg.repo,
            branch=cfg.git_branch,
            git_head="h",
            clone_head="h",
            fast_tier_lag=0,
            scip_lag=413,
            scip_py_lag=0,
            tier_health=[failing],
            state={},
            last_refresh_at="",
            counts={},
            chunks_total=0,
            chunks_embedded=0,
            embedding_coverage=1.0,
        )

    monkeypatch.setattr(index_status, "_repo_status", _status)
    index_status.reset_repo_status_cache()
    set_current_auth(_login_token())
    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    by = {r.namespace: r for r in out.repos}
    assert by[MASTER].tier_health == [failing]
    assert by[PRIVATE].tier_health == []  # an unreadable row shows nothing of the index
