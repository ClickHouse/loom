"""A memory an agent was shown reads as recalled, whichever read showed it.

`recall_count` holds only the ranking credit (a credited memory.search). A context pack
or a memory.ask writes a `served` event instead, which memory.get reports as
`served_count` / `last_served_at` under include_recall_breakdown, for every live copy of
the key; the dashboard's "Last recalled" reads those.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

pytestmark = pytest.mark.asyncio

ORG = unique_org("served-reads")
FACT = "the nightly backup of the orders cluster runs at 02:00 UTC"


async def _get(agent: Any, key: str, **extra: Any) -> dict[str, Any]:
    return await agent.transport.post(
        "/v1/memory.get",
        {
            "org": agent.org,
            "namespace": agent.namespace,
            "agent": agent.agent,
            "memory_key": key,
            "include_recall_breakdown": True,
            **extra,
        },
    )


async def test_a_context_pack_read_is_served_on_every_copy_of_the_key(
    control_plane_url: str,
) -> None:
    from loom import Agent

    ns = f"served-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("backup_window", FACT, memory_type="semantic")
        # A second session's copy of the same key, written later: the pack's event lands
        # on the newest copy, and the first copy reads the same figure.
        await a.transport.post(
            "/v1/memory.set",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "w",
                "session_id": "s2",
                "memory_key": "backup_window",
                "value": FACT,
                "memory_type": "semantic",
            },
        )
        before = await _get(a, "backup_window")
        assert (before["served_count"], before["last_served_at"]) == (0, None)
        assert before["recall_count"] == 0

        async def packed() -> bool:
            pack = await a.transport.post(
                "/v1/context.pack",
                {"org": ORG, "namespace": ns, "agent": "w", "query": "when does the backup run"},
            )
            return "02:00" in str(pack)

        await eventually(packed, description="the pack serves the fact")

        async def served() -> dict[str, Any]:
            got = await _get(a, "backup_window")
            return got if got["served_count"] >= 1 else {}

        got = await eventually(served, description="the served event lands")
        assert got["last_served_at"] is not None
        # the pack adds no ranking credit
        assert got["recall_count"] == 0
        other = await _get(a, "backup_window", session_id="s2")
        assert other["served_count"] == got["served_count"]
        # without the flag the figures are not read
        plain = await a.transport.post(
            "/v1/memory.get",
            {"org": ORG, "namespace": ns, "agent": "w", "memory_key": "backup_window"},
        )
        assert plain.get("served_count") is None


async def test_a_read_of_one_users_copy_counts_on_every_copy_in_the_namespace(
    control_plane_url: str,
) -> None:
    """An agent read puts its event on the newest copy of the key in the namespace,
    whichever user wrote it: a namespace-wide reader sees it on the other user's copy."""
    from loom import Agent

    ns = f"served-u-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for user in ("u1", "u2"):
            await a.transport.post(
                "/v1/memory.set",
                {
                    "org": ORG,
                    "namespace": ns,
                    "agent": "w",
                    "user_id": user,
                    "memory_key": "editor",
                    "value": "the team edits configuration in a terminal editor",
                    "memory_type": "semantic",
                },
            )

        async def packed() -> bool:
            pack = await a.transport.post(
                "/v1/context.pack",
                {
                    "org": ORG,
                    "namespace": ns,
                    "agent": "w",
                    "user_id": "u1",
                    "query": "which editor does the team use",
                },
            )
            return "terminal editor" in str(pack)

        await eventually(packed, description="the pack serves the fact")

        async def served(user: str) -> dict[str, Any]:
            got = await _get(a, "editor", user_id=user)
            return got if got["served_count"] >= 1 else {}

        for user in ("u1", "u2"):
            got = await eventually(lambda u=user: served(u), description=f"{user} sees the read")
            assert got["recall_count"] == 0
