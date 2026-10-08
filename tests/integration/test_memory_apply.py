"""memory.apply end to end: filter target with dry run then
enforce, a locked row as a refused row, label add / remove, demote,
search target, truncation."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.integration._resources import unique_org

ORG = unique_org("apply")


def _post(client: httpx.Client, op: str, body: dict) -> httpx.Response:
    return client.post(f"/v1/{op}", json=body)


@pytest.fixture
def ns() -> str:
    return f"ap-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def writer(control_plane_url: str, mint_test_token):
    tok = mint_test_token(org=ORG, namespaces=["*"], roles=["writer"], subject="w1")
    with httpx.Client(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {tok}"}, timeout=30.0
    ) as c:
        yield c


def _ident(ns: str, **extra) -> dict:
    return {"org": ORG, "namespace": ns, "agent": "t", **extra}


def _set(c: httpx.Client, ns: str, key: str, value: str, tags: list[str]) -> dict:
    r = _post(
        c,
        "memory.set",
        _ident(ns, memory_key=key, value=value, memory_type="semantic", tags=tags, classify=False),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_filter_dry_run_then_enforce_with_a_locked_row(writer: httpx.Client, ns: str) -> None:
    for i in range(4):
        _set(writer, ns, f"mig_{i}", f"q3 migration note {i}", ["q3-migration", "infra"])
    _set(writer, ns, "keep", "unrelated", ["other"])
    r = _post(
        writer, "memory.lock", _ident(ns, memory_key="mig_3", mode="no_delete", reason="audit")
    )
    assert r.status_code == 200 and r.json()["locked"]

    body = _ident(
        ns,
        target={"filter": {"tags_any": ["q3-migration"]}},
        action="delete",
        reason="migration closed",
        limit=10,
    )
    r = _post(writer, "memory.apply", body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dry_run"] and d["target"] == "filter" and d["matched"] == 4
    assert d["would_apply"] == 3 and d["refused"] == 1 and d["applied"] == 0 and not d["truncated"]
    refused = [x for x in d["rows"] if x["status"] == "refused"][0]
    assert refused["memory_key"] == "mig_3" and "locked no_delete" in refused["detail"]
    # nothing happened
    assert _post(writer, "memory.get", _ident(ns, memory_key="mig_0")).json()["found"]

    # limit smaller than the match → truncated
    r = _post(writer, "memory.apply", {**body, "limit": 2})
    assert r.status_code == 200 and r.json()["truncated"] is True and r.json()["matched"] == 2

    r = _post(writer, "memory.apply", {**body, "dry_run": False})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"] == 3 and d["refused"] == 1
    for i in range(3):
        assert not _post(writer, "memory.get", _ident(ns, memory_key=f"mig_{i}")).json()["found"]
    assert _post(writer, "memory.get", _ident(ns, memory_key="mig_3")).json()["found"]
    assert _post(writer, "memory.get", _ident(ns, memory_key="keep")).json()["found"]


def test_label_demote_expire_and_search_targets(writer: httpx.Client, ns: str) -> None:
    _set(writer, ns, "rb_1", "runbook: rotate the CH password quarterly", ["runbook"])
    _set(writer, ns, "rb_2", "runbook: failover steps for the staging service", ["runbook"])
    _set(writer, ns, "note", "a note about lunch", ["misc"])

    r = _post(
        writer,
        "memory.apply",
        _ident(
            ns,
            target={"keys": ["rb_1", "rb_2", "nope"]},
            action="label",
            reason="tag runbooks",
            dry_run=False,
            label={"tags": ["ops", "runbook"], "mode": "add"},
        ),
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["matched"] == 2 and d["applied"] == 2
    got = _post(writer, "memory.get", _ident(ns, memory_key="rb_1")).json()
    assert sorted(got["tags"]) == ["ops", "runbook"]

    r = _post(
        writer,
        "memory.apply",
        _ident(
            ns,
            target={"keys": ["rb_1"]},
            action="label",
            reason="untag",
            dry_run=False,
            label={"tags": ["ops"], "mode": "remove"},
        ),
    )
    assert r.json()["applied"] == 1
    assert _post(writer, "memory.get", _ident(ns, memory_key="rb_1")).json()["tags"] == ["runbook"]

    r = _post(
        writer,
        "memory.apply",
        _ident(
            ns,
            target={"filter": {"tags_all": ["runbook"]}},
            action="demote",
            reason="aging",
            limit=10,
            dry_run=False,
            demote={"confidence_factor": 0.5},
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 2
    got = _post(writer, "memory.get", _ident(ns, memory_key="rb_2")).json()
    assert abs(got["confidence"] - 0.5) < 1e-3

    r = _post(
        writer,
        "memory.apply",
        _ident(
            ns,
            target={"keys": ["rb_2"]},
            action="expire",
            reason="review in a day",
            dry_run=False,
            expire={"ttl_seconds": 86400, "on_expire": "demote"},
        ),
    )
    assert r.json()["applied"] == 1
    got = _post(writer, "memory.get", _ident(ns, memory_key="rb_2")).json()
    assert (
        got["expires_at"]
        and got["on_expire"] == "demote"
        and got["expire_reason"] == "review in a day"
    )

    r = _post(
        writer,
        "memory.apply",
        _ident(
            ns,
            target={"search": {"query": "runbook failover password"}},
            action="lock",
            reason="freeze runbooks",
            limit=5,
            lock={"mode": "read_only"},
        ),
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["target"] == "search" and d["matched"] >= 1
    assert all(x["status"] == "would_apply" for x in d["rows"])
    keys = {x["memory_key"] for x in d["rows"]}
    assert "rb_1" in keys or "rb_2" in keys
