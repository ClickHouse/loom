"""An org admin's projection-failure ops leave out a restricted namespace it cannot read.

`ops.projection_failures.*` skip the transport's namespace gate, so the handler's org
scope is what keeps a restricted namespace out. The org admin here holds no grant on
the restricted namespace: list (org-wide and naming it) does not return its failure,
and resolve leaves that failure active. The failure of an ordinary namespace in the
same org is listed and resolved, so an empty answer cannot pass by accident.

Needs a running server (LOOM_BASE_URL) with auth on and the same ClickHouse the tests
reach (LOOM_CH_*); skips otherwise.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org


def _post(base: str, token: str, op: str, body: dict[str, Any]) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=60.0) as c:
        return c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})


def _status(ch_client, failure_id: str) -> str:
    return ch_client.query(
        "SELECT status FROM loom.memory_projection_failures WHERE failure_id = {id:UUID}",
        parameters={"id": failure_id},
    ).result_rows[0][0]


def test_an_org_admin_neither_reads_nor_resolves_a_restricted_namespaces_failures(
    control_plane_url, ch_client, mint_test_token
) -> None:
    with httpx.Client(base_url=control_plane_url, timeout=60.0) as c:
        if c.post("/v1/ops.projection_failures.list", json={"org": "x"}).status_code != 401:
            pytest.skip("server runs with auth off: the org scope admits every namespace")
    org = unique_org("pfail")
    restricted, ordinary = f"priv-{uuid.uuid4().hex[:6]}", f"open-{uuid.uuid4().hex[:6]}"
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    ch_client.insert(
        "loom.namespaces",
        [
            [org, ns, "ops@pfail.invalid", "", [], True, "memory", "private", cls, now, now]
            for ns, cls in ((restricted, "restricted"), (ordinary, ""))
        ],
        column_names=[
            "org",
            "namespace",
            "owner_email",
            "description",
            "tags",
            "active",
            "kind",
            "visibility",
            "classification",
            "created_at",
            "updated_at",
        ],
    )
    hidden_id, open_id = str(uuid.uuid4()), str(uuid.uuid4())
    ch_client.insert(
        "loom.memory_projection_failures",
        [
            [hidden_id, org, restricted, "entity_edges", ["launch/code"], "secret payload"],
            [open_id, org, ordinary, "entity_edges", ["open/key"], "open payload"],
        ],
        column_names=[
            "failure_id",
            "org",
            "namespace",
            "projection",
            "memory_keys",
            "error_message",
        ],
    )
    admin = mint_test_token(org=org, namespaces=["*"], roles=["org_admin"], subject="ann")

    r = _post(control_plane_url, admin, "ops.projection_failures.list", {"org": org})
    assert r.status_code == 200, r.text[:300]
    assert [row["failure_id"] for row in r.json()["rows"]] == [open_id]
    assert "secret payload" not in r.text

    named = {"org": org, "namespace": restricted}
    r = _post(control_plane_url, admin, "ops.projection_failures.list", named)
    assert r.status_code == 200 and r.json()["rows"] == [], r.text[:300]

    ids = {"org": org, "failure_ids": [hidden_id, open_id]}
    r = _post(control_plane_url, admin, "ops.projection_failures.resolve", ids)
    assert r.status_code == 200, r.text[:300]
    # One mutation carries both ids: once it has resolved the open failure, it has
    # finished, and the restricted one is still active.
    wait_until(lambda: _status(ch_client, open_id) == "resolved", description="resolve applied")
    assert _status(ch_client, hidden_id) == "active"
