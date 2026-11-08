"""A refusal on a repository namespace says how to join it.

Every code row is private and the join is the read, so a
principal refused on a code namespace is one click from access — the refusal
text must say so. A refusal on an ordinary memory namespace says nothing of
the kind. Pure unit: the repo config and the grant lookup are patched; no CH
(the scoped-resolve path is CH-free by invariant and this pins that it makes
no read: the fake CH client raises on any use)."""

from __future__ import annotations

import pytest

import server.auth.authz as authz
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "clickhouse"
CODE_NS = "code-clickhouse"
MEMORY_NS = "team-notes"
JOIN = "This is a repository namespace: join it from the dashboard's Repositories page, or call code.repos"


def _scoped(ns: str = "clickgap-public", role: str = "writer") -> AuthInfo:
    return AuthInfo(
        subject="sa:clickgap",
        org=ORG,
        namespaces=(ns,),
        roles=(),
        is_service_account=True,
        ns_roles=((ns, role),),
    )


def _flat_viewer() -> AuthInfo:
    return AuthInfo(
        subject="someone@x",
        org=ORG,
        namespaces=("personal",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """`code-clickhouse` is the one configured repository; nobody holds a
    grant; nothing is restricted; ClickHouse is unreachable on purpose."""
    import server.code.domain.index.repos as repos
    import server.core.ch as ch

    class _Cfg:
        org_readable = False
        classification = "standard"

    monkeypatch.setattr(
        repos,
        "config_entry",
        lambda org, ns: _Cfg() if (org, ns) == (ORG, CODE_NS) else None,
    )

    async def _no_grant(org, ns, subject):
        return None

    async def _not_restricted(org, ns):
        return False

    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    monkeypatch.setattr(authz, "may_be_restricted", _not_restricted)

    def _no_ch():
        raise AssertionError("authz refusal must not read ClickHouse")

    monkeypatch.setattr(ch, "get_client", _no_ch)


@pytest.mark.asyncio
async def test_scoped_token_refused_on_a_repository_names_the_join(plane) -> None:
    with pytest.raises(PermissionError) as ei:
        await authz.check_authorized(_scoped(), Role.VIEWER, org=ORG, namespace=CODE_NS)
    msg = str(ei.value)
    assert f"cannot access {ORG}/{CODE_NS}" in msg
    assert JOIN in msg


@pytest.mark.asyncio
async def test_scoped_token_refused_a_write_on_a_repository_names_the_join(plane) -> None:
    """The scoped-token branch itself (not the grant fall-through) carries
    the hint too: a WRITER check on a repository is refused there."""
    with pytest.raises(PermissionError) as ei:
        await authz.check_authorized(_scoped(), Role.WRITER, org=ORG, namespace=CODE_NS)
    msg = str(ei.value)
    assert msg.startswith(
        f"scoped token: WRITER required on {ORG}/{CODE_NS}; token grants no access there"
    )
    assert JOIN in msg


@pytest.mark.asyncio
async def test_flat_viewer_refused_on_a_repository_names_the_join(plane) -> None:
    with pytest.raises(PermissionError) as ei:
        await authz.check_authorized(_flat_viewer(), Role.VIEWER, org=ORG, namespace=CODE_NS)
    assert JOIN in str(ei.value)


@pytest.mark.asyncio
async def test_scoped_token_refused_on_a_memory_namespace_does_not(plane) -> None:
    with pytest.raises(PermissionError) as ei:
        await authz.check_authorized(_scoped(), Role.VIEWER, org=ORG, namespace=MEMORY_NS)
    msg = str(ei.value)
    assert (
        msg == f"scoped token: VIEWER required on {ORG}/{MEMORY_NS}; token grants no access there"
    )
    assert "Repositories" not in msg


@pytest.mark.asyncio
async def test_flat_viewer_refused_on_a_memory_namespace_does_not(plane) -> None:
    with pytest.raises(PermissionError) as ei:
        await authz.check_authorized(_flat_viewer(), Role.VIEWER, org=ORG, namespace=MEMORY_NS)
    assert "Repositories" not in str(ei.value)


def test_hint_is_one_sentence_appended_to_the_original_text(plane) -> None:
    e = authz._refused(
        "scoped token: VIEWER required on x/y; token grants no access there", ORG, CODE_NS
    )
    assert isinstance(e, PermissionError)  # same class → AUTH_INSUFFICIENT at the transport
    assert str(e).startswith("scoped token: VIEWER required on x/y; token grants no access there ")
    assert str(e).endswith("or call code.repos to see what you can join.")
