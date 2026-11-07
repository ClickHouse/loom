"""memory.lock end to end against a live Loom + ClickHouse.

set → lock read_only → every guarded write answers 423 LOCKED with the
structured body → an identical restatement still passes → memory.get shows
the lock → a writer cannot loosen, an admin can → clear leaves the locked
row and reports it → after unlock the delete goes through.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

ORG = unique_org("lock")


def _post(client: httpx.Client, op: str, body: dict) -> httpx.Response:
    return client.post(f"/v1/{op}", json=body)


@pytest.fixture
def ns() -> str:
    return f"lk-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def writer(control_plane_url: str, mint_test_token):
    tok = mint_test_token(org=ORG, namespaces=["*"], roles=["writer"], subject="w1")
    with httpx.Client(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {tok}"}, timeout=30.0
    ) as c:
        yield c


@pytest.fixture
def admin(control_plane_url: str, mint_test_token):
    tok = mint_test_token(org=ORG, namespaces=["*"], roles=["admin"], subject="a1")
    with httpx.Client(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {tok}"}, timeout=30.0
    ) as c:
        yield c


def _ident(ns: str, **extra) -> dict:
    return {"org": ORG, "namespace": ns, "agent": "t", **extra}


def test_lock_is_refused_at_the_write(writer: httpx.Client, admin: httpx.Client, ns: str) -> None:
    key = "deploy_window"
    r = _post(
        writer,
        "memory.set",
        _ident(
            ns, memory_key=key, value="Tue and Thu only", memory_type="semantic", classify=False
        ),
    )
    assert r.status_code == 200, r.text
    mid = r.json()["memory_id"]

    r = _post(
        writer, "memory.lock", _ident(ns, memory_key=key, mode="read_only", reason="legal hold")
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["locked"] and out["lock_mode"] == "read_only" and out["previous_mode"] == ""
    assert out["locked_by"] == "process:w1"  # the token decides, not the body

    got = _post(writer, "memory.get", _ident(ns, memory_key=key)).json()
    assert got["found"] and got["lock_mode"] == "read_only"
    assert got["lock_reason"] == "legal hold" and got["locked_by"] == "process:w1"

    # every guarded write
    for op, body in (
        ("memory.delete", _ident(ns, memory_key=key)),
        ("memory.touch", _ident(ns, memory_key=key, ttl_seconds=600)),
        (
            "memory.set",
            _ident(ns, memory_key=key, value="Fridays too", memory_type="semantic", classify=False),
        ),
        ("memory.update_by_id", {"org": ORG, "memory_id": mid, "value": "Fridays too"}),
        ("memory.reject", _ident(ns, memory_key=key, rejection_reason="wrong")),
    ):
        r = _post(writer, op, body)
        assert r.status_code == 423, (op, r.text)
        d = r.json()["detail"]
        assert d["code"] == "LOCKED" and d["lock_mode"] == "read_only"
        assert d["memory_key"] == key and d["unlock_via"]

    # an identical restatement is reinforcement, not a write of the value
    r = _post(
        writer,
        "memory.set",
        _ident(
            ns, memory_key=key, value="Tue and Thu only", memory_type="semantic", classify=False
        ),
    )
    assert r.status_code == 200, r.text
    got = _post(writer, "memory.get", _ident(ns, memory_key=key)).json()
    assert got["value"] == "Tue and Thu only" and got["lock_mode"] == "read_only"

    # a writer cannot loosen; the op is admin-only anyway
    r = _post(writer, "memory.lock", _ident(ns, memory_key=key, mode="no_delete", reason="relax"))
    assert r.status_code == 423 and r.json()["detail"]["refused_action"] == "loosen"
    r = _post(writer, "memory.unlock", _ident(ns, memory_key=key))
    assert r.status_code == 403, r.text

    # clear leaves the locked row and says so
    _post(
        writer,
        "memory.set",
        _ident(ns, memory_key="scratch", value="x", memory_type="semantic", classify=False),
    )
    r = _post(
        admin, "memory.clear", {"org": ORG, "namespace": ns, "agent": "t", "confirm_subject": "t"}
    )
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["locked_skipped"] == 1 and c["rows_affected"]["loom.memory"] >= 1
    assert _post(writer, "memory.get", _ident(ns, memory_key=key)).json()["found"]
    assert not _post(writer, "memory.get", _ident(ns, memory_key="scratch")).json()["found"]

    # admin unlocks; the delete now goes through and history shows the lock
    r = _post(admin, "memory.unlock", _ident(ns, memory_key=key))
    assert (
        r.status_code == 200 and r.json()["unlocked"] and r.json()["previous_mode"] == "read_only"
    )
    got = _post(writer, "memory.get", _ident(ns, memory_key=key)).json()
    assert got["lock_mode"] == ""
    r = _post(writer, "memory.delete", _ident(ns, memory_key=key))
    assert r.status_code == 200 and r.json()["tombstoned"] is True


@pytest.mark.slow
def test_no_delete_and_a_lapsing_lock(writer: httpx.Client, ns: str) -> None:
    key = "runbook"
    _post(
        writer,
        "memory.set",
        _ident(ns, memory_key=key, value="v1", memory_type="semantic", classify=False),
    )
    # 3 s, not 1: the 423 below is asserted only while the lock still holds,
    # and on a slow runner the get→delete pair alone can take longer than a 1 s
    # lock.
    r = _post(
        writer,
        "memory.lock",
        _ident(ns, memory_key=key, mode="no_delete", reason="keep", ttl_seconds=3),
    )
    assert r.status_code == 200 and r.json()["lock_until"]
    # the value may change under no_delete
    r = _post(
        writer,
        "memory.set",
        _ident(ns, memory_key=key, value="v2", memory_type="semantic", classify=False),
    )
    assert r.status_code == 200, r.text
    # the lock rides the new version row
    got = _post(writer, "memory.get", _ident(ns, memory_key=key)).json()
    assert got["value"] == "v2"
    if got["lock_mode"]:  # not yet lapsed
        for op in ("memory.delete", "memory.reject"):
            r = _post(writer, op, _ident(ns, memory_key=key))
            assert r.status_code == 423, (op, r.text)
    # lapsed = no lock; poll the lapse rather than sleeping past the TTL
    wait_until(
        lambda: _post(writer, "memory.get", _ident(ns, memory_key=key)).json()["lock_mode"] == "",
        max_wait=15,
        interval=0.25,
        description="the 3 s lock to lapse",
    )
    r = _post(writer, "memory.delete", _ident(ns, memory_key=key))
    assert r.status_code == 200 and r.json()["tombstoned"] is True


def test_a_star_agent_clear_leaves_only_the_locked_identity(
    writer: httpx.Client, admin: httpx.Client, ns: str
) -> None:
    """One user's key under two agents, locked under one: agent="*" clears the
    other agent's copy and every other key, and reports the locked row."""

    def ident(agent: str, **extra) -> dict:
        return {"org": ORG, "namespace": ns, "agent": agent, "user_id": "u1", **extra}

    for agent, key in (("a", "deploy_window"), ("b", "deploy_window"), ("b", "oncall")):
        body = ident(agent, memory_key=key, value=f"{agent} {key}", memory_type="semantic")
        r = _post(writer, "memory.set", {**body, "classify": False})
        assert r.status_code == 200, r.text
    r = _post(
        writer,
        "memory.lock",
        ident("a", memory_key="deploy_window", mode="read_only", reason="keep"),
    )
    assert r.status_code == 200 and r.json()["locked"], r.text

    r = _post(
        admin,
        "memory.clear",
        {"org": ORG, "namespace": ns, "user_id": "u1", "agent": "*", "confirm_subject": "u1"},
    )
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["locked_skipped"] == 1 and c["rows_affected"]["loom.memory"] == 2
    # Agent b's vector of the locked key goes too: only agent a's is excluded.
    assert c["rows_affected"]["loom.memory_embeddings"] == 2

    # memory.list reads one agent's rows; memory.get would fall back to another agent's.
    def keys(agent: str) -> list[str]:
        r = _post(writer, "memory.list", ident(agent))
        assert r.status_code == 200, r.text
        return sorted(e["memory_key"] for e in r.json()["entries"])

    assert keys("a") == ["deploy_window"]
    assert keys("b") == []
