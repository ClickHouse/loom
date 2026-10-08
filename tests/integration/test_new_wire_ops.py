"""Wire-shape smoke tests for memory.timeline, facts.traverse, procedure.*,
abstain, calibration.get, namespace.taxonomy, and memory.link."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("new-ops")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client
    from server.embedding import embedder as _embedder_mod

    _embedder_mod.reset()
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


async def _post(url: str, token: str, op: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{url}/v1/{op}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        r.raise_for_status()
        return r.json()


@pytest.mark.asyncio
async def test_timeline_returns_entries_in_temporal_order(control_plane_url, mint_test_token):
    ns = f"timeline-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    for key, text in [
        ("ev_first", "First event about widgets"),
        ("ev_second", "Second event about widgets"),
        ("ev_third", "Third event about widgets"),
    ]:
        await _post(
            control_plane_url,
            tok,
            "memory.set",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "t",
                "memory_key": key,
                "value": text,
                "memory_type": "semantic",
                "entities": ["widgets"],
            },
        )

    async def _all_three():
        o = await _post(
            control_plane_url,
            tok,
            "memory.timeline",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "t",
                "entity": "widgets",
                "top_k": 10,
                "order": "desc",
            },
        )
        ks = [e["memory_key"] for e in o["events"]]
        return o if {"ev_first", "ev_second", "ev_third"} <= set(ks) else None

    # Entity-filtered timeline reads lag the writes (async flush + entity
    # projection); the 15s default occasionally times out under CI load even
    # though it normally resolves in <1s. 45s absorbs the spike without
    # masking a real hang.
    out = await eventually(_all_three, max_wait=45.0, description="3 timeline events visible")
    keys = [e["memory_key"] for e in out["events"]]
    assert keys.index("ev_third") < keys.index("ev_first")
    assert out["total_matching"] >= 3


@pytest.mark.asyncio
async def test_timeline_refuses_unscoped_query(control_plane_url, mint_test_token):
    ns = f"timeline-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{control_plane_url}/v1/memory.timeline",
            headers={"Authorization": f"Bearer {tok}"},
            json={"org": ORG, "namespace": ns, "agent": "t", "top_k": 10},
        )
    assert r.status_code == 400
    body = r.json()
    assert body.get("detail", {}).get("code") == "TIMELINE_REQUIRES_FILTER"


@pytest.mark.asyncio
async def test_facts_traverse_returns_seed_and_neighbors(control_plane_url, mint_test_token):
    ns = f"traverse-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    for key, text, ents in [
        ("m1", "Alice works on Project X", ["alice", "project_x"]),
        ("m2", "Bob also works on Project X", ["bob", "project_x"]),
        ("m3", "Bob also handles Project Y", ["bob", "project_y"]),
    ]:
        await _post(
            control_plane_url,
            tok,
            "memory.set",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "t",
                "memory_key": key,
                "value": text,
                "memory_type": "semantic",
                "entities": ents,
            },
        )

    # A point write's graph projection lands beside the response, but the walk reads the
    # caller's own writes: the first walk after the sets already reaches hop 2.
    out = await _post(
        control_plane_url,
        tok,
        "facts.traverse",
        {
            "org": ORG,
            "namespace": ns,
            "seed_entity": "alice",
            "max_hops": 2,
            "top_k_per_hop": 20,
        },
    )
    by_hop: dict[int, set[str]] = {}
    for n in out["nodes"]:
        by_hop.setdefault(n["hop"], set()).add(n["entity"])
    assert out["seed"] == "alice"
    # The seed is `seed`, not a hop-0 row; neighbours start at hop 1.
    assert 0 not in by_hop
    # The write path canonicalises entities ("project_x" -> "project x", plus
    # the head noun), so match on the stem rather than the literal key.
    assert any("project" in e for e in by_hop.get(1, set()))
    assert "bob" in by_hop.get(2, set())
    assert out.get("truncated") is False


@pytest.mark.asyncio
async def test_procedure_add_then_match_finds_it(
    control_plane_url, mint_test_token, require_real_embedder
):
    ns = f"proc-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    add_out = await _post(
        control_plane_url,
        tok,
        "procedure.add",
        {
            "org": ORG,
            "namespace": ns,
            "agent": "t",
            "name": "rollback_deploy",
            "situation": "Deploy fails or production starts emitting errors",
            "steps": [
                "Revert to the previous git tag",
                "Restart all replicas",
                "Verify health endpoint returns 200",
            ],
            "tags": ["ops", "incident"],
        },
    )
    assert add_out["memory_key"].startswith("procedure_")

    async def _matched():
        mo = await _post(
            control_plane_url,
            tok,
            "procedure.match",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "*",
                "situation": "Production is throwing errors after a deploy",
                "top_k": 3,
            },
        )
        ms = mo["matches"]
        return ms if any(m["name"] == "rollback_deploy" for m in ms) else None

    matches = await eventually(_matched, description="procedure to surface in match")
    rollback = next(m for m in matches if m["name"] == "rollback_deploy")
    assert rollback["steps"] == [
        "Revert to the previous git tag",
        "Restart all replicas",
        "Verify health endpoint returns 200",
    ]


@pytest.mark.asyncio
async def test_procedure_list_enumerates_registered(control_plane_url, mint_test_token):
    ns = f"proc-list-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    for name in ("first_proc", "second_proc", "third_proc"):
        await _post(
            control_plane_url,
            tok,
            "procedure.add",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "t",
                "name": name,
                "situation": f"Situation for {name}",
                "steps": ["step a", "step b"],
            },
        )

    async def _listed():
        o = await _post(
            control_plane_url,
            tok,
            "procedure.list",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "t",
                "limit": 100,
            },
        )
        names = {p["name"] for p in o["procedures"]}
        return names if {"first_proc", "second_proc", "third_proc"} <= names else None

    names = await eventually(_listed, description="all 3 procedures listed")
    assert {"first_proc", "second_proc", "third_proc"} <= names


@pytest.mark.asyncio
async def test_search_abstain_on_empty_namespace(control_plane_url, mint_test_token):
    ns = f"empty-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    out = await _post(
        control_plane_url,
        tok,
        "memory.search",
        {
            "org": ORG,
            "namespace": ns,
            "agent": "t",
            "query": "what is the capital of mars",
            "top_k": 5,
        },
    )
    assert out.get("abstain") is True
    assert "noise floor" in out.get("abstain_reason", "")


@pytest.mark.asyncio
async def test_sdk_search_surfaces_abstain_envelope(control_plane_url):
    """MemoryClient.search() surfaces the abstain/retrieval_risk envelope,
    not just the (empty) hit list."""
    from loom import Agent

    ns = f"sdk-abstain-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="t", base_url=control_plane_url) as a:
        result = await a.memory.search("what is the capital of mars", top_k=5)
    assert list(result) == []
    assert result.abstain is True
    assert result.abstain_reason
    assert isinstance(result.retrieval_risk, str)


@pytest.mark.asyncio
async def test_calibration_get_returns_resolved_thresholds(control_plane_url, mint_test_token):
    ns = f"cal-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    out = await _post(
        control_plane_url,
        tok,
        "calibration.get",
        {
            "org": ORG,
            "namespace": ns,
        },
    )

    assert out["source"] in ("namespace", "cluster", "scalar")
    for field in (
        "noise_floor",
        "rerank_skip_above",
        "hyde_fallback_threshold",
        "query_rewrite_threshold",
        "abstain_threshold",
        "conflict_threshold",
        "duplicate_threshold",
    ):
        assert isinstance(out[field], (int, float)), f"{field} missing"
    assert out["embedder_model"] != ""


@pytest.mark.asyncio
async def test_calibration_get_sdk_round_trip(control_plane_url, mint_test_token):
    from loom import Agent

    ns = f"cal-sdk-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    async with Agent(
        org=ORG,
        namespace=ns,
        agent="t",
        base_url=control_plane_url,
        api_key=tok,
    ) as a:
        cal = await a.calibration.get()

    assert "source" in cal
    assert cal["source"] in ("namespace", "cluster", "scalar")
    assert "noise_floor" in cal
    assert "abstain_threshold" in cal


@pytest.mark.asyncio
async def test_taxonomy_aggregates_tags_entities_types(control_plane_url, mint_test_token):
    ns = f"tax-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    for key, value, tags, entities in [
        ("m1", "User has a dog named Bowie.", ["pet"], ["bowie", "dog"]),
        ("m2", "User likes Italian food.", ["preference", "food"], ["italian"]),
    ]:
        await _post(
            control_plane_url,
            tok,
            "memory.set",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "t",
                "memory_key": key,
                "value": value,
                "memory_type": "semantic",
                "tags": tags,
                "entities": entities,
            },
        )

    async def _tax():
        t = await _post(
            control_plane_url,
            tok,
            "namespace.taxonomy",
            {
                "org": ORG,
                "namespace": ns,
            },
        )
        tagv = {x["value"] for x in t["tags"]}
        return t if (t["memory_count"] >= 2 and {"pet", "preference", "food"} <= tagv) else None

    tax = await eventually(_tax, description="taxonomy to aggregate both writes")
    tag_values = {t["value"] for t in tax["tags"]}
    assert {"pet", "preference", "food"} <= tag_values
    type_values = {t["value"] for t in tax["memory_types"]}
    assert "semantic" in type_values


@pytest.mark.asyncio
async def test_memory_link_round_trip(control_plane_url, mint_test_token):
    from loom import Agent

    ns = f"link-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    async with Agent(
        org=ORG,
        namespace=ns,
        agent="t",
        base_url=control_plane_url,
        api_key=tok,
    ) as a:
        out = await a.memory.link(
            "api_design",
            "legal_constraint",
            relation="driven_by",
            label="GDPR PII rule",
        )

    assert out["linked"] is True
    assert out["from_key"] == "api_design"
    assert out["to_key"] == "legal_constraint"
    assert out["relation"] == "derived_from"  # canonical verb (driven_by folds onto it)
    assert out["conflict_opened"] is False


@pytest.mark.asyncio
async def test_memory_link_contradicts_opens_a_conflict_and_provenance_shows_why(
    control_plane_url, mint_test_token
):
    """An agent-asserted contradiction between two live memories reaches
    conflicts.list next to the ones loom detects itself, and the edge's
    justification is readable from memory.provenance."""
    from loom import Agent

    ns = f"link-conf-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    async with Agent(
        org=ORG,
        namespace=ns,
        agent="t",
        base_url=control_plane_url,
        api_key=tok,
    ) as a:
        await a.memory.set("port:http", {"v": "the HTTP port is 8123"}, memory_type="semantic")
        await a.memory.set("port:native", {"v": "the HTTP port is 9000"}, memory_type="semantic")
        out = await a.memory.link(
            "port:http",
            "port:native",
            relation="conflicts with",
            explanation="Both claim to be the HTTP port with different numbers.",
            evidence=["8123 in port:http", "9000 in port:native"],
            confidence=0.9,
        )
        assert out["relation"] == "contradicts" and out["conflict_opened"] is True

        async def _both_conflicted() -> bool:
            rows = (await a.conflicts.list(limit=50))["rows"]
            return {r["memory_key"] for r in rows} >= {"port:http", "port:native"}

        await eventually(
            _both_conflicted, max_wait=10.0, description="both memories flagged conflicted"
        )

        prov = await a.memory.provenance("port:http")
        links = [ln for ln in prov["links"] if ln["memory_key"] == "port:native"]
        assert links and links[0]["relation"] == "contradicts"
        assert links[0]["explanation"].startswith("Both claim") and links[0]["confidence"] == 0.9
        assert links[0]["evidence"] == ["8123 in port:http", "9000 in port:native"]


@pytest.mark.asyncio
async def test_memory_link_tombstone_path(control_plane_url, mint_test_token):
    """tombstone=True returns linked=False; the row is still written for audit."""
    from loom import Agent

    ns = f"link-tomb-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    async with Agent(
        org=ORG,
        namespace=ns,
        agent="t",
        base_url=control_plane_url,
        api_key=tok,
    ) as a:
        out = await a.memory.link(
            "a",
            "b",
            relation="related",
            tombstone=True,
        )

    assert out["linked"] is False
    assert out["from_key"] == "a"
    assert out["to_key"] == "b"


@pytest.mark.asyncio
async def test_procedure_record_run_lands_in_ledger_and_evolve_folds_it(
    control_plane_url, mint_test_token, ch_client
):
    """record_run is one INSERT into loom.procedure_runs (no row rewrite);
    evolve folds the unfolded rows into the graph and advances the watermark."""
    ns = f"proc-ledger-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])

    add_out = await _post(
        control_plane_url,
        tok,
        "procedure.add",
        {
            "org": ORG,
            "namespace": ns,
            "agent": "t",
            "name": "ledger_proc",
            "situation": "A procedure whose runs are recorded",
            "steps": ["first", "second", "third"],
        },
    )
    key = add_out["memory_key"]

    async def _visible():
        try:
            return await _post(
                control_plane_url,
                tok,
                "procedure.record_run",
                {
                    "org": ORG,
                    "namespace": ns,
                    "agent": "t",
                    "memory_key": key,
                    "path": ["n1", "n2"],
                    "ok": True,
                },
            )
        except Exception:  # noqa: BLE001 — eventually() polls; any failure means not visible yet
            return None

    first = await eventually(_visible, description="procedure row readable for record_run")
    assert first["run_stats"]["total"] >= 1
    for ok in (True, False):
        await _post(
            control_plane_url,
            tok,
            "procedure.record_run",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "t",
                "memory_key": key,
                "path": ["n1", "n2"],
                "ok": ok,
            },
        )

    rows = ch_client.query(
        "SELECT count() AS n, countIf(ok) AS ok FROM loom.procedure_runs "
        "WHERE namespace = {ns:String} AND memory_key = {k:String}",
        parameters={"ns": ns, "k": key},
    ).result_rows
    assert rows and rows[0][0] == 3 and rows[0][1] == 2

    ev = await _post(
        control_plane_url,
        tok,
        "procedure.evolve",
        {
            "org": ORG,
            "namespace": ns,
            "agent": "t",
            "memory_key": key,
            "min_runs": 3,
        },
    )
    assert ev["folded_runs"] == 3
    assert ev["run_stats"] == {"total": 3, "ok": 2}
    assert ev["dead_end_steps"] == ["third"]

    ev2 = await _post(
        control_plane_url,
        tok,
        "procedure.evolve",
        {
            "org": ORG,
            "namespace": ns,
            "agent": "t",
            "memory_key": key,
            "min_runs": 3,
        },
    )
    assert ev2["folded_runs"] == 0

    # Deleting the procedure takes its ledger with it.
    await _post(
        control_plane_url,
        tok,
        "memory.delete",
        {
            "org": ORG,
            "namespace": ns,
            "agent": "t",
            "memory_key": key,
        },
    )

    async def _gone():
        n = ch_client.query(
            "SELECT count() FROM loom.procedure_runs WHERE namespace = {ns:String} AND memory_key = {k:String}",
            parameters={"ns": ns, "k": key},
        ).result_rows[0][0]
        return True if n == 0 else None

    assert await eventually(_gone, description="procedure_runs rows deleted with the procedure")
