"""knowledge.inspect against a live Loom + ClickHouse.

A namespace holds a guide revised once (knowledge.upsert twice), a notes page written
twice with knowledge.add, a page on a web URI, a deleted document, and a document whose
chunks are under another embedding model; one chunk of the guide carries no vector.
No wire op writes those last two states, so their rows go in through ClickHouse. Three
decided rows cite the guide's sections, one pinned and one tagged source-changed, and a
proposed one cites it too. The report counts each, cites namespace.audit, and writes
nothing.
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

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("kinspect")


def _memory_rows(ch_client: Any) -> list[tuple[Any, ...]]:
    """Each row's latest version (FINAL), so a merge leaves it as it was: the fields a
    write would change, not `version` or `entities`, which the entity extractor rewrites."""
    return [
        tuple(r)
        for r in ch_client.query(
            "SELECT agent, user_id, session_id, memory_key, memory_id, content_sha256, status,"
            " pinned, tags, tombstoned_at, actor, source_class, reinforcement_count"
            " FROM loom.memory FINAL WHERE org = {org:String}"
            " ORDER BY agent, user_id, session_id, memory_key",
            parameters={"org": ORG},
        ).result_rows
    ]


@pytest.mark.asyncio
async def test_inspect_reports_formats_revisions_chunks_and_decisions(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    ns = f"ki-{uuid.uuid4().hex[:6]}"
    writer = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="writer")
    viewer = mint_test_token(org=ORG, namespaces=[ns], roles=["viewer"], subject="viewer")
    base = {"org": ORG, "namespace": ns}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:

        async def post(op: str, body: dict[str, Any], token: str = writer) -> dict[str, Any]:
            r = await c.post(
                f"/v1/{op}", json={**base, **body}, headers={"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 200, (op, r.text[:400])
            return r.json()

        guide = "docs/guide.md"
        await post("knowledge.upsert", {"source_uri": guide, "content": "# Intro\nOld text."})
        await post(
            "knowledge.upsert",
            {"source_uri": guide, "title": "Guide", "content": "# Intro\nWe ship weekly."},
        )
        await post("knowledge.add", {"source_uri": "docs/notes.md", "content": "First."})
        await post("knowledge.add", {"source_uri": "docs/notes.md", "content": "Second."})
        await post(
            "knowledge.add",
            {"source_uri": "https://wiki.example/pages/plan.html?v=2", "content": "Plan."},
        )
        await post("knowledge.add", {"source_uri": "docs/gone.md", "content": "Gone."})
        await post("knowledge.delete", {"source_uri": "docs/gone.md"})
        await post("knowledge.add", {"source_uri": "docs/old.md", "content": "Old model."})

        # docs/old.md's chunks retired and written again under a model this server does not
        # embed with; one more chunk of the guide's live version with no vector.
        ch_client.command(
            "INSERT INTO loom.chunk_supersessions (org, namespace, embedding_model, "
            "source_uri, doc_version, superseded_at) SELECT DISTINCT org, namespace, "
            "embedding_model, source_uri, doc_version, now() FROM loom.chunks "
            "WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = 'docs/old.md'",
            parameters={"org": ORG, "ns": ns},
        )
        for uri, model, idx, dim in (("docs/old.md", "retired-model", 0, 3072), (guide, "", 9, 0)):
            ch_client.command(
                "INSERT INTO loom.chunks (org, namespace, source_uri, chunk_idx, content, "
                "doc_version, embedding_model, embedding_dim, embedding) "
                "SELECT org, namespace, source_uri, {idx:UInt32}, 'extra chunk', doc_version, "
                "if({model:String} = '', embedding_model, {model:String}), {dim:UInt16}, "
                "embedding FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String} "
                "AND source_uri = {uri:String} ORDER BY doc_version DESC LIMIT 1",
                parameters={
                    "org": ORG,
                    "ns": ns,
                    "uri": uri,
                    "model": model,
                    "idx": idx,
                    "dim": dim,
                },
            )

        # A document as knowledge.ingest publishes it (the extractor service and the ingest
        # worker do not run here): revision 2 with its format, parser and ingestion, one
        # chunk with keys at its version, and a later attempt's chunks that never
        # published, retired as the job's failure retires them.
        sheet, job = "files/sheet.xlsx", str(uuid.uuid4())
        ch_client.command(
            "INSERT INTO loom.documents (org, namespace, source_uri, title, content, version, "
            "revision, format, blob_content_type, original_filename, parser, artifact_sha256, "
            "ingestion_id) VALUES ({org:String}, {ns:String}, {uri:String}, 'Sheet', 'cells', "
            "1001, 2, 'xlsx', 'application/vnd.ms-excel', 'sheet.xlsx', 'ref/1', "
            "repeat('a', 64), {job:String})",
            parameters={"org": ORG, "ns": ns, "uri": sheet, "job": job},
        )
        for version in (1001, 1002):
            ch_client.command(
                "INSERT INTO loom.chunks (org, namespace, source_uri, chunk_idx, content, "
                "doc_version, embedding_model, embedding_dim, embedding, keys) "
                "SELECT org, namespace, {uri:String}, 0, 'Sheet1 cells', {v:UInt64}, "
                "embedding_model, embedding_dim, embedding, ['Sheet1'] FROM loom.chunks "
                "WHERE org = {org:String} AND namespace = {ns:String} "
                "AND source_uri = 'docs/notes.md' LIMIT 1",
                parameters={"org": ORG, "ns": ns, "uri": sheet, "v": version},
            )
        ch_client.command(
            "INSERT INTO loom.chunk_supersessions (org, namespace, embedding_model, "
            "source_uri, doc_version, superseded_at) SELECT DISTINCT org, namespace, "
            "embedding_model, source_uri, doc_version, now() FROM loom.chunks "
            "WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String} "
            "AND doc_version = 1002",
            parameters={"org": ORG, "ns": ns, "uri": sheet},
        )

        cite = f"knowledge:{ns}/{guide}#Intro"
        keys = []
        for n, tags in enumerate(([], ["source-changed"], [])):
            out = await post(
                "decision.record",
                {
                    "agent": "loom-decisions",
                    "category": "release",
                    "scenario": f"cadence {n}",
                    "reasoning": "We ship weekly.",
                    "outcome": "weekly",
                    "tags": tags,
                    "source_resource_id": cite,
                },
            )
            keys.append(out["memory_key"])
        await post(
            "decision.record",
            {
                "agent": "loom-decisions",
                "category": "release",
                "scenario": "move to daily releases",
                "reasoning": "Weekly is slow.",
                "outcome": "daily",
                "stage": "proposed",
                "source_resource_id": cite,
            },
        )
        row = await post("memory.get", {"agent": "loom-decisions", "memory_key": keys[2]})
        # memory.pin writes the row it is given: the row's type, tags and citation ride.
        await post(
            "memory.pin",
            {
                "agent": "loom-decisions",
                "memory_key": keys[2],
                "value": row["value"],
                "memory_type": "decision",
                "tags": row["tags"],
                "source_resource_id": cite,
            },
        )

        before = _memory_rows(ch_client)
        out = await post("knowledge.inspect", {"sample_limit": 3}, viewer)

    assert out["skipped"] == [], out["skipped"]
    docs = out["documents"]
    # The sheet's unpublished attempt is a retired chunk version no revision numbers.
    assert (docs["live"], docs["deleted"], docs["retired_versions"]) == (5, 1, 4)
    # From the rows: the guide's, the notes' and the sheet's second revisions.
    assert (docs["earlier_revisions"], docs["unnumbered"]) == (3, 0)
    assert {f["name"]: (f["live"], f["deleted"]) for f in docs["by_format"]} == {
        ".md": (3, 1),
        ".html": (1, 0),
        "xlsx": (1, 0),  # the row's format, not its content type
    }
    assert {s["name"]: s["live"] for s in docs["by_source_scheme"]} == {"(none)": 4, "https": 1}

    chunks = out["chunks"]
    assert chunks["unembedded"] == 1 and chunks["live"] == chunks["searchable"] + 2
    assert {m["model"] for m in chunks["by_model"]} == {chunks["embedder_model"], "retired-model"}

    findings = {f["code"]: f for f in out["findings"]}
    assert findings["unembedded_chunks"]["samples"] == [guide]
    assert findings["chunks_under_other_model"]["samples"] == ["docs/old.md"]
    assert findings["mixed_live_versions"]["samples"] == ["docs/notes.md"]
    assert findings["documents_without_chunks"]["count"] == 0
    assert all(f["ingested"] == 0 for f in out["findings"])
    assert {f["kind"] for f in out["findings"]} == {"system_state", "usage_heuristic"}

    # Three of the five live documents, so the listing says it is not all of them.
    assert len(out["listed"]) == 3 and out["listed_complete"] is False
    # The namespace total counts the guide's decisions whether or not it is listed.
    decided = out["decisions"]["all_documents"]["decided"]
    proposed = out["decisions"]["all_documents"]["proposed"]
    counts = ("total", "confirmed", "unconfirmed", "source_changed", "outcome_known")
    assert tuple(decided[k] for k in counts) == (3, 1, 2, 1, 0)
    assert tuple(proposed[k] for k in counts) == (1, 0, 1, 0, 0)
    assert [m["code"] for m in out["memory_findings"]] == ["oversized_values", "episodic_blobs"]

    # With room for every document, each carries its own chunks and decisions.
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        r = await c.post(
            "/v1/knowledge.inspect",
            json={**base, "sample_limit": 10},
            headers={"Authorization": f"Bearer {viewer}"},
        )
    assert r.json()["listed_complete"] is True
    listed = {d["source_uri"]: d for d in r.json()["listed"]}
    assert set(listed) == {
        guide,
        sheet,
        "docs/notes.md",
        "docs/old.md",
        "https://wiki.example/pages/plan.html?v=2",
    }
    x = listed[sheet]
    assert (x["format"], x["revision"], x["ingestion_id"], x["retired_versions"]) == (
        "xlsx",
        2,
        job,
        1,
    )
    assert (x["chunks"]["live"], x["chunks"]["searchable"], x["chunks"]["versions"]) == (1, 1, 1)
    assert listed["docs/notes.md"]["ingestion_id"] == ""
    g = listed[guide]
    assert (g["title"], g["retired_versions"], g["chunks"]["versions"]) == ("Guide", 1, 1)
    assert g["chunks"]["unembedded"] == 1 and g["chunks"]["searchable"] == 1
    assert tuple(g["decisions"]["decided"][k] for k in counts) == (3, 1, 2, 1, 0)
    assert g["decisions"]["proposed"]["total"] == 1
    assert listed["docs/notes.md"]["chunks"]["versions"] == 2
    assert listed["docs/notes.md"]["decisions"]["decided"]["total"] == 0
    old = listed["docs/old.md"]
    assert (old["chunks"]["other_model"], old["chunks"]["searchable"]) == (1, 0)
    assert old["chunks"]["models"] == ["retired-model"] and old["retired_versions"] == 1
    assert listed["https://wiki.example/pages/plan.html?v=2"]["source_scheme"] == "https"

    assert _memory_rows(ch_client) == before


@pytest.mark.asyncio
async def test_a_narrowed_token_reads_documents_but_no_audit_and_a_foreign_one_is_refused(
    control_plane_url: str, mint_test_token: Any
) -> None:
    from server.auth.jwt import mint_token

    ns = f"ki-{uuid.uuid4().hex[:6]}"
    body = {"org": ORG, "namespace": ns}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        anonymous = await c.post("/v1/knowledge.inspect", json=body)
        if anonymous.status_code == 200:
            pytest.skip("the server does not enforce auth")
        pinned = mint_token(
            subject="sa:kinspect-pinned",
            org=ORG,
            namespaces=[ns],
            roles=["viewer"],
            ttl_seconds=600,
            is_service_account=True,
            read_scope="own_user",
            read_user_id="u1",
        )
        r = await c.post(
            "/v1/knowledge.inspect", json=body, headers={"Authorization": f"Bearer {pinned}"}
        )
        assert r.status_code == 200, r.text[:300]
        assert r.json()["memory_findings"] == []
        assert any(s.startswith("memory_findings: ") for s in r.json()["skipped"])
        foreign = mint_test_token(org=ORG, namespaces=[f"{ns}-other"], roles=["viewer"])
        r = await c.post(
            "/v1/knowledge.inspect", json=body, headers={"Authorization": f"Bearer {foreign}"}
        )
        assert r.status_code == 403, r.text[:300]
