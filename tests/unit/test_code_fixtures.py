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
    received = json.loads(notes[0].removeprefix("linear_cost_context="))
    samples = received.pop("samples")
    assert [sample["cost_s"] for sample in samples] == values
    assert received == context


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
        return type(value) in (int, float, bool)

    assert numeric(context)


@pytest.mark.parametrize("values", [[1.0, 8.0], [1.0, 16.0, 2.0, 3.0, 17.0, 18.0]])
def test_failed_cost_samples_keep_values_and_boundary_context(values: list[float]) -> None:
    calls: list[str] = []
    measurements = iter(values)

    def cost(size: str) -> float:
        calls.append(size)
        return next(measurements)

    with pytest.raises(AssertionError) as error:
        code_fixtures.assert_linear(cost, "private-small", "private-big")
    context = json.loads(error.value.__notes__[0].removeprefix("linear_cost_context="))
    samples = context["samples"]
    assert [sample["cost_s"] for sample in samples] == values
    expected = ["private-small", "private-big"]
    if len(values) == 6:
        expected += ["private-small", "private-small", "private-big", "private-big"]
    assert calls == expected
    assert error.value.args == ((values[0], values[1]),)
    assert "private-small" not in error.value.__notes__[0]
    for sample in samples:
        before, after = sample["before"], sample["after"]
        for clock in ("process_cpu_ns", "monotonic_ns", "thread_cpu_ns"):
            assert after[clock] >= before[clock] >= 0
        assert before["trace_active"] == after["trace_active"] == (sys.gettrace() is not None)
        assert before["profile_active"] == after["profile_active"] == (sys.getprofile() is not None)
        assert isinstance(before["gc_enabled"], bool)
        assert len(before["gc_count"]) == len(after["gc_count"]) == 3


def test_registry_sample_context_surrounds_original_cost_and_excludes_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.code.domain.extract import bounded_parse
    from tests.unit import lane_walks

    # The registry normally runs in a child; this check must restore its wrapper.
    monkeypatch.setattr(bounded_parse, "parse", bounded_parse.parse)
    clocks = iter([0.0, 1.0, 10.0, 11.0, 20.0, 24.0])
    monkeypatch.setattr(lane_walks, "time", SimpleNamespace(process_time=lambda: next(clocks)))
    monkeypatch.setattr(lane_walks, "SHAPES", [("observed", lambda n: "a\\n" * n)])
    calls: list[int] = []

    def extract(path: str, source: bytes, **kwargs: object) -> tuple[object, bool]:
        calls.append(len(source))
        return SimpleNamespace(lang="python", tree=object()), True

    monkeypatch.setattr(lane_walks, "_extract", extract)
    diagnostics: dict[str, list[dict[str, object]]] = {}
    got = lane_walks.registry_cost("python", diagnostics=diagnostics)
    assert got == [("observed", "N", 1.0, 4.0, 0, 0)]
    assert len(calls) == 3
    samples = diagnostics["observed"]
    assert [sample["cpu_s"] for sample in samples] == [1.0, 4.0]
    for sample in samples:
        assert sample["after"]["thread_cpu_ns"] >= sample["before"]["thread_cpu_ns"]


@pytest.mark.parametrize("large", [4.0, 8.0])
def test_sample_context_error_preserves_the_original_judge(
    monkeypatch: pytest.MonkeyPatch, large: float
) -> None:
    def unavailable() -> int:
        raise RuntimeError("private context error")

    monkeypatch.setattr(code_fixtures.time, "thread_time_ns", unavailable)

    def cost(n: int) -> float:
        return 1.0 if n == 1 else large

    if large == 4.0:
        code_fixtures.assert_linear(cost, 1, 4)
    else:
        with pytest.raises(AssertionError) as error:
            code_fixtures.assert_linear(cost, 1, 4)
        assert error.value.args == ((1.0, 8.0),)
        context = json.loads(error.value.__notes__[0].removeprefix("linear_cost_context="))
        assert [sample["cost_s"] for sample in context["samples"]] == [1.0, 8.0]
        for sample in context["samples"]:
            assert sample["before"] == sample["after"] == {"context_error": "RuntimeError"}
        assert "private context error" not in error.value.__notes__[0]


def test_sample_context_is_collected_outside_the_cost_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside = False
    snapshots = 0
    real = code_fixtures.linear_sample_context

    def snapshot() -> dict[str, object]:
        nonlocal snapshots
        assert not inside
        snapshots += 1
        return real()

    monkeypatch.setattr(code_fixtures, "linear_sample_context", snapshot)

    def cost(n: int) -> float:
        nonlocal inside
        inside = True
        try:
            return float(n)
        finally:
            inside = False

    code_fixtures.assert_linear(cost, 1, 4)
    assert snapshots == 4


@pytest.mark.parametrize("raises", [False, True])
def test_document_cpu_samples_surround_the_original_timed_call(
    monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    from types import SimpleNamespace

    from tests.unit import test_document_extraction as document

    clocks = iter([3.0, 5.0])
    monkeypatch.setattr(document, "time", SimpleNamespace(process_time=lambda: next(clocks)))
    snapshots: list[str] = []
    inside = False
    real = code_fixtures.linear_sample_context

    def snapshot() -> dict[str, object]:
        assert not inside
        snapshots.append("boundary")
        return real()

    monkeypatch.setattr(document, "linear_sample_context", snapshot, raising=False)
    failure = RuntimeError("private cost failure")

    def cost() -> None:
        nonlocal inside
        inside = True
        try:
            assert sys.gettrace() is None
            if raises:
                raise failure
        finally:
            inside = False

    samples: list[dict[str, object]] = []
    if raises:
        with pytest.raises(RuntimeError) as error:
            document._cpu(cost, samples=samples)
        assert error.value is failure
    else:
        assert document._cpu(cost, samples=samples) == 2.0
    assert snapshots == ["boundary", "boundary"]
    assert len(samples) == 1
    assert samples[0]["after"]["thread_cpu_ns"] >= samples[0]["before"]["thread_cpu_ns"]
    assert "private cost failure" not in json.dumps(samples)


def test_document_cpu_without_samples_does_not_collect_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tests.unit import test_document_extraction as document

    clocks = iter([3.0, 5.0])
    monkeypatch.setattr(document, "time", SimpleNamespace(process_time=lambda: next(clocks)))

    def forbidden() -> dict[str, object]:
        pytest.fail("A caller without diagnostics must not collect context")

    monkeypatch.setattr(document, "linear_sample_context", forbidden, raising=False)
    assert document._cpu(lambda: None) == 2.0


def test_sql_failure_keeps_the_complete_sample_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit import test_document_extraction as document

    values = iter([1.0, 25.0])
    recorded: list[dict[str, object]] = []

    def cpu(fn: object, *, samples: list[dict[str, object]]) -> float:
        context = {
            "before": {"thread_cpu_ns": len(recorded)},
            "after": {"thread_cpu_ns": len(recorded) + 1},
        }
        samples.append(context)
        recorded.append(context)
        return next(values)

    monkeypatch.setattr(document, "_cpu", cpu)
    with pytest.raises(AssertionError) as error:
        document.test_one_line_sql_with_a_comment_after_every_semicolon_splits_in_linear_time()
    assert json.loads(str(error.value.args[0]).splitlines()[0]) == {
        "small_s": 1.0,
        "large_s": 25.0,
        "samples": recorded,
    }
    assert len(recorded) == 2
