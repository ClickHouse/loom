"""A reflection cites its evidence or it is not written.

`RunReflection.evidence` names the memory_keys a lesson rests on; it is
required when `what_failed` or `lessons` is given. The reflection memory
records the keys, carries an expiry (90 days by default) so an unvalidated
lesson fades, and gets one `derived_from` edge per evidence key.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from server.tasks.api.models_run import RunCompleteInput, RunReflection


def test_lessons_without_evidence_are_refused() -> None:
    with pytest.raises(ValidationError, match="evidence is required"):
        RunReflection(lessons=["always retry on 429"])
    with pytest.raises(ValidationError, match="evidence is required"):
        RunReflection(what_failed=["the cache key collided"], evidence=["  "])


def test_what_worked_and_assumptions_alone_need_no_evidence() -> None:
    r = RunReflection(what_worked=["batching the inserts"], assumptions=["one writer"])
    assert r.evidence == [] and r.expires_in_days == 90


def test_evidence_is_cleaned_and_bounded() -> None:
    r = RunReflection(lessons=["x"], evidence=[" k1 ", "", "k2"])
    assert r.evidence == ["k1", "k2"]
    with pytest.raises(ValidationError):
        RunReflection(lessons=["x"], evidence=[f"k{i}" for i in range(51)])
    with pytest.raises(ValidationError):
        RunReflection(what_worked=["x"], expires_in_days=-1)


@pytest.fixture
def write_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from server.memory.app import ops as _memory_ops
    from server.memory.app import write as write_mod

    spy: dict[str, Any] = {"set": [], "links": []}

    async def fake_set(inp: Any) -> Any:
        spy["set"].append(inp)
        return None

    async def fake_link(inp: Any) -> Any:
        spy["links"].append(inp)
        return None

    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    monkeypatch.setattr(_memory_ops, "handle_memory_link", fake_link)
    return spy


def _complete(reflection: RunReflection) -> RunCompleteInput:
    return RunCompleteInput(
        org="o",
        namespace="ns",
        run_id="run-7",
        status="completed",
        reflection=reflection,
    )


@pytest.mark.asyncio
async def test_reflection_memory_records_evidence_expiry_and_edges(
    write_spy: dict[str, Any],
) -> None:
    from server.tasks.app.run import _write_reflection

    existing = {"agent": "bot", "user_id": "-", "session_id": "s1", "goal": "ship it"}
    key = await _write_reflection(
        existing,
        _complete(
            RunReflection(
                lessons=["retry on 429"],
                what_failed=["hit the rate limit"],
                evidence=["api_429_log", "retry_policy", "api_429_log"],
            )
        ),
    )
    assert key == "reflection:run-7"
    set_input = write_spy["set"][0]
    assert set_input.memory_type == "reflection"
    assert set_input.value["evidence"] == ["api_429_log", "retry_policy"]  # deduped, ordered
    assert set_input.ttl_seconds == 90 * 86400
    # one derived_from edge per evidence key, from the reflection to what it rests on
    edges = [(e.from_key, e.to_key, e.relation) for e in write_spy["links"]]
    assert edges == [
        ("reflection:run-7", "api_429_log", "derived_from"),
        ("reflection:run-7", "retry_policy", "derived_from"),
    ]
    assert all(e.run_id == "run-7" for e in write_spy["links"])


@pytest.mark.asyncio
async def test_expiry_zero_keeps_the_lesson_and_no_evidence_means_no_edges(
    write_spy: dict[str, Any],
) -> None:
    from server.tasks.app.run import _write_reflection

    await _write_reflection(
        {"agent": "bot"},
        _complete(RunReflection(what_worked=["batching"], expires_in_days=0)),
    )
    assert write_spy["set"][0].ttl_seconds is None
    assert write_spy["set"][0].value["evidence"] == []
    assert write_spy["links"] == []


@pytest.mark.asyncio
async def test_edge_failure_does_not_fail_the_close(
    write_spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import ops as _memory_ops
    from server.tasks.app.run import _write_reflection

    async def boom(inp: Any) -> Any:
        raise RuntimeError("edges down")

    monkeypatch.setattr(_memory_ops, "handle_memory_link", boom)
    key = await _write_reflection(
        {"agent": "bot"},
        _complete(RunReflection(lessons=["x"], evidence=["k1"])),
    )
    assert key == "reflection:run-7" and len(write_spy["set"]) == 1


def test_nested_value_errors_reach_the_client_as_json() -> None:
    """The reflection rule raises ValueError in a model_validator; pydantic keeps
    the exception object in the error context; handing that straight to the
    JSON encoder would turn a 422 into a 500."""
    import json

    from server.transport._common import validation_errors

    with pytest.raises(ValidationError) as ei:
        RunCompleteInput(
            org="o", namespace="ns", run_id="r", status="completed", reflection={"lessons": ["x"]}
        )
    errs = validation_errors(ei.value)
    assert json.dumps(errs)  # serialisable
    assert errs[0]["type"] == "value_error" and "evidence is required" in errs[0]["msg"]
    assert errs[0]["loc"][0] == "reflection"
