"""on_expire end to end: set with a 1 s TTL and each
post-action, let it expire, run memory.sweep_expired, and read back."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

ORG = unique_org("expire")


def _post(client: httpx.Client, op: str, body: dict) -> httpx.Response:
    return client.post(f"/v1/{op}", json=body)


@pytest.fixture
def ns() -> str:
    return f"ex-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def admin(control_plane_url: str, mint_test_token):
    tok = mint_test_token(org=ORG, namespaces=["*"], roles=["admin"], subject="a1")
    with httpx.Client(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {tok}"}, timeout=30.0
    ) as c:
        yield c


def _ident(ns: str, **extra) -> dict:
    return {"org": ORG, "namespace": ns, "agent": "t", **extra}


def _set(c: httpx.Client, ns: str, key: str, value: str, **extra) -> dict:
    r = _post(
        c,
        "memory.set",
        _ident(ns, memory_key=key, value=value, memory_type="semantic", classify=False, **extra),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _wait_hidden(c: httpx.Client, ns: str, key: str) -> None:
    wait_until(
        lambda: not _post(c, "memory.get", _ident(ns, memory_key=key)).json()["found"],
        max_wait=15,
        interval=0.2,
        description=f"{key} to expire",
    )


@pytest.mark.slow
def test_each_post_action(admin: httpx.Client, ns: str) -> None:
    _set(admin, ns, "gone", "tombstone me", ttl_seconds=1)
    _set(admin, ns, "stale", "demote me", ttl_seconds=1, on_expire="demote", expire_reason="aging")
    _set(
        admin,
        ns,
        "secret",
        "anonymize me",
        ttl_seconds=1,
        on_expire="anonymize",
        expire_reason="retention",
    )
    keep = _set(admin, ns, "keep", "not expiring", on_expire="demote")
    got = _post(admin, "memory.get", _ident(ns, memory_key="keep")).json()
    assert got["on_expire"] == "demote" and got["expires_at"] is None
    # hidden from reads once the TTL passes, before any sweep
    for k in ("gone", "stale", "secret"):
        _wait_hidden(admin, ns, k)

    # update_by_id on an expired row is refused, then revived explicitly
    r = _post(
        admin,
        "memory.update_by_id",
        {"org": ORG, "memory_id": keep["memory_id"], "value": "still fine"},
    )
    assert r.status_code == 200  # not expired
    stale_id = _post(admin, "memory.history", _ident(ns, memory_key="stale")).json()
    # dry run first
    r = _post(admin, "memory.sweep_expired", {"org": ORG, "namespace": ns, "dry_run": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dry_run"] and (d["tombstoned"], d["demoted"], d["anonymized"]) == (1, 1, 1)
    assert {s["memory_key"] for s in d["sample"]} == {"gone", "stale", "secret"}
    # still hidden, nothing written
    assert not _post(admin, "memory.get", _ident(ns, memory_key="stale")).json()["found"]

    r = _post(admin, "memory.sweep_expired", {"org": ORG, "namespace": ns})
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["tombstoned"], d["demoted"], d["anonymized"], d["locked_skipped"]) == (1, 1, 1, 0)
    assert d["failures"] == []

    # tombstone: gone from get, restorable
    assert not _post(admin, "memory.get", _ident(ns, memory_key="gone")).json()["found"]
    r = _post(admin, "memory.restore", _ident(ns, memory_key="gone"))
    assert r.status_code == 200 and r.json()["restored"] is True

    # demote: back, no expiry, confidence halved, reason recorded
    got = _post(admin, "memory.get", _ident(ns, memory_key="stale")).json()
    assert got["found"] and got["expires_at"] is None and got["on_expire"] == ""
    assert abs(got["confidence"] - 0.5) < 1e-3
    assert got["expire_reason"].startswith("demoted on expiry: aging")

    # anonymize: shell row, marker value, no expiry
    got = _post(admin, "memory.get", _ident(ns, memory_key="secret")).json()
    assert got["found"] and got["value"] == "[EXPIRED]" and got["expires_at"] is None

    # a second pass finds nothing
    d = _post(admin, "memory.sweep_expired", {"org": ORG, "namespace": ns}).json()
    assert d["scanned"] == 0
    del stale_id


@pytest.mark.slow
def test_expired_update_is_refused_then_revived(admin: httpx.Client, ns: str) -> None:
    w = _set(admin, ns, "note", "v1", ttl_seconds=1, on_expire="demote")
    _wait_hidden(admin, ns, "note")
    r = _post(
        admin, "memory.update_by_id", {"org": ORG, "memory_id": w["memory_id"], "value": "v2"}
    )
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "EXPIRED" and d["on_expire"] == "demote" and d["memory_key"] == "note"
    r = _post(
        admin,
        "memory.update_by_id",
        {"org": ORG, "memory_id": w["memory_id"], "value": "v2", "revive": True},
    )
    assert r.status_code == 200 and r.json()["updated"]
    got = _post(admin, "memory.get", _ident(ns, memory_key="note")).json()
    assert got["found"] and got["value"] == "v2" and got["expires_at"] is None
