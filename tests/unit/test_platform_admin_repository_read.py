"""Platform authority reads registered repositories without widening other scopes."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth import authz
from server.auth.directory import ClassificationUnavailable
from server.code.domain.index import repos
from server.identity.app import namespace_classification as classification
from server.operations.base import Role
from tests.unit.code_fixtures import repo_config
from tests.unit.principals import acting_as, principal

ORG = "example"
NS = "code-private"


@pytest.fixture
def repository(monkeypatch):
    entries = {
        (ORG, NS): repo_config(
            org=ORG,
            namespace=NS,
            repo="example/private",
            classification="restricted",
            org_readable=False,
        )
    }
    monkeypatch.setattr(repos, "config_entry", lambda org, ns: entries.get((org, ns)))
    monkeypatch.setattr(repos, "configured_repos", lambda: list(entries.values()))
    monkeypatch.setattr(classification, "restricted_set", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(authz, "grant_role_for", AsyncMock(return_value=None))
    monkeypatch.setattr(authz, "_lookup_grant", AsyncMock(return_value=None))
    return entries


def _admin(**fields):
    return principal(
        Role.PLATFORM_ADMIN, org=ORG, namespaces=("personal",), service_account=False, **fields
    )


@pytest.mark.parametrize("level", ["standard", "restricted"])
@pytest.mark.parametrize("cross_org", [False, True])
async def test_platform_admin_reads_repository_without_claim_or_grant(repository, level, cross_org):
    repository[(ORG, NS)] = replace(repository[(ORG, NS)], classification=level)
    auth = _admin()
    if cross_org:
        auth = replace(auth, org="other")
    await authz.check_authorized(auth, Role.VIEWER, ORG, NS)


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ORG_ADMIN])
@pytest.mark.parametrize("level", ["standard", "restricted"])
async def test_namespace_and_org_admin_still_need_repository_access(repository, role, level):
    repository[(ORG, NS)] = replace(repository[(ORG, NS)], classification=level)
    auth = replace(_admin(), roles=(role,))
    with pytest.raises(PermissionError):
        await authz.check_authorized(auth, Role.VIEWER, ORG, NS)


@pytest.mark.parametrize("level", ["standard", "restricted"])
@pytest.mark.parametrize("authority", ["scoped", "impersonated", "delegated"])
async def test_platform_role_does_not_escape_token_authority(
    repository, monkeypatch, level, authority
):
    repository[(ORG, NS)] = replace(repository[(ORG, NS)], classification=level)
    auth = _admin()
    if authority == "scoped":
        auth = replace(auth, ns_roles=(("personal", "viewer"),))
    elif authority == "impersonated":
        auth = replace(auth, impersonated_by="sa:gateway")
    else:
        auth = replace(auth, creator="owner@example.com", parents=("parent-token",))
        monkeypatch.setattr(authz, "_check_creator", AsyncMock())
    with pytest.raises(PermissionError):
        await authz.check_authorized(auth, Role.VIEWER, ORG, NS)


@pytest.mark.parametrize("namespace", ["memory-private", "tracker-private", "code-unknown"])
async def test_platform_authority_does_not_widen_unregistered_namespaces(
    repository, monkeypatch, namespace
):
    monkeypatch.setattr(
        classification, "restricted_set", AsyncMock(return_value=frozenset({namespace}))
    )
    with pytest.raises(PermissionError):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, namespace)


@pytest.mark.parametrize("role", [Role.WRITER, Role.ADMIN])
@pytest.mark.parametrize("level", ["standard", "restricted"])
async def test_repository_writes_still_require_access(repository, role, level):
    repository[(ORG, NS)] = replace(repository[(ORG, NS)], classification=level)
    with pytest.raises(PermissionError):
        await authz.check_authorized(_admin(), role, ORG, NS)


async def test_registered_repository_does_not_need_a_code_prefix(repository):
    cfg = repository.pop((ORG, NS))
    repository[(ORG, "repository-index")] = replace(cfg, namespace="repository-index")
    await authz.check_authorized(_admin(), Role.VIEWER, ORG, "repository-index")


async def test_new_registry_entry_is_checked_on_each_read(repository):
    with pytest.raises(PermissionError):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, "code-added")
    repository[(ORG, "code-added")] = replace(repository[(ORG, NS)], namespace="code-added")
    await authz.check_authorized(_admin(), Role.VIEWER, ORG, "code-added")
    del repository[(ORG, "code-added")]
    with pytest.raises(PermissionError):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, "code-added")


async def test_restricted_kill_switch_still_refuses_platform_admin(repository, monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "restricted_serve", False)
    with pytest.raises(PermissionError):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, NS)


async def test_unavailable_classification_still_refuses_platform_admin(repository, monkeypatch):
    monkeypatch.setattr(
        authz.directory(), "is_restricted", AsyncMock(side_effect=ClassificationUnavailable())
    )
    with pytest.raises(PermissionError):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, NS)


async def test_held_org_refusal_precedes_repository_authority(repository, monkeypatch):
    from server.auth import tenancy

    monkeypatch.setattr(tenancy, "ensure_held", AsyncMock(side_effect=PermissionError("not held")))
    with pytest.raises(PermissionError, match="not held"):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, NS)


async def test_invalid_principal_is_refused_before_repository_authority(repository):
    with pytest.raises(PermissionError, match="invalid auth principal"):
        await authz.check_authorized(
            SimpleNamespace(roles=(Role.PLATFORM_ADMIN,)), Role.VIEWER, ORG, NS
        )


async def test_row_scope_agrees_without_revealing_foreign_memory(repository, monkeypatch):
    monkeypatch.setattr(
        classification, "restricted_set", AsyncMock(return_value=frozenset({"memory-private"}))
    )
    with acting_as(_admin()):
        assert await authz.row_in_scope(ORG, NS)
        assert not await authz.row_in_scope(ORG, "memory-private")
        assert not await authz.row_in_scope(ORG, NS, Role.WRITER)
    with acting_as(replace(_admin(), ns_roles=(("personal", "viewer"),))):
        assert not await authz.row_in_scope(ORG, NS)


@pytest.mark.parametrize("join_policy", ["open", "approval", "closed"])
@pytest.mark.parametrize("level", ["standard", "restricted"])
async def test_admin_listing_returns_actual_heads_and_counts(
    repository, monkeypatch, join_policy, level
):
    from server.code.api.models_repos import CodeReposInput
    from server.code.app import repos as listing
    from server.code.app.index import repo_registry

    repository[(ORG, NS)] = replace(repository[(ORG, NS)], classification=level)
    monkeypatch.setattr(repo_registry, "refresh_db_repos", AsyncMock(return_value=[]))
    monkeypatch.setattr(repos, "docs_namespaces", lambda: {})
    monkeypatch.setattr(listing.CodeRepo, "all_state_many", AsyncMock(return_value={}))
    monkeypatch.setattr(
        listing,
        "_repo_directory_state",
        AsyncMock(return_value=({(ORG, NS): join_policy}, {}, {}, set())),
    )
    monkeypatch.setattr(listing, "_bound_trackers", AsyncMock(return_value=lambda *a: []))
    status = SimpleNamespace(
        org=ORG,
        namespace=NS,
        repo="example/private",
        branch="main",
        git_head="actual-index-head",
        clone_head="actual-clone-head",
        clone_fetched_at="2026-01-01T00:00:00Z",
        fast_tier_lag=2,
        scip_lag=3,
        scip_py_lag=4,
        tier_health=[],
        state={},
        last_refresh_at="2026-01-01T00:00:00Z",
        counts={"files": 17, "symbols": 23},
        chunks_total=10,
        chunks_embedded=10,
        embedding_coverage=1.0,
    )
    status_read = AsyncMock(return_value=status)
    monkeypatch.setattr(listing, "_repo_status_cached", status_read)
    with acting_as(replace(_admin(), org="other")):
        rows = await listing._code_repo_rows(CodeReposInput(org="other"), None)
    assert len(rows) == 1
    row = rows[0]
    assert row.readable and row.join_policy == join_policy
    assert row.git_head == status.git_head and row.clone_head == status.clone_head
    assert row.counts == status.counts and row.last_refresh_at == status.last_refresh_at
    assert row.chunks_total == 10 and row.scip_lag == 3
    assert row.my_role == ""  # read authority does not invent a maintainer grant
    assert status_read.await_count >= 1


async def test_repository_authority_never_opens_mirrors(repository):
    mirror = "acl-0123456789abcdef01234567"
    repository[(ORG, mirror)] = replace(repository[(ORG, NS)], namespace=mirror)
    for auth in (_admin(), None):
        with pytest.raises(PermissionError):
            await authz.check_authorized(auth, Role.VIEWER, ORG, mirror)


async def test_collection_gate_precedes_repository_authority(repository, monkeypatch):
    from server.auth import sharing

    namespace = sharing.COLLECTION_PREFIX + "private"
    repository[(ORG, namespace)] = replace(repository[(ORG, NS)], namespace=namespace)
    denied = AsyncMock(side_effect=PermissionError("collection refused"))
    monkeypatch.setattr(sharing, "check", denied)
    with pytest.raises(PermissionError, match="collection refused"):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, namespace)
    denied.assert_awaited_once()


async def test_direct_restricted_predicate_checks_tenancy(repository, monkeypatch):
    from server.auth import tenancy

    assert await authz.restricted_allows(_admin(), Role.VIEWER, ORG, NS)
    monkeypatch.setattr(tenancy, "ensure_held", AsyncMock(side_effect=PermissionError("not held")))
    assert not await authz.restricted_allows(_admin(), Role.VIEWER, ORG, NS)


@pytest.fixture
def transports(repository, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.auth import bearer
    from server.auth.jwt import TokenError
    from server.code.app import files
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.settings import settings
    from server.transport import http, mcp_call, mcp_outcome

    tokens = {
        "admin": _admin(),
        "other-org-admin": replace(_admin(), org="other"),
        "namespace-admin": replace(_admin(), roles=(Role.ADMIN,)),
        "scoped": replace(_admin(), ns_roles=(("personal", "viewer"),)),
    }

    def verify(token):
        if token not in tokens:
            raise TokenError("revoked or unverifiable token")
        return tokens[token]

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(bearer, "authenticate_token", AsyncMock(side_effect=verify))
    monkeypatch.setattr(
        "server.auth.personal_namespace.ensure_personal_namespace_cached", AsyncMock()
    )
    monkeypatch.setattr(
        "server.identity.app.namespace_autoregister.maybe_autoregister", AsyncMock()
    )
    audit = AsyncMock()
    monkeypatch.setattr("server.identity.app.restricted_access.note_restricted_access", audit)
    monkeypatch.setattr(files, "require_code_intel", lambda: None)
    monkeypatch.setattr(files, "_not_covered_output", lambda *a: None)
    monkeypatch.setattr(files, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(files, "index_as_of", AsyncMock(return_value={"commit": "indexed-head"}))
    read = AsyncMock(return_value=([{"path": "src/private.py", "lang": "python"}], 1))
    monkeypatch.setattr(files.CodeRepo, "list_files", read)
    op = OPERATIONS_BY_NAME["code.files"]
    monkeypatch.setattr(http, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(http, "_emit_event", lambda *a, **kw: None)
    monkeypatch.setattr(mcp_call, "_caller_defaults", AsyncMock())
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, "loom_code_files", op)
    for mod in (mcp_call, mcp_outcome):
        monkeypatch.setattr(mod, "_emit_mcp_event", lambda *a, **kw: None)
    app = FastAPI()
    app.include_router(http.build_router())
    with TestClient(app) as client:
        yield SimpleNamespace(http=client, tokens=tokens, read=read, audit=audit, mcp=mcp_call)


@pytest.mark.parametrize(
    "who", ["admin", "other-org-admin", "namespace-admin", "scoped", "revoked"]
)
def test_http_repository_read_uses_actual_authorization(transports, who):
    response = transports.http.post(
        "/v1/code.files",
        json={"org": ORG, "namespace": NS, "path_prefix": "src/"},
        headers={"Authorization": "Bearer " + who},
    )
    if who in ("admin", "other-org-admin"):
        assert response.status_code == 200, response.text
        assert response.json()["files"] == [{"path": "src/private.py", "lang": "python"}]
        transports.read.assert_awaited_once()
        transports.audit.assert_awaited_once()
    else:
        assert response.status_code in (401, 403), response.text
        assert "src/private.py" not in response.text
        transports.read.assert_not_awaited()
        transports.audit.assert_not_awaited()


@pytest.mark.parametrize("who", ["admin", "other-org-admin", "namespace-admin", "scoped"])
async def test_mcp_repository_read_uses_actual_authorization(transports, who):
    response = await transports.mcp._call_tool(
        1,
        {
            "name": "loom_code_files",
            "arguments": {"org": ORG, "namespace": NS, "path_prefix": "src/"},
        },
        auth=transports.tokens[who],
    )
    result = response["result"]
    if who in ("admin", "other-org-admin"):
        assert not result["isError"], result
        assert result["structuredContent"]["files"] == [
            {"path": "src/private.py", "lang": "python"}
        ]
        transports.read.assert_awaited_once()
        transports.audit.assert_awaited_once()
    else:
        assert result["isError"], result
        assert "src/private.py" not in str(result)
        transports.read.assert_not_awaited()
        transports.audit.assert_not_awaited()


async def test_self_service_registry_snapshot_decides_repository_authority(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_configured_from_env", lambda: [])
    for name in ("_registry_repos", "_registry_namespaces", "_page_namespaces", "_registry_loaded"):
        monkeypatch.setattr(repos, name, getattr(repos, name))
    monkeypatch.setattr(classification, "restricted_set", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(authz, "_lookup_grant", AsyncMock(return_value=None))
    entry = repo_config(
        org=ORG,
        namespace="page-index",
        repo="example/page",
        classification="restricted",
        source="registry",
        row_kind="page",
        org_readable=False,
    )
    repos.set_registry_repos([], frozenset())
    with pytest.raises(PermissionError):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, entry.namespace)
    repos.set_registry_repos([entry], frozenset({(ORG, entry.namespace)}))
    await authz.check_authorized(_admin(), Role.VIEWER, ORG, entry.namespace)
    repos.set_registry_repos([], frozenset({(ORG, entry.namespace)}))
    with pytest.raises(PermissionError):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, entry.namespace)


@pytest.mark.parametrize("level", ["standard", "restricted"])
async def test_read_authority_does_not_admit_repository_refresh(repository, level):
    from server.code.app.member_writes import authorize_op
    from server.operations.registry import OPERATIONS_BY_NAME

    repository[(ORG, NS)] = replace(repository[(ORG, NS)], classification=level)
    with pytest.raises(PermissionError):
        await authorize_op(_admin(), OPERATIONS_BY_NAME["code.refresh"], org=ORG, namespace=NS)


@pytest.mark.parametrize("transport", ["http", "mcp"])
async def test_transport_restricted_kill_switch_precedes_repository_read(
    transports, monkeypatch, transport
):
    from server.settings import settings

    monkeypatch.setattr(settings, "restricted_serve", False)
    body = {"org": ORG, "namespace": NS, "path_prefix": "src/"}
    if transport == "http":
        response = transports.http.post(
            "/v1/code.files", json=body, headers={"Authorization": "Bearer admin"}
        )
        assert response.status_code == 403, response.text
    else:
        response = await transports.mcp._call_tool(
            1, {"name": "loom_code_files", "arguments": body}, auth=transports.tokens["admin"]
        )
        assert response["result"]["isError"], response
    transports.read.assert_not_awaited()
    transports.audit.assert_not_awaited()


@pytest.mark.parametrize("op_name", ["memory.set", "memory.update", "memory.delete"])
@pytest.mark.parametrize("claim", ["missing", "other-org", "named"])
async def test_repository_read_authority_does_not_widen_member_writes(
    repository, monkeypatch, op_name, claim
):
    from server.code.app.member_writes import admitted_as_code_member, authorize_op
    from server.operations.registry import OPERATIONS_BY_NAME

    monkeypatch.setattr(authz, "grant_role_for", AsyncMock(return_value=Role.VIEWER))
    monkeypatch.setattr(authz, "_lookup_grant", AsyncMock(return_value=Role.VIEWER))
    auth = _admin()
    if claim == "other-org":
        auth = replace(auth, org="other", namespaces=(NS,))
    elif claim == "named":
        auth = replace(auth, namespaces=(NS,))
    await authz.check_authorized(auth, Role.VIEWER, ORG, NS)
    if claim == "named":
        await authorize_op(auth, OPERATIONS_BY_NAME[op_name], org=ORG, namespace=NS)
        assert admitted_as_code_member()
    else:
        with pytest.raises(PermissionError):
            await authorize_op(auth, OPERATIONS_BY_NAME[op_name], org=ORG, namespace=NS)
        assert not admitted_as_code_member()


@pytest.mark.parametrize("level", ["standard", "restricted"])
@pytest.mark.parametrize("unavailable", [False, True])
async def test_provisioning_precedes_repository_authority(
    repository, monkeypatch, level, unavailable
):
    from server.auth import login, scim
    from server.core.errors import BackendUnavailableError

    repository[(ORG, NS)] = replace(repository[(ORG, NS)], classification=level)
    check = AsyncMock(return_value=True)
    if unavailable:
        check.side_effect = login.SignInUnavailable("provisioning is unavailable")
    monkeypatch.setattr(scim, "refuses_auth", check)
    refusal = BackendUnavailableError if unavailable else authz.ActedForRefused
    with pytest.raises(refusal):
        await authz.check_authorized(_admin(), Role.VIEWER, ORG, NS)
    check.assert_awaited_once()
