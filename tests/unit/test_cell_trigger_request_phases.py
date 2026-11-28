"""Selected request phases preserve the native delegate and its failures."""

import asyncio
import hashlib
import inspect
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from tests.unit.test_cell_trigger_ci_creator import keeper_config_case as keeper_config_case
from tests.unit.test_cell_trigger_ci_creator import request_cost_api_case as request_cost_api_case


@pytest.mark.parametrize("asynchronous", [True, False])
async def test_selected_phase_preserves_result_arguments_signature_and_bound_tag(
    keeper_config_case, monkeypatch, asynchronous
):
    native, _, _, _, _ = keeper_config_case
    sample = ContextVar("sample", default=None)
    sample.set({"ordinal": 8192, "done": 0})
    rows, seen = [], []
    state = {"tag": "private-previous-tag"}
    marker = object()

    def delegate(value, *, option):
        seen.append((value, option))
        state["tag"] = "memory.set private-current-request"
        return marker

    async def async_delegate(value, *, option):
        return delegate(value, option=option)

    original = async_delegate if asynchronous else delegate
    monkeypatch.setattr(native.time, "perf_counter_ns", lambda: 120)
    monkeypatch.setattr(native.time, "process_time_ns", lambda: 70)
    wrapped = native._observe_request_phase(
        original, 10, sample, 100, rows.append, lambda: state["tag"], asynchronous=asynchronous
    )
    result = (
        await wrapped(marker, option=marker) if asynchronous else wrapped(marker, option=marker)
    )
    assert result is marker and seen == [(marker, marker)]
    assert inspect.signature(wrapped) == inspect.signature(original)
    expected = int.from_bytes(hashlib.sha256(state["tag"].encode()).digest())
    assert rows == [[1, 8192, 10, 20, 0, 0, expected, 0, 0, 0, 0]]
    assert "private" not in str(rows)


@pytest.mark.parametrize("asynchronous", [True, False])
async def test_unselected_phase_touches_no_clock_tag_or_writer(
    keeper_config_case, monkeypatch, asynchronous
):
    native, _, _, _, _ = keeper_config_case
    sample = ContextVar("sample", default=None)
    marker = object()

    def forbidden(*args):
        raise AssertionError("unselected diagnostic ran")

    def original():
        return marker

    async def async_original():
        return marker

    monkeypatch.setattr(native.time, "perf_counter_ns", forbidden)
    monkeypatch.setattr(native.time, "process_time_ns", forbidden)
    wrapped = native._observe_request_phase(
        async_original if asynchronous else original,
        11,
        sample,
        0,
        forbidden,
        forbidden,
        asynchronous=asynchronous,
    )
    assert (await wrapped() if asynchronous else wrapped()) is marker


@pytest.mark.parametrize("error", [RuntimeError("delegate"), asyncio.CancelledError()])
async def test_original_async_failure_survives_diagnostic_storage_failure(
    keeper_config_case, error
):
    native, _, _, _, _ = keeper_config_case
    sample = ContextVar("sample", default=None)
    sample.set({"ordinal": 8192, "done": 0})

    async def original():
        raise error

    def broken_writer(row):
        assert row[8] == 1
        raise OSError("diagnostic disk")

    wrapped = native._observe_request_phase(
        original, 12, sample, 0, broken_writer, lambda: "", asynchronous=True
    )
    with pytest.raises(type(error)) as caught:
        await wrapped()
    assert caught.value is error


def test_sync_failure_survives_unencodable_optional_tag(keeper_config_case):
    native, _, _, _, _ = keeper_config_case
    sample = ContextVar("sample", default=None)
    sample.set({"ordinal": 8192, "done": 0})
    error = ValueError("delegate")

    def original():
        raise error

    wrapped = native._observe_request_phase(
        original, 10, sample, 0, lambda row: None, lambda: "\ud800", asynchronous=False
    )
    with pytest.raises(ValueError) as caught:
        wrapped()
    assert caught.value is error


def test_installed_http_boundaries_are_exact_and_restored(keeper_config_case):
    native, _, _, _, _ = keeper_config_case
    sample = ContextVar("sample", default=None)

    def begin():
        return object()

    async def phase():
        return object()

    names = (
        "_begin_request",
        "_extract_auth",
        "_read_body",
        "_authorize",
        "_bind_context_and_policy",
        "_idempotency_gate",
        "_start_telemetry",
        "_run_handler",
        "_finish_success",
    )
    originals = {name: begin if name == "_begin_request" else phase for name in names}
    http = SimpleNamespace(**originals)
    restore = native._observe_http_phases(http, sample, 0, lambda row: None, lambda: "")
    for name in names:
        assert getattr(http, name) is not originals[name]
        assert inspect.iscoroutinefunction(getattr(http, name)) == (name != "_begin_request")
    restore()
    assert vars(http) == originals


def test_partial_installation_restores_before_raising(keeper_config_case):
    native, _, _, _, _ = keeper_config_case
    sample = ContextVar("sample", default=None)

    def begin(*args):
        return None

    async def extract(*args):
        return None

    originals = {"_begin_request": begin, "_extract_auth": extract}
    http = SimpleNamespace(**originals)
    with pytest.raises(AttributeError):
        native._observe_http_phases(http, sample, 0, lambda row: None, lambda: "")
    assert vars(http) == originals


def test_optional_clock_failure_keeps_the_original_call(keeper_config_case, monkeypatch):
    native, _, _, _, _ = keeper_config_case
    sample = ContextVar("sample", default=None)
    sample.set({"ordinal": 8192, "done": 0})
    marker = object()
    calls, rows = [], []

    def original():
        calls.append(1)
        return marker

    def broken_clock():
        raise OSError("optional clock")

    monkeypatch.setattr(native.time, "perf_counter_ns", broken_clock)
    wrapped = native._observe_request_phase(
        original, 10, sample, 0, rows.append, lambda: "", asynchronous=False
    )
    assert wrapped() is marker and calls == [1] and rows == []


def test_native_phase_installation_restores_after_server_startup_error(request_cost_api_case):
    state = request_cost_api_case("startup-error")
    assert state["error"] is state["expected_error"]
    assert state["calls"] == 0 and state["rows"][-1] == [3, 0, 0, 0, 0]


def test_native_api_records_all_nine_boundaries_only_for_existing_samples(request_cost_api_case):
    state = request_cost_api_case("phases")
    assert state["error"] is None
    rows = state["rows"]
    phases = [row for row in rows if row[0] == 1 and row[2] >= 10]
    assert len(phases) == 54
    for ordinal, digest in state["tags"].items():
        selected = [row for row in phases if row[1] == ordinal]
        assert [row[2] for row in selected] == list(range(10, 19))
        assert all(row[6] == digest and row[8:] == [0, 0, 0] for row in selected)
    assert rows[-1] == [3, 18432, 72, 0, 0]
    assert state["calls"] == 18432 and state["sends"] == 2 * 18432
