"""The code tests' shared timing helpers (`tests/unit/code_fixtures.py`)."""

from __future__ import annotations

import gc
import json
import sys

import pytest

from tests.unit import code_fixtures
from tests.unit.code_fixtures import untraced


def test_untraced_leaves_the_collector_as_it_found_it() -> None:
    with untraced():
        assert not gc.isenabled()
    assert gc.isenabled()
    gc.disable()
    try:
        with untraced():
            pass
        assert not gc.isenabled()
    finally:
        gc.enable()


@pytest.mark.parametrize(
    ("values", "calls", "ratio", "floor"),
    [
        ([1.0, 4.0], ["small", "big"], 8.0, 0.0),
        (
            [0.001, 0.01, 0.001, 0.001, 0.01, 0.01],
            ["small", "big", "small", "small", "big", "big"],
            8.0,
            0.05,
        ),
        (
            [1.0, 3.0, 1.0, 1.0, 3.0, 3.0],
            ["small", "big", "small", "small", "big", "big"],
            2.0,
            4.0,
        ),
    ],
)
def test_linear_cost_success_does_not_collect_failure_context(
    monkeypatch: pytest.MonkeyPatch,
    values: list[float],
    calls: list[str],
    ratio: float,
    floor: float,
) -> None:
    def forbidden() -> dict[str, object]:
        pytest.fail("A passing judge must not collect failure context")

    monkeypatch.setattr(code_fixtures, "_linear_failure_context", forbidden, raising=False)
    observed: list[str] = []
    measurements = iter(values)

    def cost(size: str) -> float:
        observed.append(size)
        return next(measurements)

    code_fixtures.assert_linear(cost, "small", "big", ratio=ratio, floor=floor)
    assert observed == calls


@pytest.mark.parametrize("values", [[1.0, 8.0], [1.0, 16.0, 2.0, 3.0, 17.0, 18.0]])
def test_context_is_collected_after_the_original_failed_judge(
    monkeypatch: pytest.MonkeyPatch, values: list[float]
) -> None:
    calls: list[str] = []
    measurements = iter(values)
    expected = ["private-small", "private-big"]
    if len(values) == 6:
        expected += ["private-small", "private-small", "private-big", "private-big"]
    seen: list[BaseException] = []
    context = {"process_cpu_ns_at_failure": 123, "gc_count_at_failure": [1, 2, 3]}

    def snapshot() -> dict[str, object]:
        assert calls == expected
        original = sys.exception()
        assert isinstance(original, AssertionError)
        assert original.args == ((values[0], values[1]),)
        seen.append(original)
        return context

    monkeypatch.setattr(code_fixtures, "_linear_failure_context", snapshot, raising=False)

    def cost(size: str) -> float:
        calls.append(size)
        return next(measurements)

    with pytest.raises(AssertionError) as error:
        code_fixtures.assert_linear(cost, "private-small", "private-big")

    assert error.value.args == ((values[0], values[1]),)
    assert calls == expected and seen == [error.value]
    notes = getattr(error.value, "__notes__", [])
    assert len(notes) == 1
    assert notes[0].startswith("linear_cost_context=")
    assert "private-small" not in notes[0] and "private-big" not in notes[0]
    assert json.loads(notes[0].removeprefix("linear_cost_context=")) == context


@pytest.mark.parametrize("failure", [AssertionError("cost failed"), RuntimeError("cost failed")])
def test_original_cost_error_does_not_collect_context(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def forbidden() -> dict[str, object]:
        pytest.fail("A cost error must propagate without collecting judge context")

    monkeypatch.setattr(code_fixtures, "_linear_failure_context", forbidden, raising=False)
    calls: list[int] = []

    def cost(size: int) -> float:
        calls.append(size)
        raise failure

    with pytest.raises(type(failure)) as error:
        code_fixtures.assert_linear(cost, 1, 4)
    assert error.value is failure
    assert calls == [1]
    assert not getattr(failure, "__notes__", [])


def test_context_error_retains_the_original_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable = RuntimeError("context unavailable")

    def snapshot() -> dict[str, object]:
        assert isinstance(sys.exception(), AssertionError)
        raise unavailable

    monkeypatch.setattr(code_fixtures, "_linear_failure_context", snapshot, raising=False)
    calls: list[int] = []

    def cost(size: int) -> float:
        calls.append(size)
        return float(size)

    with pytest.raises(AssertionError) as error:
        code_fixtures.assert_linear(cost, 1, 8)
    assert error.value.args == ((1.0, 8.0),)
    assert error.value.__cause__ is unavailable
    assert getattr(error.value, "__notes__", []) == ["linear_cost_context_error=RuntimeError"]
    assert calls == [1, 8]


def test_real_failure_context_contains_numeric_state_without_input_text() -> None:
    with pytest.raises(AssertionError) as error:
        code_fixtures.assert_linear(
            lambda size: 1.0 if size == "private-small" else 8.0, "private-small", "private-big"
        )
    notes = getattr(error.value, "__notes__", [])
    assert len(notes) == 1
    assert "private-small" not in notes[0] and "private-big" not in notes[0]
    context = json.loads(notes[0].removeprefix("linear_cost_context="))
    assert context["process_cpu_ns_at_failure"] >= 0
    assert context["monotonic_ns_at_failure"] >= 0
    assert context["allocated_blocks_at_failure"] >= 0
    assert len(context["gc_stats_at_failure"]) == 3

    def numeric(value: object) -> bool:
        if isinstance(value, dict):
            return all(numeric(item) for item in value.values())
        if isinstance(value, list):
            return all(numeric(item) for item in value)
        return type(value) in (int, bool)

    assert numeric(context)
