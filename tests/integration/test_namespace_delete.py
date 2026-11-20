"""namespace.delete — soft-delete (active=false)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from tests.integration._resources import unique_org
from tests.integration._sources import NAME, retirement, seed_source

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


@pytest.mark.asyncio
async def test_a_delete_retires_the_namespace_s_document_sources(
    control_plane_url,
    mint_test_token,
    ch_client,
) -> None:
    """A source on a deleted namespace goes inactive with its credential cleared."""
    from loom import Agent

    org = unique_org("nsdel")
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"])
    seed_source(ch_client, org, ns)
    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.namespaces.register(owner_email="alice@example.com")
        out = await a.namespaces.delete()
    assert (out["deleted"], out["sources_retired"]) == (True, [NAME])
    assert retirement(ch_client, org, ns) == [(False, "")]


@pytest.mark.asyncio
async def test_a_source_retired_with_its_namespace_can_still_have_its_documents_retired(
    control_plane_url,
    mint_test_token,
    ch_client,
) -> None:
    """After a delete, a remove with delete_documents marks the retired source for the
    worker; without it the source is not found."""
    from loom import Agent
    from loom.errors import LoomError

    org = unique_org("nsdel")
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"])
    seed_source(ch_client, org, ns)
    named = {"name": NAME, "confirm": NAME}
    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.namespaces.register(owner_email="alice@example.com")
        await a.namespaces.delete()
        with pytest.raises(LoomError) as refused:
            await a.call("knowledge.source.remove", named)
        assert (refused.value.code, refused.value.status) == ("SOURCE_NOT_FOUND", 404)
        await a.call("knowledge.source.remove", {**named, "delete_documents": True})
    rows = ch_client.query(
        "SELECT active, pending_delete FROM loom.knowledge_sources FINAL"
        " WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    ).result_rows
    assert [tuple(r) for r in rows] == [(False, True)]


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_namespace_erasure_retires_the_namespace_s_document_sources(
    control_plane_url,
    mint_test_token,
    ch_client,
) -> None:
    """The erasure's first step leaves the source inactive with its credential cleared."""
    from loom import Agent

    org = unique_org("nserase")
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["org_admin", "admin", "writer"])
    seed_source(ch_client, org, ns)
    async with Agent(
        org=org, namespace=ns, agent="a", base_url=control_plane_url, api_key=tok, timeout_s=60.0
    ) as a:
        result = await a.erasure.request(
            scope_type="namespace", scope_value=ns, requester="ops@example.com"
        )
    assert result["status"] == "completed"
    assert "knowledge_sources.retire" in {s["step"] for s in result["steps"]}
    assert retirement(ch_client, org, ns) == [(False, "")]
