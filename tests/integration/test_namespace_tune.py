"""namespace.tune against a live Loom + ClickHouse.

A namespace whose writer double-encodes values by hand and tags rows `billing`, with no
guide and no categories: tune drafts a `billing` category and a guide from the key
layout, and no extraction rule, before and after memory.set_from_messages writes there,
since none of the flagged rows came from extraction; `skipped` says so once it writes.
Each draft cites live revisions memory.get serves, names its write and guard, and the
first tune writes nothing: the memory rows, the guide and the categories are as they
were. A token narrowed to one user and a token for another namespace are refused.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("tune")


@pytest.mark.asyncio
async def test_tune_drafts_from_the_rows_and_changes_nothing(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    ns = f"tu-{uuid.uuid4().hex[:6]}"
    writer = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="writer")
    # namespace.tune is a viewer op: the drafts are read with a viewer's token.
    viewer = mint_test_token(org=ORG, namespaces=[ns], roles=["viewer"], subject="tuner")
    base = {"org": ORG, "namespace": ns}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:

        async def post(op: str, body: dict[str, Any], token: str = writer) -> dict[str, Any]:
            r = await c.post(
                f"/v1/{op}",
                json={**base, **body},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, (op, r.text[:400])
            return r.json()

        # Eight double-encoded invoices tagged `billing`, and two plain rows.
        for n in range(8):
            await post(
                "memory.set",
                {
                    "agent": "importer",
                    "memory_key": f"billing/inv-{n}",
                    "value": json.dumps({"invoice": n, "total": 10 * n}),
                    "tags": ["billing"],
                },
            )
        for n in range(2):
            await post(
                "memory.set",
                {
                    "agent": "bot",
                    "memory_key": f"billing/note-{n}",
                    "value": f"Invoices go out on day {n + 1}.",
                    "tags": ["billing"],
                },
            )

        def counts() -> tuple[int, int]:
            # FINAL: a background merge, or a worker's new version of a row (entity
            # extraction), must not read as a change.
            rows = ch_client.query(
                "SELECT count() FROM loom.memory FINAL WHERE org = {org:String}",
                parameters={"org": ORG},
            ).result_rows[0][0]
            cats = ch_client.query(
                "SELECT count() FROM loom.categories FINAL WHERE org = {org:String}",
                parameters={"org": ORG},
            ).result_rows[0][0]
            return int(rows), int(cats)

        before = counts()
        out = await post("namespace.tune", {"sample_limit": 3}, viewer)
        assert out["model_used"] is False
        # Extraction writes nothing here yet: the one skipped note says no rule is drafted.
        assert [s.split(",")[0] for s in out["skipped"]] == [
            "extraction: memory.set_from_messages writes no row here"
        ], out["skipped"]
        by_target = {p["target"]: p for p in out["proposals"]}

        # The double-encoded rows were written by hand (memory.set), which extraction
        # instructions never reach: no extraction rule is drafted for them.
        assert "extraction_instructions.request" not in by_target

        assert "org admin" in out["extraction_policy_unavailable"]

        category = by_target["namespace.category"]
        assert category["proposed"] == "billing" and category["evidence"]["supporting_count"] == 10
        assert category["apply"]["op"] == "namespace.register_category"
        assert set(category["affected_agents"]) == {"importer", "bot"}
        cited = category["evidence"]["supporting"][0]
        served = await post(
            "memory.get", {"agent": cited["agent"], "memory_key": cited["memory_key"]}
        )
        assert (cited["memory_id"], cited["version"], cited["content_sha256"]) == (
            served["memory_id"],
            served["version"],
            served["content_sha256"],
        )

        guide = by_target["namespace.guide"]
        assert guide["current_revision"] == 0 and guide["apply"]["args"]["expected_revision"] == 0
        assert "`billing/…`: 10 rows" in guide["proposed"]

        assert counts() == before
        assert (await post("namespace.guide_get", {}))["found"] is False

        # Extraction now writes here, but none of the flagged rows came from it: the rule
        # would rest on no row it reaches, so none is drafted, and `skipped` says why.
        await post(
            "memory.set_from_messages",
            {
                "agent": "chat",
                "messages": [{"role": "user", "content": "Invoices go out on Fridays."}],
            },
        )
        out = await post("namespace.tune", {"sample_limit": 3}, viewer)
        assert not [p for p in out["proposals"] if p["target"] == "extraction_instructions.request"]
        assert any(
            s.startswith("extraction: no cited row of double_encoded_values")
            for s in out["skipped"]
        ), out["skipped"]


@pytest.mark.asyncio
async def test_a_narrowed_or_foreign_token_cannot_tune(
    control_plane_url: str, mint_test_token: Any
) -> None:
    from server.auth.jwt import mint_token

    ns = f"tu-{uuid.uuid4().hex[:6]}"
    body = {"org": ORG, "namespace": ns}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        anonymous = await c.post("/v1/namespace.tune", json=body)
        if anonymous.status_code == 200:
            pytest.skip("the server does not enforce auth")
        pinned = mint_token(
            subject="sa:tune-pinned",
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
                "/v1/namespace.tune", json=body, headers={"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 403, r.text[:300]
