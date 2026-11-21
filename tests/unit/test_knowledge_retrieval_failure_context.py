"""Fixture failures expose ingestion outcomes without printing stored content."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from server.core import ch
from server.knowledge.app import ingest_run
from server.knowledge.store import ingestions
from tests.integration import test_knowledge_ingest_live as ingest_native
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_state", "count", "expected"),
    [
        ("pending", 0, "pending"),
        ("processing", 0, "processing"),
        ("ready", 0, "ready"),
        ("partial", 0, "partial"),
        ("failed", 0, "failed"),
        ("cancelled", 0, "cancelled"),
        ("ready", 2, "ready"),
        ("future-state", 0, "unknown"),
        ("secret", 0, "unknown"),
    ],
)
async def test_dataset_failure_reports_worker_outcome_once(
    monkeypatch: pytest.MonkeyPatch, worker_state: str, count: int, expected: str
) -> None:
    calls = _install_dataset(monkeypatch, worker_state, count)
    with pytest.raises(AssertionError) as caught:
        await ingest_native.test_a_delete_bounded_before_a_metadata_edit_keeps_the_carried_snapshots_rows(
            "http://fixture.test"
        )
    assert str(caught.value).split("\n", 1)[0] == repr(
        {"dataset_count": count, "worker_state": expected}
    )
    assert "secret" not in str(caught.value)
    assert calls == [
        "formats",
        "http-enter",
        "upload",
        "ingest",
        "work",
        "dataset_get",
        "http-exit",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_state", ["ready", "pending"])
async def test_one_dataset_keeps_the_metadata_and_delete_checks_without_status_reads(
    monkeypatch: pytest.MonkeyPatch, worker_state: str
) -> None:
    calls = _install_dataset(monkeypatch, worker_state, 1)
    await (
        ingest_native.test_a_delete_bounded_before_a_metadata_edit_keeps_the_carried_snapshots_rows(
            "http://fixture.test"
        )
    )
    assert calls == [
        "formats",
        "http-enter",
        "upload",
        "ingest",
        "work",
        "dataset_get",
        "connect",
        "live_version",
        "update_metadata",
        "clock",
        "forget_published",
        "disconnect",
        "catalog",
        "deletes",
        "catalog",
        "deletes",
        "dataset_get_sample",
        "http-exit",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["row_count", "rows"])
async def test_one_dataset_still_checks_the_carried_snapshot_rows(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    calls = _install_dataset(monkeypatch, "ready", 1, corrupt=field)
    with pytest.raises(AssertionError):
        await ingest_native.test_a_delete_bounded_before_a_metadata_edit_keeps_the_carried_snapshots_rows(
            "http://fixture.test"
        )
    assert calls[-2:] == ["dataset_get_sample", "http-exit"]
    assert "read" not in calls


def _install_dataset(
    monkeypatch: pytest.MonkeyPatch, worker_state: str, count: int, *, corrupt: str = ""
) -> list[str]:
    from server.knowledge.app import extractors
    from server.knowledge.store import datasets

    calls: list[str] = []
    org, ns, uri = "secret-org", "docs", "repo://edit.csv"
    initial = {"snapshot_id": "secret-snapshot", "row_count": 2, "content": "secret"}

    class Client:
        async def connect(self) -> None:
            calls.append("connect")

        async def disconnect(self) -> None:
            calls.append("disconnect")

        async def query_one(self, sql: str) -> dict[str, str]:
            assert sql == "SELECT now() AS t"
            calls.append("clock")
            return {"t": "fixture-clock"}

    class HTTPClient:
        def __init__(self, *, base_url: str, timeout: int) -> None:
            assert (base_url, timeout) == ("http://fixture.test", 60)

        async def __aenter__(self) -> HTTPClient:
            calls.append("http-enter")
            return self

        async def __aexit__(self, *_: Any) -> None:
            calls.append("http-exit")

    async def formats() -> set[str]:
        calls.append("formats")
        return {"csv"}

    async def upload(client: Any, *args: Any) -> httpx.Response:
        assert isinstance(client, HTTPClient)
        assert args == (org, ns, b"k,v\na,1\nb,2\n", "edit.csv")
        calls.append("upload")
        return httpx.Response(201, json={"upload_id": "secret-upload"})

    async def op(client: Any, operation: str, **body: Any) -> dict[str, Any]:
        assert isinstance(client, HTTPClient)
        assert (body["org"], body["namespace"], body["source_uri"]) == (org, ns, uri)
        if operation == "knowledge.ingest":
            assert body["upload_id"] == "secret-upload"
            calls.append("ingest")
            return {"ingestion_id": "secret-ingestion"}
        if operation == "knowledge.dataset_get":
            if "sample_rows" not in body:
                calls.append("dataset_get")
                return {"datasets": [dict(initial) for _ in range(count)]}
            assert body["sample_rows"] == 5
            calls.append("dataset_get_sample")
            served = {**initial, "rows": [{"k": "a"}, {"k": "b"}]}
            if corrupt:
                served[corrupt] = 3 if corrupt == "row_count" else [{"k": "wrong"}]
            return {"datasets": [served]}
        assert operation == "knowledge.update_metadata"
        assert body["title"] == "Edited"
        calls.append("update_metadata")
        return {}

    async def work(*args: Any) -> str:
        assert args == (org, ns, "secret-ingestion")
        calls.append("work")
        return worker_state

    async def read(*_: Any, **__: Any) -> None:
        calls.append("read")
        raise AssertionError("no diagnostic status lookup")

    async def live_version(client: Any, *args: Any) -> int:
        assert isinstance(client, Client) and args == (org, ns, uri)
        calls.append("live_version")
        return 7

    async def forget(*args: Any, **kwargs: Any) -> None:
        assert args == (org, ns, {uri: 7}) and isinstance(kwargs["ch"], Client)
        calls.append("forget_published")

    async def catalog(*args: Any) -> bool:
        assert args == (org, ns, uri, 7)
        calls.append("catalog")
        return False

    async def deletes(since: str) -> bool:
        assert since == "fixture-clock"
        calls.append("deletes")
        return False

    monkeypatch.setattr(ch, "get_client", Client)
    monkeypatch.setattr(extractors, "available_formats", formats)
    monkeypatch.setattr(ingest_native.httpx, "AsyncClient", HTTPClient)
    monkeypatch.setattr(ingest_native, "unique_org", lambda _: org)
    monkeypatch.setattr(ingest_native, "_upload", upload)
    monkeypatch.setattr(ingest_native, "_op", op)
    monkeypatch.setattr(ingest_native, "_work", work)
    monkeypatch.setattr(ingest_native, "_live_version", live_version)
    monkeypatch.setattr(ingest_native, "_catalog_at", catalog)
    monkeypatch.setattr(ingest_native, "_deletes_running", deletes)
    monkeypatch.setattr(datasets, "forget_published", forget)
    monkeypatch.setattr(ingestions, "get_ingestion", read)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["ready", "pending", "processing", "partial", "failed", "cancelled", "future-state", None],
)
async def test_worker_helper_returns_its_single_attempt_outcome(
    monkeypatch: pytest.MonkeyPatch, outcome: str | None
) -> None:
    calls: list[str] = []

    class Client:
        async def connect(self) -> None:
            calls.append("connect")

        async def disconnect(self) -> None:
            calls.append("disconnect")

    async def read(*args: Any) -> dict[str, str]:
        assert args == ("fixture-org", "fixture-ns", "fixture-ingestion")
        calls.append("read")
        return {"ingestion_id": "fixture-ingestion"}

    async def run(row: dict[str, str]) -> str | None:
        assert row == {"ingestion_id": "fixture-ingestion"}
        calls.append("run")
        return outcome

    monkeypatch.setattr(ch, "get_client", Client)
    monkeypatch.setattr(ingestions, "get_ingestion", read)
    monkeypatch.setattr(ingest_run, "take_and_run", run)
    if outcome is None:
        with pytest.raises(AssertionError, match="job fixture-ingestion did not run"):
            await ingest_native._work("fixture-org", "fixture-ns", "fixture-ingestion")
    else:
        assert (
            await ingest_native._work("fixture-org", "fixture-ns", "fixture-ingestion") == outcome
        )
    assert calls == ["connect", "read", "run", "disconnect"]
