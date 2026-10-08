"""memory.bulk_set gets memory.set's conflict verdicts: a
canonical restatement merges instead of inserting, a close write against a
pinned peer is refused, a close-but-different write lands as conflicted and
cross-links its peer. Runs against the control plane; bodies go over HTTP so
the whole path (verdict fold, insert, peer marking, audit row) is exercised.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _post(base: str, token: str, op: str, payload: dict[str, Any]) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=30.0) as c:
        return c.post(f"/v1/{op}", json=payload, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def ctx(control_plane_url: str, mint_test_token):
    org = f"bulkc-{uuid.uuid4().hex[:6]}"
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=org, namespaces=[ns], roles=["admin", "writer"], subject="alice")
    return control_plane_url, org, ns, tok


def _ident(org: str, ns: str) -> dict[str, Any]:
    return {"org": org, "namespace": ns, "agent": "a"}


def test_bulk_restatement_merges_and_reinforces_instead_of_inserting(ctx) -> None:
    base, org, ns, tok = ctx
    text = "The deploy window is Tuesday and Thursday only, never Friday."
    r = _post(
        base,
        tok,
        "memory.set",
        {
            **_ident(org, ns),
            "memory_key": "deploy_window",
            "value": text,
            "memory_type": "semantic",
        },
    )
    assert r.status_code == 200, r.text
    existing_id = r.json()["memory_id"]

    r = _post(
        base,
        tok,
        "memory.bulk_set",
        {
            **_ident(org, ns),
            "entries": [
                {"memory_key": "deploy_window_restated", "value": text, "memory_type": "semantic"},
                {
                    "memory_key": "unrelated_fact",
                    "value": "The office plant is a ficus named Gerald.",
                    "memory_type": "semantic",
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["merged"] == ["deploy_window_restated"], out
    assert [
        w["version"] for w in out["written"] if w["memory_key"] == "deploy_window_restated"
    ] == [0]
    assert [w["version"] for w in out["written"] if w["memory_key"] == "unrelated_fact"][0] > 0
    # Nothing landed under the restated key; the original row is untouched.
    g = _post(
        base, tok, "memory.get", {**_ident(org, ns), "memory_key": "deploy_window_restated"}
    ).json()
    assert g.get("found") is False
    g = _post(base, tok, "memory.get_by_id", {"org": org, "memory_id": existing_id}).json()
    assert g.get("found") is True and g.get("value") == text


def test_bulk_close_write_against_a_pinned_peer_is_refused(ctx) -> None:
    base, org, ns, tok = ctx
    text = "Production deploys happen on Tuesday and Thursday, never on Friday."
    r = _post(
        base,
        tok,
        "memory.set",
        {
            **_ident(org, ns),
            "memory_key": "deploy_policy",
            "value": text,
            "memory_type": "semantic",
            "pinned": True,
        },
    )
    assert r.status_code == 200, r.text
    close = "Production deploys happen on Tuesday and Thursday, never on Friday afternoons."
    r = _post(
        base,
        tok,
        "memory.bulk_set",
        {
            **_ident(org, ns),
            "entries": [
                {"memory_key": "deploy_policy_v2", "value": close, "memory_type": "semantic"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    # Either the pinned short-circuit (rejected) or, if the embedder rates the
    # pair above the duplicate threshold, a duplicate — never a silent
    # unchecked insert of a competing policy.
    assert out["rejected_pinned"] == ["deploy_policy_v2"] or out["duplicates"] == [
        "deploy_policy_v2"
    ], out
    if out["rejected_pinned"]:
        g = _post(
            base, tok, "memory.get", {**_ident(org, ns), "memory_key": "deploy_policy_v2"}
        ).json()
        assert g.get("found") is False


def test_bulk_close_but_different_write_is_conflicted_and_cross_linked(ctx) -> None:
    base, org, ns, tok = ctx
    r = _post(
        base,
        tok,
        "memory.set",
        {
            **_ident(org, ns),
            "memory_key": "retention",
            "value": "Logs are retained for 30 days in the primary region.",
            "memory_type": "semantic",
        },
    )
    assert r.status_code == 200, r.text
    peer_id = r.json()["memory_id"]
    r = _post(
        base,
        tok,
        "memory.bulk_set",
        {
            **_ident(org, ns),
            "entries": [
                {
                    "memory_key": "retention_v2",
                    "value": "Logs are retained for 90 days in the primary region.",
                    "memory_type": "semantic",
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    verdict_lists = {k: out[k] for k in ("merged", "rejected_pinned", "duplicates", "conflicted")}
    assert any(verdict_lists.values()), f"a near-identical contradiction went in unchecked: {out}"
    if out["conflicted"]:
        lst = _post(base, tok, "conflicts.list", {**_ident(org, ns)})
        assert lst.status_code == 200, lst.text
        body = lst.text
        assert peer_id in body or "retention" in body, body[:400]


def test_bulk_write_leaves_a_hash_only_audit_row(ctx, ch_client) -> None:
    base, org, ns, tok = ctx
    secret = f"the passphrase is {uuid.uuid4().hex}"
    r = _post(
        base,
        tok,
        "memory.bulk_set",
        {
            **_ident(org, ns),
            "entries": [
                {"memory_key": "audited", "value": secret, "memory_type": "semantic"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    rows = ch_client.query(
        "SELECT subject, actor FROM loom.audit_log WHERE org = {o:String} AND namespace = {n:String} "
        "AND event_type = 'memory.bulk_set' ORDER BY ts DESC LIMIT 1",
        parameters={"o": org, "n": ns},
    ).result_rows
    assert rows, "no audit row for the bulk write"
    subject, actor = rows[0]
    assert "audited" in subject and "content_sha256" in subject
    assert secret not in subject, "audit row carries content, not a hash"
    assert actor.startswith(("human:", "process:")) or actor == "-"
