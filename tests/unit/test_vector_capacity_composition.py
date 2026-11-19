"""Capacity refusals survive composed reads and deadline cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app import search_legs
from server.core.errors import VectorInputTooLargeError
from server.memory.app.loom_search import fanout
from server.tracker.api.models import TrackerSimilarInput, TrackerSimilarOutput
from server.tracker.app import similar


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [True, False], ids=["capacity-refusal", "ordinary-deadline"])
async def test_code_model_deadline_keeps_a_capacity_failure_during_cancellation(
    monkeypatch: pytest.MonkeyPatch, capacity: bool
) -> None:
    monkeypatch.setattr(search_legs.settings, "code_search_llm_leg_grace_ms", 1)
    started, stopped = asyncio.Event(), asyncio.Event()
    failure = (
        VectorInputTooLargeError("deadline capacity refusal")
        if capacity
        else RuntimeError("ordinary optional model failure")
    )

    async def lexical() -> list[str]:
        await started.wait()
        return ["lexical-row"]

    async def model() -> list[str]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
            raise failure

    result = SimpleNamespace(legs_timed_out=[], epistemic="", budget=None)
    work: dict[str, Any] = {}
    call = search_legs.run_legs(
        {"lexical": (lexical(), []), "hyde": (model(), [])},
        result=result,
        work=work,
        deadline_ms=20,
    )
    if capacity:
        with pytest.raises(VectorInputTooLargeError) as caught:
            await asyncio.wait_for(call, timeout=1)
        assert caught.value is failure
        assert result.legs_timed_out == []
    else:
        assert await asyncio.wait_for(call, timeout=1) == [["lexical-row"], []]
        assert result.legs_timed_out == ["hyde"]
        assert result.epistemic == "partial"
        assert work["legs_timed_out"] == 1
    assert started.is_set() and stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["returned-memory", "knowledge-renewal"])
@pytest.mark.parametrize("capacity", [True, False], ids=["capacity-refusal", "ordinary-fallback"])
async def test_section_assembly_preserves_refusal_values_and_renewal_failures(
    monkeypatch: pytest.MonkeyPatch, site: str, capacity: bool
) -> None:
    failure = (
        VectorInputTooLargeError("section capacity refusal")
        if capacity
        else RuntimeError("ordinary unavailable section")
    )
    renewed: list[bool] = []

    async def namespaces(_org: str, ns: str, _scope: str) -> tuple[list[str], list[str]]:
        return [ns], []

    async def prepare(*_args: Any) -> Any:
        return None, SimpleNamespace(marked=False)

    async def memory(*_args: Any, **_kwargs: Any) -> Any:
        # A returned Exception is a possible gather result and must be inspected.
        return failure if site == "returned-memory" else RuntimeError("ordinary memory failure")

    async def knowledge(*_args: Any) -> Any:
        if site == "knowledge-renewal":
            return object()
        raise RuntimeError("ordinary knowledge failure")

    async def publish(*_args: Any) -> Any:
        renewed.append(True)
        raise failure

    async def empty(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def conversation(*_args: Any) -> Any:
        raise RuntimeError("ordinary conversation failure")

    monkeypatch.setattr("server.auth.fanout.namespaces_to_search", namespaces)
    monkeypatch.setattr(fanout, "request_scope", lambda: None)
    monkeypatch.setattr(fanout, "_prepare_knowledge", prepare)
    monkeypatch.setattr(fanout, "memory_with_repo_decisions", memory)
    monkeypatch.setattr(fanout, "_run_knowledge", knowledge)
    monkeypatch.setattr(fanout, "_published_knowledge", publish)
    monkeypatch.setattr(fanout, "search_profile", empty)
    monkeypatch.setattr(fanout, "_conversation_leg", conversation)
    monkeypatch.setattr(fanout, "tracker_leg", empty)
    monkeypatch.setattr(fanout, "answer_first", lambda payload, *_a, **_k: payload)
    call = fanout.search_sections(
        org="org",
        namespace="home",
        agent="a",
        query="question",
        top_k=3,
        tags=[],
        namespace_scope="this",
        recipe="",
        bodies="excerpt",
        page="",
        explain=False,
        repo_namespace="",
        tracker_plan=None,
    )
    if capacity:
        with pytest.raises(VectorInputTooLargeError) as caught:
            await asyncio.wait_for(call, timeout=1)
        assert caught.value is failure
    else:
        payload = await asyncio.wait_for(call, timeout=1)
        assert payload["memory"] == {"error": "RuntimeError", "results": []}
        assert payload["knowledge"] == {"error": "RuntimeError", "results": []}
        assert payload["profile"] == {"results": []}
        assert payload["conversations"] == {"error": "RuntimeError", "results": []}
    assert renewed == ([True] if site == "knowledge-renewal" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [True, False], ids=["capacity-refusal", "ordinary-fallback"])
async def test_tracker_similar_secondary_namespace_keeps_capacity_refusal(
    monkeypatch: pytest.MonkeyPatch, capacity: bool
) -> None:
    failure = (
        VectorInputTooLargeError("secondary tracker capacity refusal")
        if capacity
        else RuntimeError("ordinary secondary tracker failure")
    )
    addressed: list[str] = []

    async def linked(_org: str, _namespace: str) -> list[str]:
        return ["primary", "secondary"]

    async def one(inp: TrackerSimilarInput) -> TrackerSimilarOutput:
        addressed.append(inp.namespace)
        if inp.namespace == "secondary":
            raise failure
        return TrackerSimilarOutput(items=[], query={"known": True}, legs={"vector": "empty"})

    monkeypatch.setattr(similar.common, "linked_trackers", linked)
    monkeypatch.setattr(similar, "_similar_one", one)
    call = similar.handle_tracker_similar(
        TrackerSimilarInput(org="org", namespace="repository", text="question", top_k=3)
    )
    if capacity:
        with pytest.raises(VectorInputTooLargeError) as caught:
            await asyncio.wait_for(call, timeout=1)
        assert caught.value is failure
    else:
        out = await asyncio.wait_for(call, timeout=1)
        assert out.namespaces == ["primary"]
        assert out.query == {"known": True}
        assert out.legs == {"vector": "empty"}
        assert not out.tracker_unavailable
    assert addressed == ["primary", "secondary"]
