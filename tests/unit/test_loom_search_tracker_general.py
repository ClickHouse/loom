"""Ordinary questions read authorized trackers without changing memory ranking."""

import asyncio
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.memory.app.loom_search import tracker_leg as leg
from server.memory.app.loom_search.answer_first import answer_first
from server.tracker.api.models import TrackerSimilarOutput


async def _run(
    query: str, *, org: str, top_k: int = 5, timeout_s: float | None = None
) -> dict[str, Any] | None:
    plan = await leg.prepare_tracker_search(query, org=org, top_k=top_k, timeout_s=timeout_s)
    return await leg.tracker_leg(query, plan=plan, top_k=top_k)


@pytest.fixture
def trackers(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr("server.code.domain.scope.current_code_scope", lambda: None)
    monkeypatch.setattr("server.identity.app.request_scope.request_scope", lambda: WHOLE_NAMESPACE)
    monkeypatch.setattr(
        "server.tracker.app.dupcheck_fanout.readable_tracker_namespaces",
        AsyncMock(return_value=["tracker-one", "tracker-two"]),
    )

    async def search(input: Any) -> TrackerSimilarOutput:
        assert input.org == "o"
        assert input.kinds == ["issue", "pr"]
        assert "vector" not in input.legs
        return TrackerSimilarOutput(
            items=[
                {
                    "kind": "pr",
                    "number": 7,
                    "title": "Retention decision",
                    "state": "closed",
                    "score": 0.5 if input.namespace == "tracker-one" else 0.8,
                    "url": "https://example.test/pull/7",
                }
            ]
        )

    call = AsyncMock(side_effect=search)
    monkeypatch.setattr("server.tracker.app.similar.similar_in_namespace", call)
    return call


async def test_ordinary_question_searches_readable_trackers_without_code_scope(
    trackers: AsyncMock,
) -> None:
    result = await _run("what did we decide about retention?", org="o", top_k=2)
    assert result is not None and result["mode"] == "search"
    assert trackers.await_count == 2
    assert [r["namespace"] for r in result["results"]] == ["tracker-two", "tracker-one"]
    assert all(r["url"] == "https://example.test/pull/7" for r in result["results"])
    assert "verdict" not in result
    lines = "\n".join(leg.tracker_text_lines(result))
    assert "Retention decision" in lines and "[tracker-two] #7" in lines
    assert "duplicate" not in lines.lower()


async def test_read_narrowed_search_reads_no_tracker(
    trackers: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "server.identity.app.request_scope.request_scope",
        lambda: ReadFilters(scope="own_user", user_id="u", session_id=None),
    )
    assert await _run("retention decision", org="o") is None
    trackers.assert_not_awaited()


async def test_empty_tracker_results_add_no_section(trackers: AsyncMock) -> None:
    trackers.side_effect = None
    trackers.return_value = TrackerSimilarOutput()
    assert await _run("retention decision", org="o") is None


async def test_timeout_covers_namespace_discovery(
    trackers: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = asyncio.Event()

    async def discover(*args: Any, **kwargs: Any) -> list[str]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return []

    monkeypatch.setattr("server.tracker.app.dupcheck_fanout.readable_tracker_namespaces", discover)
    assert await _run("retention decision", org="o", timeout_s=0.01) is None
    assert cancelled.is_set()
    trackers.assert_not_awaited()


def test_general_matches_preserve_the_existing_answer_first_sections() -> None:
    payload = {
        "memory": {
            "results": [
                {
                    "memory_key": "home",
                    "namespace": "home",
                    "memory_type": "fact",
                    "similarity": 0.9,
                },
                {
                    "memory_key": "other",
                    "namespace": "away",
                    "memory_type": "fact",
                    "similarity": 0.4,
                },
            ]
        },
        "knowledge": {
            "results": [{"namespace": "away", "content": "related document", "similarity": 0.5}]
        },
        "profile": {"results": []},
        "conversations": {"results": [{"content": "earlier discussion"}]},
    }
    baseline = answer_first(deepcopy(payload), "home")
    treatment = answer_first(
        {
            **deepcopy(payload),
            "tracker": {"mode": "search", "results": [{"title": "Retention decision"}]},
        },
        "home",
    )
    for section in ("memory", "knowledge", "profile", "conversations", "withheld"):
        assert treatment.get(section) == baseline.get(section)


@pytest.mark.parametrize("scoped_defect", [False, True])
async def test_tracker_consent_is_narrowed_in_parent_before_any_search_leg(
    trackers: AsyncMock, monkeypatch: pytest.MonkeyPatch, scoped_defect: bool
) -> None:
    from contextvars import ContextVar

    from server.memory.api.models_loom_search import SearchInput
    from server.memory.app.loom_search import handler

    capture = ContextVar("tracker_test_capture", default="full")
    calls: list[tuple[str, list[str]]] = []

    async def narrow(org: str, namespaces: list[str]) -> None:
        calls.append((org, namespaces))
        if "tracker-two" in namespaces:
            capture.set("off")

    async def sections(**kwargs: Any) -> dict[str, Any]:
        async def sibling() -> str:
            return capture.get()

        assert await asyncio.create_task(sibling()) == "off"
        return {k: {"results": []} for k in ("memory", "knowledge", "profile", "conversations")}

    monkeypatch.setattr(
        "server.auth.fanout.namespaces_to_search", AsyncMock(return_value=(["home"], []))
    )
    monkeypatch.setattr(handler, "repo_namespace_in_scope", AsyncMock(return_value=""))
    monkeypatch.setattr("server.observability.capture_consent.narrow_to", narrow)
    monkeypatch.setattr(handler, "search_sections", sections)
    query = "retention"
    if scoped_defect:
        from server.tracker.app.dupcheck import DuplicateCheckPlan

        query = "LOGICAL_ERROR in DB::MergeTree::merge: already reported?"
        monkeypatch.setattr(
            "server.code.domain.scope.current_code_scope", lambda: ("other-org", "code-example")
        )

        async def prepare(inp: Any) -> DuplicateCheckPlan:
            return DuplicateCheckPlan(
                inp, ("tracker-one", "tracker-two"), inp.namespace, False, False
            )

        monkeypatch.setattr("server.tracker.app.dupcheck.prepare_duplicate_check", prepare)
    try:
        await handler.handle_search(SearchInput(org="o", namespace="home", query=query))
        assert calls == (
            [("o", ["home"]), ("other-org", ["tracker-one", "tracker-two"])]
            if scoped_defect
            else [("o", ["home", "tracker-one", "tracker-two"])]
        )
    finally:
        capture.set("full")


async def test_prepared_targets_do_not_widen_after_discovery(
    trackers: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovered = ["tracker-one"]
    discover = AsyncMock(return_value=discovered)
    monkeypatch.setattr("server.tracker.app.dupcheck_fanout.readable_tracker_namespaces", discover)
    plan = await leg.prepare_tracker_search("retention", org="o")
    assert plan is not None
    discovered.append("tracker-without-consent")
    result = await leg.tracker_leg("retention", plan=plan)
    assert result is not None and result["namespaces"] == ["tracker-one"]
    discover.assert_awaited_once()
    assert [call.args[0].namespace for call in trackers.await_args_list] == ["tracker-one"]


async def test_preflight_consumes_the_same_execution_budget(trackers: AsyncMock) -> None:
    from dataclasses import replace

    plan = await leg.prepare_tracker_search("retention", org="o")
    assert plan is not None
    expired = replace(plan, deadline=asyncio.get_running_loop().time() - 1)
    assert await leg.tracker_leg("retention", plan=expired) is None
    trackers.assert_not_awaited()


@pytest.mark.parametrize("kind", ["full", "empty", "failed_legs", "no_block"])
@pytest.mark.parametrize("namespace", ["alice", "team", "other"])
@pytest.mark.parametrize("explain", [False, True])
def test_recorded_sections_are_neutral_with_general_tracker_matches(
    kind: str, namespace: str, explain: bool
) -> None:
    from tests.unit.test_loom_search_mcp_render import _payload

    payload = _payload(kind)
    baseline = answer_first(deepcopy(payload), namespace, explain=explain)
    payload["tracker"] = {
        "mode": "search",
        "results": [
            {
                "namespace": "tracker-one",
                "kind": "issue",
                "number": 7,
                "title": "Retention decision",
            }
        ],
    }
    treatment = answer_first(payload, namespace, explain=explain)
    for section in ("memory", "knowledge", "profile", "conversations", "withheld"):
        assert treatment.get(section) == baseline.get(section)
