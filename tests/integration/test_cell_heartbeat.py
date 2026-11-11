"""Every server reports its cell, tenancy mode and configuration fingerprint in
`loom.cell_heartbeats`, beside its `loom.instances` row, even outside a cell:
`loom cell prepare` reads the fingerprint before `LOOM_CELL` is rolled out
(docs/architecture/cells.md)."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from tests.integration._resources import ch_client_from_env


def test_the_server_reports_its_fingerprint_beside_its_instance_row(
    control_plane_url: str,
) -> None:
    if not os.environ.get("LOOM_CH_HOST"):
        pytest.skip("needs LOOM_CH_HOST: the test reads loom.cell_heartbeats")
    with httpx.Client(base_url=control_plane_url, timeout=10.0) as c:
        health = c.get("/v1/health").json()
    # /v1/health names the process as host:pid; its instance row carries both.
    host, _, pid = str(health["instance"]).rpartition(":")
    ch = ch_client_from_env()
    rows = ch.query(
        "SELECT c.cell, c.tenancy_mode, c.fingerprint FROM loom.cell_heartbeats AS c "
        "INNER JOIN loom.instances AS i ON i.instance_id = c.instance_id "
        "WHERE i.host = {h:String} AND i.pid = {p:UInt32} AND i.label = {l:String} "
        # A restarted container keeps its host, pid and label: the live process's row.
        "AND i.stopped = 0 ORDER BY c.heartbeat_at DESC LIMIT 1",
        parameters={"h": host, "p": int(pid), "l": health["instance_label"]},
    ).result_rows
    assert rows, f"no cell heartbeat for {health['instance']}"
    cell, mode, raw = rows[0]
    assert (cell, mode) == ("", "")  # the suite's server is not a cell
    value = json.loads(raw)
    assert value["v"] == 1
    fields = value["fields"]
    assert set(fields) >= {
        "public_host",
        "oauth_issuer_host",
        "oidc_issuer",
        "oidc_client_id",
        "scim",
        "bearer_issuers",
        "configured_orgs",
        "end_user_orgs",
        "deployment_id",
        "bucket",
        "bucket_prefix",
    }
    recorded = ch.query(
        "SELECT authority, deployment_id FROM loom.storage_authority FINAL "
        "ORDER BY epoch DESC LIMIT 1"
    ).result_rows
    if recorded and recorded[0][0] == "postgres":
        # The store identity a cell's fingerprint carries: the deployment id both stores record.
        assert fields["deployment_id"] == recorded[0][1]
    else:
        assert fields["deployment_id"] == ""
