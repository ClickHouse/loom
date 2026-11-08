"""Org-readable code namespaces: only the deployment's org_readable flag lets
any authenticated principal of the org read a configured code-plane entry
with the token it already holds. The directory row's
visibility=public widens nothing — an open repository is joinable in one
click, not readable unjoined."""

from __future__ import annotations

import pytest

import server.auth.authz as authz
from server.auth.jwt import AuthInfo
from server.operations.base import Role


def _scoped(org: str = "clickhouse") -> AuthInfo:
    # an agent service account: scoped to its own memory namespace only, roles=[]
    return AuthInfo(
        subject="sa:clickgap",
        org=org,
        namespaces=("clickgap-public",),
        roles=(),
        is_service_account=True,
        ns_roles=(("clickgap-public", "writer"),),
    )


def _flat_viewer(org: str = "clickhouse") -> AuthInfo:
    return AuthInfo(
        subject="someone@x",
        org=org,
        namespaces=("personal",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )


@pytest.fixture
def code_ns(monkeypatch: pytest.MonkeyPatch):
    """code-clickhouse is a configured entry; visibility comes from `vis`."""
    import server.code.domain.index.repos as repos
    import server.identity.app.namespace_visibility as nv

    state = {"vis": "public", "org_readable": False}

    class _Cfg:
        classification = "standard"

        @property
        def org_readable(self) -> bool:
            return state["org_readable"]

    monkeypatch.setattr(
        repos,
        "config_entry",
        lambda org, ns: _Cfg() if (org, ns) == ("clickhouse", "code-clickhouse") else None,
    )

    async def _vm(org: str, namespaces: list[str]) -> dict[str, str]:
        return {ns: state["vis"] for ns in namespaces if ns == "code-clickhouse"}

    monkeypatch.setattr(nv, "visibility_map", _vm)

    async def _no_grant(org, ns, subject):
        return None

    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    return state


@pytest.mark.asyncio
async def test_public_visibility_no_longer_reads_with_a_scoped_token(code_ns) -> None:
    """The join is the read. A token scoped elsewhere, holding
    no grant, is refused even though the row says public."""
    with pytest.raises(PermissionError):
        await authz.check_authorized(
            _scoped(), Role.VIEWER, org="clickhouse", namespace="code-clickhouse"
        )


@pytest.mark.asyncio
async def test_public_visibility_no_longer_reads_with_a_flat_token_without_grant(code_ns) -> None:
    with pytest.raises(PermissionError):
        await authz.check_authorized(
            _flat_viewer(), Role.VIEWER, org="clickhouse", namespace="code-clickhouse"
        )


@pytest.mark.asyncio
async def test_a_reader_grant_reads(code_ns, monkeypatch) -> None:
    async def _viewer_grant(org, ns, subject):
        return Role.VIEWER if ns == "code-clickhouse" else None

    monkeypatch.setattr(authz, "_lookup_grant", _viewer_grant)
    await authz.check_authorized(
        _scoped(), Role.VIEWER, org="clickhouse", namespace="code-clickhouse"
    )
    with pytest.raises(PermissionError):  # readers never write
        await authz.check_authorized(
            _scoped(), Role.WRITER, org="clickhouse", namespace="code-clickhouse"
        )


@pytest.mark.asyncio
async def test_private_code_namespace_keeps_ordinary_rules(code_ns) -> None:
    code_ns["vis"] = "private"
    with pytest.raises(PermissionError):
        await authz.check_authorized(
            _scoped(), Role.VIEWER, org="clickhouse", namespace="code-clickhouse"
        )


@pytest.mark.asyncio
async def test_org_readable_setting_overrides_private_visibility(code_ns, monkeypatch) -> None:
    """LOOM_CODE_ORG_READABLE: no adopt, no grant, no visibility click needed."""
    import server.identity.app.namespace_visibility as nv

    code_ns["vis"] = "private"
    code_ns["org_readable"] = True

    async def _never(org, namespaces):  # config decides; visibility not consulted
        raise AssertionError("visibility_map should not be called")

    monkeypatch.setattr(nv, "visibility_map", _never)
    await authz.check_authorized(
        _scoped(), Role.VIEWER, org="clickhouse", namespace="code-clickhouse"
    )
    with pytest.raises(PermissionError):  # writes still need a grant
        await authz.check_authorized(
            _scoped(), Role.WRITER, org="clickhouse", namespace="code-clickhouse"
        )
    with pytest.raises(PermissionError):  # other org still denied
        await authz.check_authorized(
            _scoped(org="acme"), Role.VIEWER, org="clickhouse", namespace="code-clickhouse"
        )


@pytest.mark.asyncio
async def test_other_org_cannot_read_org_readable_code_namespace(code_ns) -> None:
    code_ns["org_readable"] = True
    with pytest.raises(PermissionError):
        await authz.check_authorized(
            _scoped(org="acme"), Role.VIEWER, org="clickhouse", namespace="code-clickhouse"
        )


@pytest.mark.asyncio
async def test_rule_never_applies_to_memory_namespaces(code_ns) -> None:
    with pytest.raises(PermissionError):
        await authz.check_authorized(
            _scoped(), Role.VIEWER, org="clickhouse", namespace="someone-elses-memory"
        )


@pytest.mark.asyncio
async def test_scoped_service_account_reads_through_its_creators_reader_grant(
    code_ns, monkeypatch
) -> None:
    """The live shape: an agent token scoped to the customer's memory
    namespaces, whose human creator joined the repository. The join is the
    read, so the token follows without a re-mint."""

    async def _role_for(org, ns, subject):
        return Role.VIEWER if (ns, subject) == ("code-clickhouse", "clickgap-public") else None

    async def _creator(org, subject):
        return "clickgap-public"

    async def _inheriting_lookup(org, ns, subject):
        # the production _lookup_grant: own grant, else the sa: creator's
        role = await _role_for(org, ns, subject)
        if role is None and subject.startswith("sa:"):
            role = await _role_for(org, ns, await _creator(org, subject))
        return role

    monkeypatch.setattr(authz, "_lookup_grant", _inheriting_lookup)
    await authz.check_authorized(
        _scoped(), Role.VIEWER, org="clickhouse", namespace="code-clickhouse"
    )
    with pytest.raises(PermissionError):  # reads only — refresh stays a maintainer's
        await authz.check_authorized(
            _scoped(), Role.WRITER, org="clickhouse", namespace="code-clickhouse"
        )


@pytest.mark.asyncio
async def test_scoped_token_grant_widens_code_reads_only_not_memory(code_ns, monkeypatch) -> None:
    async def _viewer_grant(org, ns, subject):
        return Role.VIEWER

    monkeypatch.setattr(authz, "_lookup_grant", _viewer_grant)
    with pytest.raises(PermissionError):
        await authz.check_authorized(
            _scoped(), Role.VIEWER, org="clickhouse", namespace="someone-elses-memory"
        )
