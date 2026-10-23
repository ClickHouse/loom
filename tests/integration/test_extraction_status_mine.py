"""Caller-bound deferred extraction status and retry over HTTP and MCP. The server runs
with LOOM_BACKGROUND_WORKERS=off, as in CI: a worker would claim the batches these
tests leave pending or requeue."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

OP = "memory.extraction_status_mine"


def _mint(org: str, namespaces: list[str], subject: str) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=subject,
        org=org,
        namespaces=namespaces,
        roles=["admin", "writer"],
        ttl_seconds=900,
        is_service_account=True,
    )


def _v1(base: str, token: str, op: str, body: dict[str, Any]) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=60.0) as c:
        return c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})


def _ok(base: str, token: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    r = _v1(base, token, op, body)
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:400]}"
    return r.json()


def _loom_op(base: str, token: str, op: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The MCP `tools/call` result of `loom_op {op, arguments}`."""
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": {"op": op, "arguments": arguments}},
    }
    with httpx.Client(base_url=base, timeout=60.0) as c:
        r = c.post(
            "/mcp",
            json=rpc,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    assert r.status_code == 200, r.text[:400]
    return dict(r.json()["result"])


@pytest.fixture
def queued(control_plane_url: str) -> dict[str, str]:
    """One deferred batch in namespace A, queued by principal `s1`; namespace B exists."""
    org = unique_org("ext-mine")
    ns_a, ns_b = f"ext-a-{uuid.uuid4().hex[:8]}", f"ext-b-{uuid.uuid4().hex[:8]}"
    s1 = f"sa:ext-mine-{uuid.uuid4().hex[:6]}"
    for ns in (ns_a, ns_b):
        _ok(
            control_plane_url,
            _mint(org, [ns], s1),
            "namespace.register",
            {"org": org, "namespace": ns, "owner_email": "ext@example.com"},
        )
    out = _ok(
        control_plane_url,
        _mint(org, [ns_a], s1),
        "memory.set_from_messages",
        {
            "org": org,
            "namespace": ns_a,
            "agent": "chat-agent",
            "session_id": "sess-1",
            "messages": [{"role": "user", "content": "Quux keeps the badge number 4242."}],
            "defer": True,
        },
    )
    assert out["deferred"] is True and out["queue_id"], out
    return {"org": org, "ns_a": ns_a, "ns_b": ns_b, "s1": s1, "queue_id": out["queue_id"]}


def test_the_queuing_principal_reads_its_batch_over_mcp_and_http(
    control_plane_url: str, queued: dict[str, str]
) -> None:
    org, ns, qid = queued["org"], queued["ns_a"], queued["queue_id"]
    tok = _mint(org, [ns], queued["s1"])
    res = _loom_op(control_plane_url, tok, OP, {"namespace": ns, "queue_ids": [qid, "q-none"]})
    assert not res.get("isError"), res
    sc = res["structuredContent"]
    assert [b["queue_id"] for b in sc["batches"]] == [qid] and sc["unknown"] == ["q-none"], sc
    assert sc["batches"][0]["agent"] == "chat-agent" and sc["batches"][0]["message_count"] == 1, sc

    out = _ok(control_plane_url, tok, OP, {"org": org, "namespace": ns, "queue_ids": [qid]})
    assert [b["queue_id"] for b in out["batches"]] == [qid] and out["unknown"] == [], out


def test_another_principal_in_the_namespace_gets_the_id_back_unknown(
    control_plane_url: str, queued: dict[str, str]
) -> None:
    org, ns, qid = queued["org"], queued["ns_a"], queued["queue_id"]
    other = _mint(org, [ns], f"sa:ext-other-{uuid.uuid4().hex[:6]}")
    res = _loom_op(control_plane_url, other, OP, {"namespace": ns, "queue_ids": [qid]})
    assert not res.get("isError"), res
    assert res["structuredContent"]["batches"] == [], res
    assert res["structuredContent"]["unknown"] == [qid], res


def test_another_namespace_never_answers_the_batch(
    control_plane_url: str, queued: dict[str, str]
) -> None:
    org, ns_a, ns_b, qid = queued["org"], queued["ns_a"], queued["ns_b"], queued["queue_id"]
    # A token for B only: A is refused at the namespace gate, and B holds no such batch.
    b_only = _mint(org, [ns_b], f"sa:ext-b-{uuid.uuid4().hex[:6]}")
    r = _v1(control_plane_url, b_only, OP, {"org": org, "namespace": ns_a, "queue_ids": [qid]})
    assert r.status_code in (403, 404) and qid not in r.text, r.text[:400]
    res = _loom_op(control_plane_url, b_only, OP, {"namespace": ns_a, "queue_ids": [qid]})
    assert res.get("isError") and "batches" not in res.get("structuredContent", {}), res
    out = _ok(control_plane_url, b_only, OP, {"org": org, "namespace": ns_b, "queue_ids": [qid]})
    assert out["batches"] == [] and out["unknown"] == [qid], out

    # The queuing principal itself, asking in B: the batch is A's.
    both = _mint(org, [ns_a, ns_b], queued["s1"])
    out = _ok(control_plane_url, both, OP, {"org": org, "namespace": ns_b, "queue_ids": [qid]})
    assert out["batches"] == [] and out["unknown"] == [qid], out
    out = _ok(control_plane_url, both, OP, {"org": org, "namespace": ns_a, "queue_ids": [qid]})
    assert [b["queue_id"] for b in out["batches"]] == [qid], out  # the control

    # The same subject in another org reads its own org only.
    elsewhere = unique_org("ext-mine-other")
    tok = _mint(elsewhere, [ns_a], queued["s1"])
    r = _v1(control_plane_url, tok, OP, {"org": elsewhere, "namespace": ns_a, "queue_ids": [qid]})
    if r.status_code == 200:
        assert r.json()["batches"] == [] and r.json()["unknown"] == [qid], r.text
    else:
        assert r.status_code in (403, 404) and qid not in r.text, r.text[:400]


def test_the_namespace_wide_op_stays_hidden_and_names_the_bounded_one(
    control_plane_url: str, queued: dict[str, str]
) -> None:
    tok = _mint(queued["org"], [queued["ns_a"]], queued["s1"])
    res = _loom_op(
        control_plane_url,
        tok,
        "memory.extraction_status",
        {"namespace": queued["ns_a"], "queue_id": queued["queue_id"]},
    )
    sc = res["structuredContent"]
    assert res["isError"] and sc["code"] == "OP_NOT_ON_MCP", res
    assert OP in sc["instead"], sc


def _assert_pending(base: str, token: str, body: dict[str, Any]) -> None:
    wait_until(
        lambda: _ok(base, token, OP, body)["batches"][0]["status"] == "pending",
        description="accepted extraction retry to become pending",
    )


def _mark_failed(ch: Any, org: str, ns: str, queue_id: str) -> None:
    """A newer version of the batch, `failed` with its attempts spent, as the worker
    leaves one whose replays all failed."""
    ch.command(
        """
        INSERT INTO loom.memory_extraction_queue
        SELECT * REPLACE ('failed' AS status, 3 AS attempts,
                          'ExtractionError: the model answered nothing' AS last_error,
                          version + 1 AS version)
        FROM loom.memory_extraction_queue FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND queue_id = {qid:String}
        """,
        parameters={"org": org, "ns": ns, "qid": queue_id},
    )


def _stored_batch(ch: Any, org: str, ns: str, queue_id: str) -> dict[str, Any]:
    rows = list(
        ch.query(
            "SELECT * FROM loom.memory_extraction_queue FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND queue_id = {qid:String}",
            parameters={"org": org, "ns": ns, "qid": queue_id},
        ).named_results()
    )
    assert len(rows) == 1
    return dict(rows[0])


def test_only_the_queuing_principal_requeues_its_failed_batch(
    control_plane_url: str, queued: dict[str, str], ch_client: Any
) -> None:
    org, ns, qid = queued["org"], queued["ns_a"], queued["queue_id"]
    body = {"org": org, "namespace": ns, "queue_ids": [qid]}
    owner = _mint(org, [ns], queued["s1"])
    _assert_pending(control_plane_url, owner, body)
    _mark_failed(ch_client, org, ns, qid)
    failed = _stored_batch(ch_client, org, ns, qid)
    out = _ok(control_plane_url, owner, OP, body)
    assert out["failed"] == 1 and out["batches"][0]["status"] == "failed", out

    other = _mint(org, [ns], f"sa:ext-other-{uuid.uuid4().hex[:6]}")
    out = _ok(control_plane_url, other, "memory.extraction_retry_mine", body)
    assert (out["requeued"], out["not_failed"], out["unknown"]) == ([], [], [qid]), out
    assert _ok(control_plane_url, owner, OP, body)["batches"][0]["status"] == "failed"

    res = _loom_op(
        control_plane_url,
        owner,
        "memory.extraction_retry_mine",
        {"namespace": ns, "queue_ids": [qid]},
    )
    assert not res.get("isError") and res["structuredContent"]["requeued"] == [qid], res
    _assert_pending(control_plane_url, owner, body)
    # A second call finds it waiting and leaves it.
    out = _ok(control_plane_url, owner, "memory.extraction_retry_mine", body)
    assert (out["requeued"], out["not_failed"], out["unknown"]) == ([], [qid], []), out

    requeued = _stored_batch(ch_client, org, ns, qid)
    changed = {"status", "attempts", "lease_until", "updated_at"}
    assert {k: v for k, v in requeued.items() if k not in changed} == {
        k: v for k, v in failed.items() if k not in changed
    }
    from server.settings import settings

    assert requeued["attempts"] == settings.extraction_queue_max_attempts - 1


def test_a_held_batch_is_reported_over_http_and_mcp(
    control_plane_url: str, queued: dict[str, str], ch_client: Any
) -> None:
    org, ns, qid = queued["org"], queued["ns_a"], queued["queue_id"]
    ch_client.command(
        "INSERT INTO loom.memory_extraction_queue "
        "SELECT * REPLACE ('running' AS status, 'held: budget' AS last_error, "
        "now64(3) + INTERVAL 1 MINUTE AS lease_until, version + 1 AS version) "
        "FROM loom.memory_extraction_queue FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND queue_id = {qid:String}",
        parameters={"org": org, "ns": ns, "qid": qid},
    )
    owner = _mint(org, [ns], queued["s1"])
    arguments = {"org": org, "namespace": ns, "queue_ids": [qid]}
    http = _ok(control_plane_url, owner, OP, arguments)
    mcp = _loom_op(control_plane_url, owner, OP, arguments)
    assert not mcp.get("isError"), mcp
    for out in (http, mcp["structuredContent"]):
        assert out["batches"][0]["status"] == "held", out
        assert out["pending"] == 1 and out["failed"] == 0, out
    retry = _ok(control_plane_url, owner, "memory.extraction_retry_mine", arguments)
    assert retry["not_failed"] == [qid] and retry["requeued"] == [], retry
    assert _stored_batch(ch_client, org, ns, qid)["status"] == "running"
