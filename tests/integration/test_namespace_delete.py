"""namespace.delete — soft-delete (active=false)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_register_then_delete_marks_inactive(
    control_plane_url,
    mint_test_token,
) -> None:
    """A registered namespace flips to active=false after delete."""
    from loom import Agent

    org = f"nsdel-{uuid.uuid4().hex[:6]}"
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"])

    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.namespaces.register(owner_email="alice@example.com")
        result = await a.namespaces.delete()
        assert result["deleted"] is True

        info = await a.namespaces.get()
        # The registry row still exists, just marked inactive — namespace.get
        # filters to active=true by default, so it returns None.
        assert info is None or info.get("active") is False


@pytest.mark.asyncio
async def test_delete_idempotent(
    control_plane_url,
    mint_test_token,
) -> None:
    """Re-deleting an already-inactive namespace returns deleted=False."""
    from loom import Agent

    org = f"nsdel-{uuid.uuid4().hex[:6]}"
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"])

    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.namespaces.register(owner_email="x@y")
        assert (await a.namespaces.delete())["deleted"] is True
        assert (await a.namespaces.delete())["deleted"] is False


@pytest.mark.asyncio
async def test_delete_unregistered_is_noop(
    control_plane_url,
    mint_test_token,
) -> None:
    """Deleting a namespace that was never registered returns deleted=False."""
    from loom import Agent

    org = f"nsdel-{uuid.uuid4().hex[:6]}"
    ns = f"phantom-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"])

    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=tok
    ) as a:
        assert (await a.namespaces.delete())["deleted"] is False
