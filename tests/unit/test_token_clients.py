"""The client setups a new token comes with (`server/identity/app/token_clients.py`).
The dashboard builds the same setups in frontend/src/lib/clients.ts; both are checked
against one fixture, this test for the server and clients.test.ts for the dashboard."""

from __future__ import annotations

import json
from pathlib import Path

from server.identity.app import token_clients

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/src/lib/clientSetups.fixture.json"


def test_the_mint_builds_the_setups_the_dashboard_prints() -> None:
    for case in json.loads(FIXTURE.read_text()):
        assert set(case["setups"]) == set(token_clients.MCP_CLIENTS)
        for client, want in case["setups"].items():
            got = token_clients.client_config(client, case["mcp_url"], "T")
            assert {k: got[k] for k in want} == want, (case["mcp_url"], client)
