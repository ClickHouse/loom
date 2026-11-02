"""namespace.audit and namespace.validate findings cite the live revision behind each
sampled key, against a live Loom + ClickHouse.

A double-encoded value fires the audit's `double_encoded_values` heuristic and a stale
attestation fires validate's `stale_verification` contract check; each finding's
evidence names the revision memory.get serves for the row that matched (memory_id and
content hash), not another identity's row of the same key, and neither report carries
the value.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("findings")


# The value size every citation in these tests stays under.
HASH_MAX = 1 << 16


@pytest.mark.asyncio
async def test_findings_cite_the_revision_memory_get_serves(
    control_plane_url, mint_test_token
) -> None:
    from loom import Agent

    ns = f"fc-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="findings")
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        blob = json.dumps(json.dumps({"service": "billing", "owner": "payments-team"}))
        await a.memory.set("billing/owner", json.loads(blob), memory_type="semantic")
        held = await a.memory.get("billing/owner")

        audit = await a.call("namespace.audit", {"sample_limit": 5})
        f = next(f for f in audit["findings"] if f["code"] == "double_encoded_values")
        assert f["count"] == 1 and f["kind"] == "usage_heuristic"
        assert f["source_op"] == "namespace.audit" and f["rule_version"] == 1
        assert f["population"] == 1 and f["samples_complete"] is True
        cited = [e for e in f["evidence"] if e["memory_key"] == "billing/owner"]
        assert len(cited) == 1
        assert cited[0]["memory_id"] == held["memory_id"]
        assert cited[0]["content_sha256"] == held["content_sha256"]
        assert "payments-team" not in json.dumps(audit)
        assert audit["coverage"]["complete"] is True

        w = await a.memory.set("deploy_window", "Tue and Thu only.", memory_type="semantic")
        await a.memory.verify("deploy_window")
        upd = await a.memory.update_by_id(w["memory_id"], "Fridays are fine now.")

    # A second agent holds the same key, unverified: only the stale row is cited.
    async with Agent(
        org=ORG, namespace=ns, agent="u", base_url=control_plane_url, api_key=tok
    ) as b:
        other = await b.memory.set("deploy_window", "Mondays.", memory_type="semantic")
        assert other["memory_id"] != w["memory_id"]
        rep = await b.call("namespace.validate", {"keys": ["deploy_window"]})
        stale = next(f for f in rep["findings"] if f["check"] == "stale_verification")
        assert stale["kind"] == "contract_violation" and stale["count"] == 1
        assert [(e["agent"], e["content_sha256"]) for e in stale["evidence"]] == [
            ("t", upd["content_sha256"])
        ]
        assert rep["coverage"]["rules_skipped"] == []


@pytest.mark.asyncio
async def test_the_citation_read_resolves_ids_and_identities(
    control_plane_url, mint_test_token
) -> None:
    """The one read every report cites through, on the live store: a row named by id, a
    row named by identity with no ids at all, and an identity that holds nothing."""
    from loom import Agent

    from server.core.ch import get_client
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
    from server.memory.store.reads import live_revisions

    ns = f"fc-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="findings")
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        w = await a.memory.set("deploy/window", "Tuesdays.", memory_type="semantic")
        held = await a.memory.get("deploy/window")
        # An object with a null field, written with a hash: the JSON column drops the
        # field, so memory.get's hash differs from the stored one; the citation serves
        # memory.get's.
        doc = await a.memory.set("billing/owner", {"a": None, "b": 1}, memory_type="semantic")
        doc_held = await a.memory.get("billing/owner")
    ch = get_client()
    await ch.connect()
    try:
        doc_cited = await live_revisions(
            ORG,
            ns,
            scope=WHOLE_NAMESPACE,
            memory_ids=[doc["memory_id"]],
            identities=[],
            hash_max_bytes=HASH_MAX,
            ch=ch,
        )
        by_id = await live_revisions(
            ORG,
            ns,
            scope=WHOLE_NAMESPACE,
            memory_ids=[w["memory_id"]],
            identities=[],
            hash_max_bytes=HASH_MAX,
            ch=ch,
        )
        by_identity = await live_revisions(
            ORG,
            ns,
            scope=WHOLE_NAMESPACE,
            memory_ids=[],
            identities=[("t", "-", "-", "deploy/window"), ("t", "-", "-", "absent")],
            hash_max_bytes=HASH_MAX,
            ch=ch,
        )
        # A read pinned to another user's scope cites nothing of this row.
        other_user = ReadFilters(scope="own_user", user_id="someone-else", session_id=None)
        hidden = await live_revisions(
            ORG,
            ns,
            scope=other_user,
            memory_ids=[w["memory_id"]],
            identities=[],
            hash_max_bytes=HASH_MAX,
            ch=ch,
        )
    finally:
        await ch.disconnect()
    assert hidden == []
    assert [r["content_sha256"] for r in doc_cited] == [doc_held["content_sha256"]]
    for rows in (by_id, by_identity):
        assert [(r["memory_id"], r["content_sha256"]) for r in rows] == [
            (w["memory_id"], held["content_sha256"])
        ]


@pytest.mark.asyncio
async def test_a_row_stored_without_a_hash_is_cited_with_the_hash_memory_get_serves(
    control_plane_url, mint_test_token
) -> None:
    """A row whose content_sha256 column holds the default '0' is cited with the hash
    memory.get computes from its value: a string, and an object (served from the JSON
    column) with a null field."""
    from loom import Agent

    from server.core.ch import get_client
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store.reads import live_revisions

    ns = f"fc-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="findings")
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        w = await a.memory.set("deploy/window", "Tuesdays.", memory_type="semantic")
        held = await a.memory.get("deploy/window")
        doc = await a.memory.set(
            "billing/owner", {"owner": "payments", "backup": None}, memory_type="semantic"
        )
    ch = get_client()
    await ch.connect()
    try:
        await ch.execute(
            "ALTER TABLE loom.memory UPDATE content_sha256 = '0' "
            "WHERE org = {org:String} AND namespace = {ns:String} SETTINGS mutations_sync = 2",
            {"org": ORG, "ns": ns},
        )
        cited = await live_revisions(
            ORG,
            ns,
            scope=WHOLE_NAMESPACE,
            memory_ids=[w["memory_id"], doc["memory_id"]],
            identities=[],
            hash_max_bytes=HASH_MAX,
            ch=ch,
        )
    finally:
        await ch.disconnect()
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        doc_held = await a.memory.get("billing/owner")
    by_key = {r["memory_key"]: r["content_sha256"] for r in cited}
    assert by_key == {
        "deploy/window": held["content_sha256"],
        "billing/owner": doc_held["content_sha256"],
    }


@pytest.mark.asyncio
async def test_a_high_sensitivity_row_is_cited_without_its_hash(
    control_plane_url, mint_test_token
) -> None:
    """The reports are unaudited reads, and a short secret's hash can be guessed: a high
    row is cited by id and version with no hash, as memory.list serves it."""
    from loom import Agent

    from server.core.ch import get_client
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store.reads import live_revisions

    ns = f"fc-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="findings")
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        w = await a.call(
            "memory.set",
            {
                "memory_key": "vault/code",
                "value": "The vault code is 4417.",
                "memory_type": "semantic",
                "sensitivity": "high",
            },
        )
    ch = get_client()
    await ch.connect()
    try:
        cited = await live_revisions(
            ORG,
            ns,
            scope=WHOLE_NAMESPACE,
            memory_ids=[w["memory_id"]],
            identities=[],
            hash_max_bytes=HASH_MAX,
            ch=ch,
        )
    finally:
        await ch.disconnect()
    assert [(r["memory_id"], r["content_sha256"]) for r in cited] == [(w["memory_id"], "")]


@pytest.mark.asyncio
async def test_hot_key_samples_are_not_complete_beside_a_cold_key(
    control_plane_url, mint_test_token
) -> None:
    """The single-scan hot-key read on the live store: a key read past the hot threshold is
    sampled, a key read once is not, so the listing is not every get."""
    from loom import Agent

    ns = f"fc-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="findings")
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("lease/worker", "run-1 owner=a", memory_type="semantic")
        await a.memory.set("team/fact", "Deploys happen on Tuesdays.", memory_type="semantic")
        for _ in range(21):
            await a.memory.get("lease/worker")
        await a.memory.get("team/fact")

        async def audited() -> Any:
            out = await a.call("namespace.audit", {"window_seconds": 3600})
            f = next(f for f in out["findings"] if f["code"] == "exact_key_store")
            return f if f["count"] >= 22 else None

        f = await eventually(audited, description="the gets reach the ledger")
        assert [s["memory_key"] for s in f["samples"]] == ["lease/worker"]
        assert f["samples_complete"] is False
