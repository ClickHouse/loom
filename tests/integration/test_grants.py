"""Per-namespace RBAC grants — auth-path elevation + the wire ops."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from tests.integration._eventually import acl_change_adopted_async

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_grant_elevates_viewer_to_writer(
    control_plane_url,
    mint_test_token,
) -> None:
    """Viewer token gets 403 on memory.set; admin grants writer role on the
    namespace; viewer retries and succeeds."""
    from loom import Agent
    from loom.errors import AuthInsufficient

    org = f"grants-{uuid.uuid4().hex[:6]}"
    ns = f"ns-{uuid.uuid4().hex[:6]}"

    viewer_tok = mint_test_token(org=org, namespaces=[ns], roles=["viewer"], subject="alice@x")
    admin_tok = mint_test_token(
        org=org, namespaces=[ns], roles=["admin", "writer"], subject="admin@x"
    )

    # Pre-grant: viewer can't write.
    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=viewer_tok
    ) as a:
        with pytest.raises(AuthInsufficient):
            await a.memory.set("k1", "v1", memory_type="semantic")

    # Admin grants alice writer.
    async with Agent(
        org=org,
        namespace=ns,
        agent="admin",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as adm:
        result = await adm.grants.create("alice@x", "writer", granted_by="admin@x")
        assert result["role"] == "writer"

    # Post-grant: viewer's writes go through via the grant fallback.
    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=viewer_tok
    ) as a:
        result = await a.memory.set("k1", "v1", memory_type="semantic")
        assert int(result["version"]) > 0


@pytest.mark.asyncio
async def test_revoke_removes_elevation(
    control_plane_url,
    mint_test_token,
) -> None:
    """After revoke, the viewer is denied again."""
    from loom import Agent
    from loom.errors import AuthInsufficient

    org = f"grants-{uuid.uuid4().hex[:6]}"
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    viewer_tok = mint_test_token(org=org, namespaces=[ns], roles=["viewer"], subject="bob@x")
    admin_tok = mint_test_token(
        org=org, namespaces=[ns], roles=["admin", "writer"], subject="admin@x"
    )

    async with Agent(
        org=org,
        namespace=ns,
        agent="adm",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as adm:
        await adm.grants.create("bob@x", "writer", granted_by="admin@x")
    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=viewer_tok
    ) as a:
        r = await a.memory.set("k", "v", memory_type="semantic")
        assert int(r["version"]) > 0
    async with Agent(
        org=org,
        namespace=ns,
        agent="adm",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as adm:
        rv = await adm.grants.revoke("bob@x")
        assert rv["revoked"] is True
    # Every server process refuses within one watermark poll of the revoke.
    await acl_change_adopted_async()
    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=viewer_tok
    ) as a:
        with pytest.raises(AuthInsufficient):
            await a.memory.set("k2", "v2", memory_type="semantic")


@pytest.mark.asyncio
async def test_revoke_nonexistent_is_idempotent(
    control_plane_url,
    mint_test_token,
) -> None:
    """Revoking a subject that was never granted returns revoked=False."""
    from loom import Agent

    org = f"grants-{uuid.uuid4().hex[:6]}"
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    admin_tok = mint_test_token(org=org, namespaces=[ns], roles=["admin"], subject="admin@x")

    async with Agent(
        org=org,
        namespace=ns,
        agent="adm",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as adm:
        rv = await adm.grants.revoke("never-granted@x")
        assert rv["revoked"] is False


@pytest.mark.asyncio
async def test_list_returns_active_grants_only(
    control_plane_url,
    mint_test_token,
) -> None:
    """grants.list filters out revoked rows."""
    from loom import Agent

    org = f"grants-{uuid.uuid4().hex[:6]}"
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    admin_tok = mint_test_token(org=org, namespaces=[ns], roles=["admin"], subject="admin@x")

    async with Agent(
        org=org,
        namespace=ns,
        agent="adm",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as adm:
        await adm.grants.create("active@x", "writer", granted_by="admin@x")
        await adm.grants.create("toRevoke@x", "writer", granted_by="admin@x")
        await adm.grants.revoke("toRevoke@x")

        rows = await adm.grants.list()
        subjects = {g["subject"] for g in rows}
        assert "active@x" in subjects
        assert "toRevoke@x" not in subjects
