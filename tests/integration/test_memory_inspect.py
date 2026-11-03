"""memory.inspect against a live Loom + ClickHouse.

Two agents write into one namespace, one of them for two end users. Inspecting an agent
lists its own rows apart from the other agent's; inspecting a user lists the rows filed
under that user apart from the rest; each listed row carries the memory_id, version and
content hash memory.get serves; the guide and categories are read; the extraction policy
is not, for a writer on a server that enforces auth; and the inspection writes nothing.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("inspect")


@pytest.mark.asyncio
async def test_inspect_splits_own_from_shared_and_writes_nothing(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    ns = f"in-{uuid.uuid4().hex[:6]}"
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="inspector")
    auth = {"Authorization": f"Bearer {token}"}
    base = {"org": ORG, "namespace": ns}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30, headers=auth) as c:

        async def post(op: str, body: dict[str, Any]) -> dict[str, Any]:
            r = await c.post(f"/v1/{op}", json={**base, **body})
            assert r.status_code == 200, (op, r.text[:400])
            return r.json()

        await post(
            "memory.set",
            {
                "agent": "bot",
                "user_id": "u1",
                "memory_key": "u1/pref",
                "value": "Prefers email.",
                "tags": ["contact"],
            },
        )
        await post(
            "memory.set",
            {"agent": "bot", "user_id": "u2", "memory_key": "u2/pref", "value": "Prefers phone."},
        )
        await post(
            "memory.set",
            {"agent": "other", "memory_key": "team/deploy", "value": "Deploys on Tuesdays."},
        )
        await post(
            "namespace.guide_set",
            {
                "content": "# Guide\nKeys are <user>/<kind>.",
                "summary": "key layout",
                "expected_revision": 0,
            },
        )
        await post("namespace.register_category", {"name": "contacts"})
        await post("memory.assign_category", {"memory_key": "u1/pref", "category_name": "contacts"})
        held = await post("memory.get", {"agent": "bot", "user_id": "u1", "memory_key": "u1/pref"})

        def rows() -> int:
            return int(
                ch_client.query(
                    "SELECT count() FROM loom.memory WHERE org = {org:String}",
                    parameters={"org": ORG},
                ).result_rows[0][0]
            )

        before = rows()
        agent = await post("memory.inspect", {"subject": "agent", "id": "bot"})
        assert agent["own"]["live_rows"] == 2 and agent["shared"]["live_rows"] == 1
        assert {r["memory_key"] for r in agent["own"]["newest"]} == {"u1/pref", "u2/pref"}
        assert [r["memory_key"] for r in agent["shared"]["newest"]] == ["team/deploy"]
        assert "not a read boundary" in agent["own"]["definition"]

        user = await post("memory.inspect", {"subject": "user", "id": "u1"})
        assert user["own"]["live_rows"] == 1 and user["shared"]["live_rows"] == 2
        row = user["own"]["newest"][0]
        assert (row["memory_id"], row["content_sha256"]) == (
            held["memory_id"],
            held["content_sha256"],
        )
        assert row["versions"] >= 1 and row["excerpt"] == "Prefers email."
        assert [(t["name"], t["rows"]) for t in user["own"]["top_tags"]] == [("contact", 1)]
        assert user["own"]["complete"] is True
        assert [(c["name"], c["rows"]) for c in user["own"]["categories"]] == [("contacts", 1)]
        assert user["shared"]["categories"] == []

        settings = user["settings"]
        assert settings["guide_revision"] == 1 and settings["guide_summary"] == "key layout"
        assert settings["categories_registered"] == 1
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as anon:
            anonymous = await anon.post(
                "/v1/memory.get", json={**base, "agent": "bot", "memory_key": "u1/pref"}
            )
        if anonymous.status_code == 200:
            # Auth is off: every caller may read the policy, and none is set.
            assert settings["extraction_policy"] == ""
        else:
            assert settings["extraction_policy"] is None
            assert "org admin" in settings["extraction_policy_unavailable"]
        assert user["skipped"] == []

        nobody = await post("memory.inspect", {"subject": "session", "id": "never-used"})
        assert nobody["own"]["live_rows"] == 0 and nobody["own"]["newest"] == []
        assert nobody["shared"]["live_rows"] == 3

        # The ledger is flushed in batches: wait for the agent's writes to land. The
        # inspections above name the agent too, and are not its calls.
        for _ in range(40):
            calls = {
                x["name"]: x["rows"]
                for x in (await post("memory.inspect", {"subject": "agent", "id": "bot"}))[
                    "activity"
                ]["calls"]
            }
            if calls.get("memory.set"):
                break
            await asyncio.sleep(0.5)
        assert calls.get("memory.set") == 2 and "memory.inspect" not in calls, calls

        r = await c.post("/v1/memory.inspect", json={**base, "subject": "user", "id": "-"})
        assert r.status_code == 422, r.text[:300]
        assert rows() == before


@pytest.mark.asyncio
async def test_a_narrowed_or_foreign_token_cannot_inspect(
    control_plane_url: str, mint_test_token: Any
) -> None:
    from server.auth.jwt import mint_token

    ns = f"in-{uuid.uuid4().hex[:6]}"
    body = {"org": ORG, "namespace": ns, "subject": "user", "id": "u1"}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        anonymous = await c.post("/v1/memory.inspect", json=body)
        if anonymous.status_code == 200:
            pytest.skip("the server does not enforce auth")
        # A user pinned to their own rows would read the rest of the namespace through
        # `shared`, so a narrowed token is refused outright.
        pinned = mint_token(
            subject="sa:inspect-pinned",
            org=ORG,
            namespaces=[ns],
            roles=["writer"],
            ttl_seconds=600,
            is_service_account=True,
            read_scope="own_user",
            read_user_id="u1",
        )
        foreign = mint_test_token(org=ORG, namespaces=[f"{ns}-other"], roles=["writer"])
        for token in (pinned, foreign):
            r = await c.post(
                "/v1/memory.inspect", json=body, headers={"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 403, r.text[:300]
