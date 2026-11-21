"""Real cell administration through OAuth, the directory and two HTTPS cells.

The PostgreSQL CI matrix supplies the disposable services. Each cell here has its
own migrated PostgreSQL and ClickHouse databases, root, bot keys and public origin.
No authentication, registration, business handler or endpoint is replaced.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.cli import cell_bot_proof, cell_each_run
from server.cli import cell_each_http as wire
from server.cli.cell_each import parse_command
from server.cli.cell_each_state import read_state
from tests.integration._cell_each_fleet import (
    access,
    admin_read,
    bot_key,
    fleet,
    router_trust,
    target,
)
from tests.integration._spawn import ROOT

SETTING = "memory_ask_recency_weight"


@pytest.fixture(scope="module")
def cells(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    with fleet(tmp_path_factory.mktemp("cell-each") / "private") as value:
        yield value


def _row(cells: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    rows = admin_read(cells, node, "settings.list", {"prefix": SETTING})["settings"]
    assert len(rows) == 1
    return rows[0]


def _history(cells: dict[str, Any], node: dict[str, Any]) -> list[dict[str, Any]]:
    return admin_read(cells, node, "settings.history", {"name": SETTING, "limit": 50})["changes"]


def _argv(cells: dict[str, Any], state: Path, value: str) -> list[str]:
    return [
        "--router",
        cells["router"],
        "--credentials",
        str(cells["manifest"]),
        "--state",
        str(state),
        "--",
        "settings",
        "set",
        SETTING,
        value,
    ]


def test_real_provisioning_short_tokens_and_each_write_readback(cells):
    nodes = cells["nodes"]
    assert nodes[0]["deployment_id"] != nodes[1]["deployment_id"]
    separate_pg = nodes[0]["env"]["LOOM_PG_DSN"] != nodes[1]["env"]["LOOM_PG_DSN"]
    assert separate_pg
    assert nodes[0]["env"]["LOOM_CH_DATABASE"] != nodes[1]["env"]["LOOM_CH_DATABASE"]
    before = [_row(cells, node) for node in nodes]
    for node in nodes:
        asyncio.run(access(cells, node))
        pair = asyncio.run(
            cell_bot_proof.prove_pair(
                cell=node["cell"],
                deployment_id=node["deployment_id"],
                origin=node["origin"],
                automation=bot_key(node["automation"]),
                recovery=bot_key(node["recovery"]),
                ca_file=cells["certificate"][0],
            )
        )
        assert pair == [
            {"purpose": p, **{k: node[p][k] for k in ("name", "thumbprint")}}
            for p in ("automation", "recovery")
        ]
    # The actual client receives no native connection or root credential.
    env = {key: value for key, value in os.environ.items() if not key.startswith("LOOM_")}
    state = cells["home"] / "completed.json"
    result = subprocess.run(
        [sys.executable, "-m", "server.cli", "cell", "each", *_argv(cells, state, "0.625")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    saved = read_state(state)
    assert {name: row["status"] for name, row in saved["outcomes"].items()} == {
        "alpha": "completed",
        "beta": "completed",
    }
    for node, old in zip(nodes, before, strict=True):
        assert _row(cells, node) == {**old, "value": 0.625, "source": "override"}
        completion = saved["outcomes"][node["cell"]]["completion"]
        assert completion["started_audit"] != completion["completed_audit"]


def test_wrong_key_and_mismatched_deployment_refuse_without_effect(cells):
    alpha, beta = cells["nodes"]
    before = _row(cells, alpha)

    async def refused():
        async with httpx.AsyncClient(
            verify=wire._tls({"ca_file": cells["certificate"][0]}), trust_env=False
        ) as client:
            # This is a real invalid signature for alpha's registered client, not
            # a local thumbprint mismatch that never contacts the token endpoint.
            assertion = wire.bot_assertion(
                beta["automation"]["key_file"].read_bytes(),
                name=alpha["automation"]["name"],
                thumbprint=beta["automation"]["thumbprint"],
                endpoint=alpha["origin"] + "/oauth/token",
            )
            answer = await client.post(
                alpha["origin"] + "/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": alpha["automation"]["name"],
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                    "expires_in": "60",
                },
            )
            assert answer.status_code == 401
            token = await access(cells, alpha)
            context = wire._context(target(alpha))
            context["expected_deployment_id"] = beta["deployment_id"]
            answer = await client.post(
                alpha["origin"] + "/v1/admin/cell.capabilities",
                headers={"Authorization": "Bearer " + token},
                json={"context": context},
            )
            assert answer.status_code == 403
            assert answer.json()["detail"] == {
                "code": "cell_identity_changed",
                "category": "auth",
                "retryable": False,
            }

    asyncio.run(refused())
    assert _row(cells, alpha) == before


def test_lost_real_business_reply_stays_unsettled_and_resume_never_replays(cells, monkeypatch):
    nodes = cells["nodes"]
    before = [_row(cells, node) for node in nodes]
    state = cells["home"] / "uncertain.json"
    original = httpx.AsyncHTTPTransport
    sent: list[str] = []
    lost: list[tuple[int, str]] = []

    class LostReply(httpx.AsyncBaseTransport):
        def __init__(self, **kwargs):
            self.real = original(**kwargs)

        async def handle_async_request(self, request):
            response = await self.real.handle_async_request(request)
            if request.url.path == "/v1/admin/cell.settings_set":
                sent.append(str(request.url))
                if request.url.port == int(nodes[1]["origin"].rsplit(":", 1)[1]):
                    try:
                        await response.aread()
                        lost.append((response.status_code, json.loads(response.content)["status"]))
                    finally:
                        await response.aclose()
                    raise httpx.ReadError(
                        "Reply lost after the real acknowledged write", request=request
                    )
            return response

        async def aclose(self):
            await self.real.aclose()

    with monkeypatch.context() as patch:
        patch.setattr(httpx, "AsyncHTTPTransport", LostReply)
        assert cell_each_run.main(_argv(cells, state, "0.375")) == 1
    assert sent == [node["origin"] + "/v1/admin/cell.settings_set" for node in nodes]
    assert lost == [(200, "completed")]
    saved = read_state(state)
    assert saved["outcomes"]["alpha"]["status"] == "completed"
    assert saved["outcomes"]["beta"]["status"] == "UNSETTLED"
    for node, old in zip(nodes, before, strict=True):
        assert _row(cells, node) == {**old, "value": 0.375, "source": "override"}
    histories = [_history(cells, node) for node in nodes]
    original_state = state.read_bytes()

    def no_transport(**_kwargs):
        pytest.fail("UNSETTLED resume attempted network access")

    with monkeypatch.context() as patch:
        patch.setattr(httpx, "AsyncHTTPTransport", no_transport)
        assert (
            cell_each_run.main(
                [
                    "--router",
                    cells["router"],
                    "--credentials",
                    str(cells["home"] / "missing.json"),
                    "--resume",
                    str(state),
                ]
            )
            == 1
        )
    assert state.read_bytes() == original_state
    assert [_history(cells, node) for node in nodes] == histories


@pytest.mark.parametrize("fault", ["untrusted", "wrong-host"])
def test_router_tls_refusal_precedes_business(cells, fault):
    node = cells["nodes"][1]
    before = _row(cells, node)
    history = _history(cells, node)
    command = parse_command(["settings", "set", SETTING, "0.8125"])

    async def refused():
        token = await access(cells, node)
        context = {
            **wire._context(target(node)),
            "run_id": str(uuid.uuid4()),
            "attempt": 1,
            "command_digest": command.digest,
        }
        async with httpx.AsyncClient(
            verify=wire._tls({"ca_file": cells["certificate"][0]}), trust_env=False
        ) as client:
            answer = await client.post(
                node["origin"] + "/v1/admin/cell.settings_set",
                headers={"Authorization": "Bearer " + token},
                json={"context": context, **command.arguments},
                timeout=35,
            )
            assert answer.status_code == 503
            result = answer.json()
            assert result["status"] == "refused"
            assert result["phase"] == "before_business"
            assert result["code"] == "security_unavailable"

    with router_trust(cells, node, fault):
        asyncio.run(refused())
        assert _row(cells, node) == before
        assert _history(cells, node) == history
