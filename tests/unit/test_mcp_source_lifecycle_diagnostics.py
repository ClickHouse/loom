"""Fixture failures report bounded locations without loading source or error payloads."""

import json
import linecache

import pytest

from scripts.gates.mcp_source_lifecycle import failure_detail


def _error(depth: int) -> RuntimeError:
    if depth:
        return _error(depth - 1)
    try:
        raise RuntimeError("fixture-private-credential-and-query")
    except RuntimeError as error:
        return error


def test_lifecycle_diagnostics_exclude_payload_and_source_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_source(*args: object, **kwargs: object) -> str:
        raise AssertionError("diagnostics must not load source lines")

    error = _error(0)
    with monkeypatch.context() as patch:
        patch.setattr(linecache, "getline", no_source)
        detail = failure_detail(error)
    assert detail["type"] == "RuntimeError"
    assert "fixture-private" not in json.dumps(detail)
    assert 0 < len(detail["frames"]) <= 8
    assert all(not frame["path"].startswith("/") for frame in detail["frames"])
    assert all(set(frame) == {"path", "line"} for frame in detail["frames"])


def test_lifecycle_diagnostics_bound_deep_failure_frames() -> None:
    def recurse(depth: int) -> None:
        if depth:
            recurse(depth - 1)
        else:
            raise RuntimeError("fixture-private-deep-payload")

    try:
        recurse(100)
    except RuntimeError as error:
        detail = failure_detail(error)
    else:
        pytest.fail("fixture must raise")
    assert detail["type"] == "RuntimeError"
    assert len(detail["frames"]) == 8
    assert "fixture-private" not in json.dumps(detail)
