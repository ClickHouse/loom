"""The retrieval fixture exposes ingestion state without printing stored content."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from server.core import ch
from server.knowledge.app import ingest_run
from server.knowledge.store import ingestions
from tests.integration import test_knowledge_retrieval_floors as native


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        (
            {"state": "pending", "stage": "embed", "error_code": "retrying", "attempts": 1},
            {"state": "pending", "stage": "embed", "error_code": "retrying", "attempts": 1},
        ),
        (
            {"state": "pending", "stage": "publish", "error_code": "", "attempts": 0},
            {"state": "pending", "stage": "publish", "error_code": "", "attempts": 0},
        ),
        (None, {"observation": "missing"}),
        (
            {"state": "secret", "stage": ["secret"], "error_code": "secret", "attempts": True},
            {"state": "unknown", "stage": "unknown", "error_code": "other", "attempts": None},
        ),
        (
            {"state": "failed", "stage": "", "error_code": "retries_exhausted", "attempts": -1},
            {"state": "failed", "stage": "", "error_code": "retries_exhausted", "attempts": None},
        ),
        (RuntimeError("secret"), {"observation": "unavailable"}),
    ],
)
async def test_failed_ingestion_reads_bounded_status_once(
    monkeypatch: pytest.MonkeyPatch, observed: Any, expected: dict[str, Any]
) -> None:
    calls = await _install(monkeypatch, "pending", observed)
    with pytest.raises(AssertionError) as caught:
        await native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", [])
    assert str(caught.value).split("\n", 1)[0] == repr(("regions.csv", expected))
    assert "secret" not in str(caught.value)
    assert calls == ["connect", "read", "run", "read", "disconnect"]


@pytest.mark.asyncio
async def test_ready_ingestion_does_not_read_diagnostic_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = await _install(monkeypatch, "ready", RuntimeError("must not read"))
    await native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", [])
    assert calls == ["connect", "read", "run", "disconnect"]


async def _install(monkeypatch: pytest.MonkeyPatch, result: str, observed: Any) -> list[str]:
    calls: list[str] = []

    class Client:
        async def connect(self) -> None:
            calls.append("connect")

        async def disconnect(self) -> None:
            calls.append("disconnect")

    async def post(*_: Any, **__: Any) -> httpx.Response:
        return httpx.Response(201, json={"upload_id": "fixture-upload"})

    async def op(*_: Any, **__: Any) -> dict[str, str]:
        return {"ingestion_id": "fixture-ingestion"}

    async def get(org: str, namespace: str, ingestion_id: str) -> Any:
        assert (org, namespace, ingestion_id) == ("fixture-org", "fixture-ns", "fixture-ingestion")
        calls.append("read")
        if calls.count("read") == 1:
            return {"ingestion_id": "fixture-ingestion"}
        if isinstance(observed, Exception):
            raise observed
        if isinstance(observed, asyncio.Event):
            try:
                await observed.wait()
            finally:
                calls.append("observation-cancelled")
        if observed is None:
            return None
        return {**observed, "error_message": "secret", "content": "secret", "metadata": "secret"}

    async def run(row: dict[str, str]) -> str:
        assert row == {"ingestion_id": "fixture-ingestion"}
        calls.append("run")
        return result

    monkeypatch.setattr(ch, "get_client", Client)
    monkeypatch.setattr(native, "_post", post)
    monkeypatch.setattr(native, "_op", op)
    monkeypatch.setattr(ingestions, "get_ingestion", get)
    monkeypatch.setattr(ingest_run, "take_and_run", run)
    return calls


@pytest.mark.asyncio
async def test_blocked_status_read_does_not_hide_the_ingestion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = await _install(monkeypatch, "pending", asyncio.Event())
    with pytest.raises(AssertionError) as caught:
        await asyncio.wait_for(
            native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", []),
            timeout=2,
        )
    assert "unavailable" in str(caught.value)
    assert calls == ["connect", "read", "run", "read", "observation-cancelled", "disconnect"]
