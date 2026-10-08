"""namespace.register doubles as the settings-update wire op.

ReplacingMergeTree(updated_at) on loom.namespaces means a re-register lands as an
update: a setting is flipped by registering again with the new value, and a
re-register that omits it keeps the stored value. This test pins both through
the SDK, so a refactor can't quietly regress to create-only or reset-on-omit.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("flag-toggle")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


def _agent(control_plane_url, mint_test_token, prefix: str):
    from loom import Agent

    ns = f"{prefix}-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    return Agent(org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok)


@pytest.mark.asyncio
async def test_settings_default_off_on_fresh_register(control_plane_url, mint_test_token) -> None:
    async with _agent(control_plane_url, mint_test_token, "flag-default") as a:
        await a.namespaces.register(owner_email="t@example.com")
        info = await a.namespaces.get()
        assert info is not None
        assert info["consolidation_enabled"] is False
        assert info["requires_approval"] is False


@pytest.mark.asyncio
async def test_reregister_flips_a_setting_and_an_omitted_one_is_kept(
    control_plane_url, mint_test_token
) -> None:
    async with _agent(control_plane_url, mint_test_token, "flag-flip") as a:
        await a.namespaces.register(owner_email="t@example.com")
        await a.call("namespace.register", owner_email="t@example.com", consolidation_enabled=True)
        info = await a.namespaces.get()
        assert info is not None and info["consolidation_enabled"] is True
        # A register that does not name the setting keeps it.
        await a.namespaces.register(owner_email="t@example.com", description="edited")
        info2 = await a.namespaces.get()
        assert info2 is not None and info2["consolidation_enabled"] is True
        await a.call("namespace.register", owner_email="t@example.com", consolidation_enabled=False)
        info3 = await a.namespaces.get()
        assert info3 is not None and info3["consolidation_enabled"] is False


@pytest.mark.asyncio
async def test_each_setting_is_independently_flippable(control_plane_url, mint_test_token) -> None:
    async with _agent(control_plane_url, mint_test_token, "flag-indep") as a:
        await a.call(
            "namespace.register",
            owner_email="t@example.com",
            consolidation_enabled=True,
            requires_approval=True,
        )
        info = await a.namespaces.get()
        assert info is not None
        assert info["consolidation_enabled"] is True and info["requires_approval"] is True
        await a.call("namespace.register", owner_email="t@example.com", requires_approval=False)
        info2 = await a.namespaces.get()
        assert info2 is not None
        assert info2["consolidation_enabled"] is True and info2["requires_approval"] is False


@pytest.mark.asyncio
async def test_a_register_that_names_one_setting_keeps_the_rest(
    control_plane_url, mint_test_token
) -> None:
    async with _agent(control_plane_url, mint_test_token, "flag-keep") as a:
        await a.namespaces.register(
            owner_email="t@example.com",
            description="team facts",
            visibility="public",
            conflict_threshold=0.7,
            rerank_endpoint="https://rerank.example/v1",
            default_conflict_policy="manual_review",
        )
        await a.namespaces.register(owner_email="t@example.com", warm_days=3)
        info = await a.namespaces.get()
        assert info is not None
        assert info["warm_days"] == 3
        assert info["description"] == "team facts"
        assert info["visibility"] == "public"
        assert info["conflict_threshold"] == pytest.approx(0.7)
        assert info["rerank_endpoint"] == "https://rerank.example/v1"
        assert info["default_conflict_policy"] == "manual_review"
        # An explicit null is a value: the namespace goes back to the server's threshold.
        await a.call("namespace.register", owner_email="t@example.com", conflict_threshold=None)
        info2 = await a.namespaces.get()
        assert info2 is not None and info2["conflict_threshold"] is None
