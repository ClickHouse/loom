"""Address refresh proves listeners before the generation-fenced directory write."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import httpx
import pytest

from server.router import app, directory
from server.settings import settings
from tests.integration.test_cell_update_directory import configured as configured
from tests.integration.test_cell_update_directory import observed
from tests.integration.test_router_directory_pg import dsn as dsn


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [False, True])
async def test_address_refresh_changes_only_verified_addresses(configured, monkeypatch, first):
    name = "first" if first else "other"
    token = "lco_address-proof"
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(token.encode()).hexdigest()
    )
    monkeypatch.setattr(settings, "router_upstream_url", "http://new-main")
    monkeypatch.setattr(settings, "router_upstream_probe_url", "http://new-probe")
    expected = await observed(name)
    nonces = {"postgres": "pg", "clickhouse": "ch", "bucket": None}

    def network(request):
        if request.url.host == "new-main":
            return httpx.Response(200, json={"deployment_id": f"dep-{name}"})
        return httpx.Response(
            200,
            json={
                "deployment_id": f"dep-{name}",
                "bucket": {"name": "", "prefix": ""},
                "nonces": nonces,
            },
        )

    before = await directory.attach_read(name)
    router = app.Router(probe_transport=httpx.MockTransport(network))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.build_app(router)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/cells/v1/operator/cells/{name}/addresses",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "expected": expected.values | {"mutation_generation": str(expected.generation)},
                "probe_url": "http://new-probe",
                "upstream_url": "http://new-main" if first else before.upstream_url,
                "nonces": nonces,
            },
        )
    assert response.status_code == 200, response.text
    after = await directory.attach_read(name)
    assert after == replace(
        before,
        probe_url="http://new-probe",
        upstream_url="http://new-main" if first else before.upstream_url,
    )


@pytest.mark.asyncio
async def test_stale_address_generation_cannot_overwrite_a_later_refresh(configured):
    expected = await observed("first")
    await observed("first")
    before = await directory.attach_read("first")
    with pytest.raises(directory.OperatorRefused, match="generation"):
        await directory.update_addresses(
            "first", "http://probe", "http://main", expected=expected, configured=lambda: True
        )
    assert await directory.attach_read("first") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["main-port", "deployment", "bucket", "nonce", "upstream", "config-changed"]
)
async def test_invalid_address_proof_leaves_exact_directory_unchanged(
    configured, monkeypatch, invalid
):
    token = "lco_address-refusal"
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(token.encode()).hexdigest()
    )
    monkeypatch.setattr(settings, "router_upstream_url", "http://new-main")
    monkeypatch.setattr(settings, "router_upstream_probe_url", "http://new-probe")
    expected = await observed("first")
    nonces = {"postgres": "pg", "clickhouse": "ch", "bucket": None}
    before = await directory.attach_read("first")

    def network(request):
        if request.url.host == "new-main":
            return httpx.Response(
                200, json={"deployment_id": "wrong" if invalid == "upstream" else "dep-first"}
            )
        answer = {
            "deployment_id": "dep-first",
            "bucket": {"name": "", "prefix": ""},
            "nonces": nonces,
        }
        if invalid == "main-port":
            answer.pop("nonces")
        elif invalid == "deployment":
            answer["deployment_id"] = "other"
        elif invalid == "bucket":
            answer["bucket"] = {"name": "other", "prefix": ""}
        elif invalid == "nonce":
            answer["nonces"] = nonces | {"postgres": "old"}
        elif invalid == "config-changed":
            monkeypatch.setattr(settings, "router_upstream_url", "http://changed-main")
        return httpx.Response(200, json=answer)

    router = app.Router(probe_transport=httpx.MockTransport(network))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.build_app(router)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/cells/v1/operator/cells/first/addresses",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "expected": expected.values | {"mutation_generation": str(expected.generation)},
                "probe_url": "http://new-probe",
                "upstream_url": "http://new-main",
                "nonces": nonces,
            },
        )
    assert response.status_code == 409, response.text
    assert await directory.attach_read("first") == before


@pytest.mark.asyncio
async def test_old_router_config_can_reverse_addresses_with_fresh_expected_row(
    configured, monkeypatch
):
    # The all-router rollout prerequisite cannot be inferred from a successful CAS.
    nonces = {"postgres": "pg", "clickhouse": "ch", "bucket": None}
    token = "lco_router-rollout"
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(token.encode()).hexdigest()
    )

    def network(request):
        answer = {"deployment_id": "dep-first"}
        if "probe" in request.url.host:
            answer |= {"bucket": {"name": "", "prefix": ""}, "nonces": nonces}
        return httpx.Response(200, json=answer)

    router = app.Router(probe_transport=httpx.MockTransport(network))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.build_app(router)), base_url="http://test"
    ) as client:
        for version in ("new", "old"):
            monkeypatch.setattr(settings, "router_upstream_url", f"http://{version}-main")
            monkeypatch.setattr(settings, "router_upstream_probe_url", f"http://{version}-probe")
            expected = await observed("first")
            response = await client.post(
                "/cells/v1/operator/cells/first/addresses",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "expected": expected.values | {"mutation_generation": str(expected.generation)},
                    "probe_url": f"http://{version}-probe",
                    "upstream_url": f"http://{version}-main",
                    "nonces": nonces,
                },
            )
            assert response.status_code == 200, response.text
    assert (await directory.attach_read("first")).upstream_url == "http://old-main"
