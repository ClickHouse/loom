"""A scoped token presented without org+namespace in the body must fail as
IDENTITY_INCOMPLETE (400), NOT AUTH_INSUFFICIENT (403) — the token may be
fully authorized; the request just didn't say for which namespace, and a 403
would send the caller chasing a token that is fine."""

from __future__ import annotations

import asyncio

import pytest

from server.auth.authz import Role, check_authorized
from server.auth.jwt import AuthInfo, mint_token, verify_token
from server.core.errors import IdentityIncompleteError

_SECRET = "test-secret-01234567890123456789012345678901"


def _scoped() -> AuthInfo:
    tok = mint_token(
        "sa:bot", "acme", namespaces=["priv"], roles=[], ns_roles={"priv": "writer"}, secret=_SECRET
    )
    return verify_token(tok, secret=_SECRET)


def test_scoped_token_missing_namespace_is_identity_incomplete() -> None:
    with pytest.raises(IdentityIncompleteError):
        asyncio.run(check_authorized(_scoped(), Role.WRITER, org="acme", namespace=None))
    with pytest.raises(IdentityIncompleteError):
        asyncio.run(check_authorized(_scoped(), Role.WRITER, org=None, namespace=None))
    assert IdentityIncompleteError.status_code == 400


def test_scoped_token_with_namespace_still_authorizes_normally() -> None:
    # sanity: the guard doesn't break the happy path
    asyncio.run(check_authorized(_scoped(), Role.WRITER, org="acme", namespace="priv"))
    with pytest.raises(PermissionError):
        asyncio.run(check_authorized(_scoped(), Role.WRITER, org="acme", namespace="other"))


def test_deployment_scoped_op_admits_a_scoped_token_by_its_best_role() -> None:
    """An op about the deployment (ops.capabilities)
    reads no namespace, so a scoped token passes its role gate on the best
    per-namespace role it carries — a short role is a 403, never a 400."""
    asyncio.run(
        check_authorized(_scoped(), Role.VIEWER, org=None, namespace=None, deployment_scoped=True)
    )
    asyncio.run(
        check_authorized(_scoped(), Role.WRITER, org="acme", namespace=None, deployment_scoped=True)
    )
    with pytest.raises(PermissionError):
        asyncio.run(
            check_authorized(
                _scoped(), Role.ADMIN, org=None, namespace=None, deployment_scoped=True
            )
        )
    # With a namespace in the body the flag changes nothing.
    asyncio.run(
        check_authorized(
            _scoped(), Role.WRITER, org="acme", namespace="priv", deployment_scoped=True
        )
    )
    assert _scoped().best_ns_role() is Role.WRITER
    # And an ordinary org-level op still refuses the namespace-less scoped call.
    with pytest.raises(IdentityIncompleteError):
        asyncio.run(check_authorized(_scoped(), Role.VIEWER, org="acme", namespace=None))


def test_only_the_org_level_listings_are_deployment_scoped() -> None:
    from server.operations.registry import ALL_OPERATIONS

    scoped = sorted(o.name for o in ALL_OPERATIONS if o.deployment_scoped)
    # Code.repos is org-level and per-entry authorized in
    # its handler, so a scoped token is admitted by its best role too.
    # The caller's own memberships and invitations name no
    # namespace either — list_mine refused every scoped token over MCP.
    # The org's tracker reads (the contract, the source list
    # and a source's check) name no namespace; the writes stay unscoped-only.
    # The static agent guide (mentor.guide) and the shipped docs (help.*) name no
    # namespace.
    # Collections are listed and made by grant, not by a namespace the call
    # names; a scoped token reaches them no further than its claim.
    assert scoped == [
        "code.repos",
        "collection.create",
        "collection.list",
        "grants.pending_for_me",
        "help.get",
        "help.search",
        "mentor.guide",
        "namespace.browse",
        "namespace.list_mine",
        "ops.capabilities",
        "tracker.contract",
        "tracker.source.check",
        "tracker.source.list",
    ]
    cap = next(o for o in ALL_OPERATIONS if o.name == "ops.capabilities")
    assert cap.skip_namespace_authz and cap.required_role is Role.VIEWER


def test_a_scoped_token_lists_its_own_namespaces_over_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """`loom_namespace_list_mine` from a scoped token (the
    sweep's Claude Code bearer) answered IDENTITY_INCOMPLETE — the op has no
    namespace field for the MCP defaults to fill."""
    import dataclasses

    import server.transport.mcp_call as mcp_call
    from server.identity.api.models import NamespaceListMineOutput

    calls: list[str] = []

    async def _handler(inp):  # type: ignore[no-untyped-def]
        calls.append(inp.org)
        return NamespaceListMineOutput(rows=[])

    tool = "loom_namespace_list_mine"
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME,
        tool,
        dataclasses.replace(mcp_call.TOOLS_BY_NAME[tool], handler=_handler),
    )

    async def _defaults(auth, client_name=None):  # type: ignore[no-untyped-def]
        return {"org": "acme", "namespace": "priv", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    resp = asyncio.run(mcp_call._call_tool(1, {"name": tool, "arguments": {}}, auth=_scoped()))
    assert calls == ["acme"], resp
    assert not resp["result"].get("isError"), resp
