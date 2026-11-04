"""memory.inspect against a live Loom + ClickHouse.

Three agents write into one namespace, one of them for two end users and one a
high-sensitivity row. Inspecting an agent lists its own rows apart from the other
agents'; inspecting a user lists the rows filed under that user apart from the rest; each
listed row carries the memory_id, version and content hash memory.get serves; the guide
and categories are read; the extraction policy is not, for a writer in any auth mode;
and the inspection changes no memory row, writing only the exposure-ledger row for what
it listed.
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

from tests.integration._resources import memory_rows, unique_org  # noqa: E402

ORG = unique_org("inspect")


@pytest.mark.asyncio
async def test_inspect_splits_own_from_others_and_changes_nothing(
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
                "session_id": "s1",
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
        # An object with a null field: the JSON column drops the field, so the hash cited
        # must be the one memory.get serves, not the stored column.
        await post(
            "memory.set",
            {"agent": "other", "memory_key": "team/oncall", "value": {"who": "ana", "until": None}},
        )
        await post(
            "memory.set",
            {
                "agent": "vault",
                "memory_key": "team/pin",
                "value": "4921",
                "sensitivity": "high",
            },
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
        held = await post(
            "memory.get",
            {"agent": "bot", "user_id": "u1", "session_id": "s1", "memory_key": "u1/pref"},
        )

        before = memory_rows(ch_client, ORG)
        agent = await post("memory.inspect", {"subject": "agent", "id": "bot"})
        assert agent["own"]["live_rows"] == 2 and agent["others"]["live_rows"] == 3
        assert {r["memory_key"] for r in agent["own"]["newest"]} == {"u1/pref", "u2/pref"}
        assert {r["memory_key"] for r in agent["others"]["newest"]} == {
            "team/deploy",
            "team/oncall",
            "team/pin",
        }
        assert "not a read boundary" in agent["own"]["definition"]
        # A high-sensitivity row is listed without its excerpt or its hash.
        (pin,) = [r for r in agent["others"]["newest"] if r["memory_key"] == "team/pin"]
        assert pin["excerpt"] == "" and pin["content_sha256"] == ""
        assert pin["sensitivity"] == "high"
        (oncall,) = [r for r in agent["others"]["newest"] if r["memory_key"] == "team/oncall"]
        served = await post("memory.get", {"agent": "other", "memory_key": "team/oncall"})
        assert oncall["content_sha256"] == served["content_sha256"] != ""

        user = await post("memory.inspect", {"subject": "user", "id": "u1"})
        assert user["own"]["live_rows"] == 1 and user["others"]["live_rows"] == 4
        row = user["own"]["newest"][0]
        assert (row["memory_id"], row["content_sha256"]) == (
            held["memory_id"],
            held["content_sha256"],
        )
        # Every version the history table holds, an entity tagger's re-emit included.
        held_versions = ch_client.query(
            "SELECT uniqExact(version) FROM loom.memory_versions "
            "WHERE org = {org:String} AND memory_id = {id:UUID}",
            parameters={"org": ORG, "id": held["memory_id"]},
        ).result_rows[0][0]
        assert row["versions"] == held_versions >= 1 and row["excerpt"] == "Prefers email."
        assert [(t["name"], t["rows"]) for t in user["own"]["top_tags"]] == [("contact", 1)]
        assert user["own"]["complete"] is True
        assert [(c["name"], c["rows"]) for c in user["own"]["categories"]] == [("contacts", 1)]
        assert user["others"]["categories"] == []

        settings = user["settings"]
        assert settings["guide_revision"] == 1 and settings["guide_summary"] == "key layout"
        assert settings["categories_registered"] == 1
        # The server checks a token it is sent even with auth off, so a writer never
        # reads the policy.
        assert settings["extraction_policy"] is None
        assert "org admin" in settings["extraction_policy_unavailable"]
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as anon:
            # An inspection names no agent, so the probe adds no call to the agent's count.
            anonymous = await anon.post(
                "/v1/memory.inspect", json={**base, "subject": "user", "id": "u1"}
            )
        # 200 with auth off, 401 with it on.
        assert anonymous.status_code in (200, 401), anonymous.text[:300]
        if anonymous.status_code == 200:
            # Auth is off: a caller with no token may read the policy, and none is set.
            anonymous_settings = anonymous.json()["settings"]
            assert anonymous_settings["extraction_policy"] == "", anonymous_settings
        assert user["skipped"] == []

        session = await post("memory.inspect", {"subject": "session", "id": "s1"})
        assert session["own"]["live_rows"] == 1 and session["others"]["live_rows"] == 4
        assert [r["memory_key"] for r in session["own"]["newest"]] == ["u1/pref"]

        nobody = await post("memory.inspect", {"subject": "session", "id": "never-used"})
        assert nobody["own"]["live_rows"] == 0 and nobody["own"]["newest"] == []
        assert nobody["others"]["live_rows"] == 5

        # The ledger is flushed in batches: wait for every call that names the agent, its
        # two writes and the read above. An inspection names no agent and is not counted.
        expected = {"memory.set": 2, "memory.get": 1}
        for _ in range(40):
            calls = {
                x["operation"]: x["calls"]
                for x in (await post("memory.inspect", {"subject": "agent", "id": "bot"}))[
                    "activity"
                ]["calls"]
            }
            if calls == expected:
                break
            await asyncio.sleep(0.5)
        assert calls == expected, calls

        r = await c.post("/v1/memory.inspect", json={**base, "subject": "user", "id": "-"})
        assert r.status_code == 422, r.text[:300]
        assert memory_rows(ch_client, ORG) == before

        # Each inspection that listed rows left one exposure-ledger row with their keys.
        for _ in range(40):
            listed = ch_client.query(
                "SELECT groupUniqArrayArray(returned_keys) FROM loom.memory_exposures "
                "WHERE org = {org:String} AND namespace = {ns:String} "
                "AND operation = 'memory.inspect'",
                parameters={"org": ORG, "ns": ns},
            ).result_rows[0][0]
            if "team/pin" in listed:
                break
            await asyncio.sleep(0.5)
        assert {"u1/pref", "u2/pref", "team/deploy", "team/pin"} <= set(listed)


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
        # `others`, so a narrowed token is refused outright.
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
