"""Operator TLS trust does not enable proxy, redirect or verification bypasses."""

from __future__ import annotations

import json
import ssl
import time

import httpx
import pytest

from server.insights.api.models_cell_admin import CellTargetContext
from server.insights.app import cell_admin_auth as authority
from server.settings import settings


@pytest.mark.asyncio
async def test_router_authority_uses_explicit_default_trust_without_proxies(monkeypatch):
    context = CellTargetContext(
        version=1, expected_cell="alpha", expected_deployment_id="deployment", expected_bot="auto"
    )
    monkeypatch.setattr(settings, "cell", "alpha")
    monkeypatch.setattr(settings, "cell_router_url", "https://router.example")
    monkeypatch.setattr(settings, "cell_router_token", "private-cell-token")
    trust = ssl.create_default_context()
    monkeypatch.setattr(authority.ssl, "create_default_context", lambda: trust)
    original = httpx.AsyncClient
    observed = []

    def reply(request):
        observed.append(request)
        value = {
            "version": 1,
            "cell": "alpha",
            "deployment_id": "deployment",
            "automation": {"name": "auto", "thumbprint": "A" * 43},
            "recovery": {"name": "recovery", "thumbprint": "B" * 43},
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(value).encode()))

    def client(**kwargs):
        assert kwargs["verify"] is trust
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert trust.verify_mode == ssl.CERT_REQUIRED and trust.check_hostname
        return original(**kwargs, transport=httpx.MockTransport(reply))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    binding = await authority._authority(context, time.monotonic() + 5)
    assert binding.name == "auto" and binding.thumbprint == "A" * 43
    assert len(observed) == 1
    assert str(observed[0].url) == "https://router.example/cells/v1/cells/alpha/administration"
    assert observed[0].headers["Authorization"] == "Bearer private-cell-token"
