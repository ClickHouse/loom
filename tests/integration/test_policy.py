"""History policy over the wire: set / list / check / decisions
/ delete against a live Control Plane. Mode-adaptive: the enforcement
assertions run only when the server reports `enforce`; the dry run and the
ledger are exercised in every mode."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.integration._resources import unique_org

ORG = unique_org("policy")


def _post(client: httpx.Client, op: str, body: dict) -> httpx.Response:
    return client.post(f"/v1/{op}", json=body)


@pytest.fixture
def ns() -> str:
    return f"pol-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def client(control_plane_url: str, mint_test_token):
    # policy.set / policy.delete are ADMIN; under LOOM_AUTH_REQUIRED=false the
    # header is ignored, under =true it is what lets the suite administer rules.
    tok = mint_test_token(org=ORG, namespaces=["*"], roles=["admin"])
    with httpx.Client(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {tok}"},
        timeout=15.0,
    ) as c:
        yield c


def _require_rule(pid: str = "read-before-write") -> dict:
    return {
        "policy_id": pid,
        "effect": "require",
        "actions": ["memory.set"],
        "condition": {
            "op": "formerly",
            "within": "1h",
            "operation": ["memory.get", "memory.search"],
        },
        "description": "a set must follow a read by the same agent within the hour",
    }


def test_set_list_check_delete_round_trip(client: httpx.Client, ns: str) -> None:
    body = {"org": ORG, "namespace": ns, **_require_rule()}
    r = _post(client, "policy.set", body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rule"]["policy_id"] == "read-before-write" and out["leaf_count"] == 1

    r = _post(client, "policy.list", {"org": ORG, "namespace": ns})
    assert r.status_code == 200, r.text
    listed = r.json()
    assert [x["policy_id"] for x in listed["rules"]] == ["read-before-write"]
    assert listed["mode"] in ("off", "shadow", "enforce")

    # Dry run: a fresh agent has no read in its history → the require denies.
    agent = f"agent-{uuid.uuid4().hex[:6]}"
    r = _post(
        client,
        "policy.check",
        {
            "org": ORG,
            "namespace": ns,
            "operation": "memory.set",
            "agent": agent,
            "args": {"memory_key": "k", "value": "v"},
        },
    )
    assert r.status_code == 200, r.text
    chk = r.json()
    assert chk["decision"] == "deny" and chk["determining_rules"] == ["read-before-write"]
    assert chk["errors"] == [] and chk["write_shaped"] is True
    assert chk["rules"][0]["leaves"][0]["op"] == "formerly"
    assert chk["rules"][0]["leaves"][0]["value"] == 0

    # A read by that agent lands in agent_events; the same dry run then allows.
    r = _post(
        client, "memory.get", {"org": ORG, "namespace": ns, "agent": agent, "memory_key": "k"}
    )
    assert r.status_code in (200, 404), r.text
    r = _post(
        client,
        "policy.check",
        {
            "org": ORG,
            "namespace": ns,
            "operation": "memory.set",
            "agent": agent,
            "args": {"memory_key": "k"},
        },
    )
    assert r.status_code == 200, r.text
    # policy.check drains this replica's event queue first, so the read the
    # line above produced is already history.
    assert r.json()["decision"] == "allow", r.text

    r = _post(
        client, "policy.delete", {"org": ORG, "namespace": ns, "policy_id": "read-before-write"}
    )
    assert r.status_code == 200 and r.json()["removed"] is True
    r = _post(client, "policy.list", {"org": ORG, "namespace": ns})
    assert r.json()["rules"] == []


def test_check_validates_inline_rules_and_rejects_bad_ones(client: httpx.Client, ns: str) -> None:
    r = _post(
        client,
        "policy.check",
        {
            "org": ORG,
            "namespace": ns,
            "operation": "memory.delete",
            "agent": "x",
            "rules": [
                {
                    "policy_id": "cap",
                    "effect": "forbid",
                    "actions": ["memory.*"],
                    "condition": {
                        "op": "count_within",
                        "within": "1h",
                        "operation": "memory.delete",
                        "cmp": "gte",
                        "value": 1,
                    },
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "allow"  # a fresh agent has deleted nothing
    r = _post(
        client,
        "policy.set",
        {
            "org": ORG,
            "namespace": ns,
            "policy_id": "bad",
            "effect": "forbid",
            "actions": ["memory.*"],
            "condition": {"op": "formerly", "within": "9d", "operation": "x"},
        },
    )
    assert r.status_code == 422, r.text
    assert "exceeds policy_max_window_seconds" in r.text


def test_gate_behaviour_matches_the_reported_mode(client: httpx.Client, ns: str) -> None:
    """In `enforce`, a write without the required read is a structured 403 and
    the decision lands in the ledger; in `shadow`, the write succeeds and the
    would-be denial lands in the ledger; in `off`, nothing is recorded."""
    _post(client, "policy.set", {"org": ORG, "namespace": ns, **_require_rule()})
    mode = _post(client, "policy.list", {"org": ORG, "namespace": ns}).json()["mode"]
    agent = f"agent-{uuid.uuid4().hex[:6]}"
    r = _post(
        client,
        "memory.set",
        {"org": ORG, "namespace": ns, "agent": agent, "memory_key": "k", "value": "first write"},
    )
    if mode == "enforce":
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "POLICY_FORBIDDEN"
        assert detail["decision"] == "deny"
        assert detail["determining_rules"] == ["read-before-write"]
        assert detail["errors"] == []
        # a read, then the write goes through
        _post(
            client, "memory.get", {"org": ORG, "namespace": ns, "agent": agent, "memory_key": "k"}
        )
        r2 = _post(
            client,
            "memory.set",
            {"org": ORG, "namespace": ns, "agent": agent, "memory_key": "k", "value": "second"},
        )
        assert r2.status_code == 200, r2.text
    else:
        assert r.status_code == 200, r.text

    dec = _post(client, "policy.decisions", {"org": ORG, "namespace": ns}).json()["rows"]
    if mode == "off":
        assert dec == []
    else:
        denied = [d for d in dec if d["decision"] == "deny" and d["agent"] == agent]
        assert denied, dec
        assert denied[0]["mode"] == mode
        assert denied[0]["enforced"] is (mode == "enforce")
        assert denied[0]["operation"] == "memory.set"
    _post(client, "policy.delete", {"org": ORG, "namespace": ns, "policy_id": "read-before-write"})
