"""Model tools retain the server's persistence and reliance evidence."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from loom import Agent
from loom.errors import LoomError

from server.memory.api.models_crud import MemorySetOutput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit


def _agent(reply: dict[str, Any]) -> tuple[Agent, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    agent = Agent(org="acme", namespace="support", agent="bot", api_key="k", base_url="http://x")
    agent.transport._client = httpx.AsyncClient(
        base_url="http://x", transport=httpx.MockTransport(handler)
    )
    return agent, calls


@pytest.mark.parametrize(
    ("receipt", "stored"),
    [
        ({"memory_id": "new", "content_sha256": "abc", "status": "active"}, True),
        (
            {
                "memory_id": "canonical-id",
                "content_sha256": "abc",
                "noop": True,
                "merged_into_key": "canonical",
                "conflict_verdict": "merge",
            },
            False,
        ),
        ({"memory_id": "dup", "status": "duplicate", "duplicate_of": "canonical-id"}, True),
        ({"dry_run": True, "noop": True, "conflict_verdict": "clear"}, False),
        (
            {
                "memory_id": "conflict",
                "conflict_verdict": "conflict",
                "peers": [{"memory_key": "other", "excerpt": "Conflicting claim"}],
                "next": "memory.update",
            },
            True,
        ),
    ],
)
async def test_remember_preserves_the_wire_receipt(receipt: dict[str, Any], stored: bool) -> None:
    reply = MemorySetOutput(
        version=0 if receipt.get("dry_run") else 3,
        memory_key="claim",
        namespace="support",
        **receipt,
    ).model_dump()
    agent, calls = _agent(reply)
    try:
        out = json.loads(
            await agent.tools().execute("loom_remember", {"memory_key": "claim", "value": "A fact"})
        )
        assert out == {**reply, "stored": stored, "verdict": reply["conflict_verdict"]}
        assert len(calls) == 1 and calls[0]["namespace"] == "support"
    finally:
        await agent.close()


@pytest.mark.parametrize("state", ["fresh", "changed", "unresolved", "missing"])
async def test_recall_keeps_code_state_and_provenance(state: str) -> None:
    hit = _MemorySearchHit(
        memory_key="claim",
        memory_type="semantic",
        content_excerpt="A code claim",
        similarity=0.9,
        distance=0.1,
        code_state=state,
        status="conflicted",
        source_class="external",
        source_resource_id="src/reader.py",
        verified=False,
        valid_at="2025-01-01T00:00:00Z",
        version=3,
        namespace="support",
    )
    reply = MemorySearchOutput(results=[hit], abstain=False, embedding_model="test").model_dump()
    agent, _ = _agent(reply)
    try:
        out = json.loads(await agent.tools().execute("loom_recall", {"query": "reader"}))
        assert out["abstain"] is False
        for field in (
            "code_state",
            "status",
            "source_class",
            "source_resource_id",
            "verified",
            "valid_at",
            "version",
            "namespace",
        ):
            assert out["memories"][0][field] == getattr(hit, field)
    finally:
        await agent.close()


async def test_recall_preserves_abstention_even_when_a_weak_hit_is_served() -> None:
    hit = _MemorySearchHit(
        memory_key="weak",
        memory_type="semantic",
        content_excerpt="Weak claim",
        similarity=0.2,
        distance=0.8,
        below_threshold=True,
    )
    reply = MemorySearchOutput(
        results=[hit],
        abstain=True,
        abstain_code="below_threshold",
        abstain_reason="Weak evidence",
        pending_extraction=2,
        embedding_model="test",
    ).model_dump()
    agent, _ = _agent(reply)
    try:
        out = json.loads(await agent.tools().execute("loom_recall", {"query": "claim"}))
        assert (out["abstain"], out["abstain_code"], out["pending_extraction"]) == (
            True,
            "below_threshold",
            2,
        )
        assert out["memories"][0]["below_threshold"] is True
    finally:
        await agent.close()


@pytest.mark.parametrize("with_governed", [True, False])
async def test_recall_distinguishes_governed_authority_from_ordinary_risk(
    with_governed: bool,
) -> None:
    ordinary = _MemorySearchHit(
        memory_key="ordinary",
        memory_type="semantic",
        content_excerpt="Weak ordinary evidence",
        distance=0.8,
        similarity=0.2,
        below_threshold=True,
    )
    definition = _MemorySearchHit(
        memory_key="definition/retry",
        memory_type="semantic",
        content_excerpt="Retries require an idempotency key.",
        distance=0.9,
        similarity=0.1,
        governed=True,
    )
    reply = MemorySearchOutput(
        results=[definition, ordinary] if with_governed else [ordinary],
        embedding_model="fixture",
        term_leg="complete",
        abstain=not with_governed,
        retrieval_risk="abstain",
        retrieval_confidence=0.0,
    ).model_dump()
    agent, _ = _agent(reply)
    try:
        out = json.loads(await agent.tools().execute("loom_recall", {"query": "retry policy"}))
        assert (
            out["term_leg"],
            out["abstain"],
            out["retrieval_risk"],
            out["retrieval_confidence"],
        ) == ("complete", not with_governed, "abstain", 0.0)
        if with_governed:
            assert out["memories"][0]["governed"] is True
            assert out["memories"][0]["similarity"] == 0.1
        assert out["memories"][-1].get("governed", False) is False
        assert out["memories"][-1]["below_threshold"] is True
    finally:
        await agent.close()


async def test_recall_budget_never_slices_json_or_detaches_trust_metadata() -> None:
    reply = {
        "abstain": False,
        "results": [
            {
                "memory_key": "large",
                "content_excerpt": "x" * 2000,
                "similarity": 0.9,
                "code_state": "changed",
                "verified": False,
            }
        ],
    }
    agent, _ = _agent(reply)
    try:
        text = await agent.tools(max_result_chars=200).execute("loom_recall", {"query": "claim"})
        out = json.loads(text)
        assert len(text) <= 200
        assert out["memories"] == [] and out["omitted"] == 1
        assert out["abstain"] is False
    finally:
        await agent.close()


async def test_oversized_receipt_reports_missing_metadata_without_a_storage_claim() -> None:
    reply = MemorySetOutput(version=3, noop=True, merged_into_key="x" * 2000).model_dump()
    agent, _ = _agent(reply)
    try:
        text = await agent.tools(max_result_chars=100).execute(
            "loom_remember", {"memory_key": "claim", "value": "A fact"}
        )
        out = json.loads(text)
        assert len(text) <= 100
        assert out == {"insufficient": True, "metadata_omitted": True, "reason": "max_result_chars"}
    finally:
        await agent.close()


@pytest.mark.parametrize("files", [["src/reader.py"], []])
async def test_memory_set_sends_explicit_file_references_including_clear(files: list[str]) -> None:
    agent, calls = _agent(MemorySetOutput(version=3).model_dump())
    try:
        await agent.memory.set(
            "claim", "`Reader.read` requires a lease", files=files, tags=["repo:0123456789"]
        )
        assert calls[0]["files"] == files
        assert calls[0]["tags"] == ["repo:0123456789"]
        assert calls[0]["namespace"] == "support"
    finally:
        await agent.close()


@pytest.mark.parametrize("name", ["loom_remember", "loom_recall"])
async def test_unreportable_memory_budget_refuses_before_sending(name: str) -> None:
    agent, calls = _agent({})
    try:
        with pytest.raises(LoomError, match="too small to report memory result omissions"):
            await agent.tools(max_result_chars=20).execute(
                name, {"memory_key": "claim", "value": "A fact", "query": "claim"}
            )
        assert calls == []
    finally:
        await agent.close()
