"""Fixture failures expose ingestion outcomes without printing stored content."""

from __future__ import annotations

import ast
import asyncio
from typing import Any

import httpx
import pytest

from server.core import ch
from server.knowledge.app import ingest_run
from server.knowledge.store import ingestions
from tests.integration import test_knowledge_ingest_live as ingest_native
from tests.integration import test_knowledge_retrieval_floors as native

_observe_ingest_retries = native._observe_ingest_retries


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
    assert str(caught.value).split("\n", 1)[0] == repr(("regions.csv", expected, {}))
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected", "native_type", "code"),
    [
        ("overload", "OverloadedError", "DatabaseError", 241),
        ("embed", "EmbedTimeoutError", "none", None),
    ],
)
async def test_retry_assertion_identifies_caught_class_and_numeric_native_code(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    kind: str,
    expected: str,
    native_type: str,
    code: int | None,
) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.core.errors import EmbedTimeoutError, OverloadedError

    error = OverloadedError("private-wrapper") if kind == "overload" else EmbedTimeoutError()
    if kind == "overload":
        error.__cause__ = DatabaseError("Code: 241. private-SQL-URL-token")
    calls, delegated, job = await _install_retry(monkeypatch, error)
    request.getfixturevalue("_observe_ingest_retries")
    with pytest.raises(AssertionError) as caught:
        await native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", [])
    observed = ast.literal_eval(str(caught.value).split("\n", 1)[0])[2]
    assert observed.get("retry_exception") == expected
    assert observed.get("native_exception") == native_type
    assert observed.get("native_code") == code
    assert "private-" not in str(caught.value)
    assert len(delegated) == 1 and delegated[0][0] is job and delegated[0][1] is error
    assert calls == ["connect", "read", "run", "read", "disconnect"]


async def _install_retry(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    *,
    result: str = "pending",
    raised: BaseException | None = None,
) -> tuple[list[str], list[tuple[dict[str, Any], Exception]], dict[str, Any]]:
    calls = await _install(
        monkeypatch,
        result,
        {"state": "pending", "stage": "embed", "error_code": "retrying", "attempts": 1},
    )
    delegated: list[tuple[dict[str, Any], Exception]] = []
    job = {"org": "fixture-org", "namespace": "fixture-ns", "ingestion_id": "fixture-ingestion"}

    async def retry(actual_job: dict[str, Any], actual_error: Exception) -> str:
        delegated.append((actual_job, actual_error))
        if raised is not None:
            raise raised
        return result

    async def work(row: dict[str, Any]) -> str:
        assert row == {"ingestion_id": "fixture-ingestion"}
        calls.append("run")
        return await ingest_run._retry_or_fail(job, error)

    monkeypatch.setattr(ingest_run, "_retry_or_fail", retry)
    monkeypatch.setattr(ingest_run, "take_and_run", work)
    return calls, delegated, job


@pytest.mark.parametrize(
    "kind",
    [
        "BackendUnavailableError",
        "BackendTimeoutError",
        "OverloadedError",
        "EmbedTimeoutError",
        "ExtractorUnavailable",
        "PostgresUnavailableError",
    ],
)
def test_retry_class_labels_are_closed(kind: str) -> None:
    from server.core import errors, pg
    from server.knowledge.app import extractors

    cls = getattr(errors, kind, None) or getattr(pg, kind, None) or getattr(extractors, kind)
    assert native._retry_failure(cls()) == {
        "retry_exception": kind,
        "native_exception": "none",
        "native_code": None,
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (-1, None),
        (0, 0),
        (159, 159),
        (1_000_000, 1_000_000),
        (1_000_001, None),
        (True, None),
        ("private-code", None),
        (None, 241),
    ],
)
def test_native_code_requires_a_bounded_strict_integer(code: Any, expected: int | None) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.core.errors import OverloadedError

    error = OverloadedError("private-wrapper")
    cause = DatabaseError("Code: 241. private-fallback")
    cause.code = code
    error.__cause__ = cause
    assert native._retry_failure(error) == {
        "retry_exception": "OverloadedError",
        "native_exception": "DatabaseError",
        "native_code": expected,
    }


@pytest.mark.parametrize(("late", "expected"), [(False, 159), (True, None)])
def test_native_code_parser_reads_only_a_bounded_existing_string(
    late: bool, expected: int | None
) -> None:
    from clickhouse_connect.driver.exceptions import OperationalError

    from server.core.errors import BackendUnavailableError

    error = BackendUnavailableError()
    error.__cause__ = OperationalError(
        ("private-" * 600 if late else "") + "Code: 159. private-SQL"
    )
    out = native._retry_failure(error)
    assert out == {
        "retry_exception": "BackendUnavailableError",
        "native_exception": "OperationalError",
        "native_code": expected,
    }
    assert "private-" not in repr(out)


@pytest.mark.parametrize("depth", [4, 5])
def test_native_cause_walk_stops_after_four_links(depth: int) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.core.errors import BackendUnavailableError

    error = BackendUnavailableError()
    previous = error
    for _ in range(depth - 1):
        cause = BackendUnavailableError()
        previous.__cause__ = cause
        previous = cause
    previous.__cause__ = DatabaseError("Code: 241. private-query")
    out = native._retry_failure(error)
    assert out["native_code"] == (241 if depth == 4 else None)
    assert "private-" not in repr(out)


def test_cyclic_native_causes_and_unknown_classes_emit_no_free_text() -> None:
    from server.core.errors import BackendUnavailableError, OverloadedError

    class PrivateError(Exception):
        def __str__(self) -> str:
            raise AssertionError("must not format exception")

    unknown = native._retry_failure(PrivateError("private-value"))
    assert unknown == {
        "retry_exception": "other",
        "native_exception": "none",
        "native_code": None,
    }
    error, cause = OverloadedError(), BackendUnavailableError()
    error.__cause__, cause.__cause__ = cause, error
    assert native._retry_failure(error) == {
        "retry_exception": "OverloadedError",
        "native_exception": "other",
        "native_code": None,
    }


@pytest.mark.parametrize("shape", ["argument", "subclass"])
def test_native_diagnostic_does_not_format_unknown_objects(shape: str) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.core.errors import OverloadedError

    class PrivateValue:
        def __str__(self) -> str:
            raise AssertionError("must not format argument")

        def __repr__(self) -> str:
            raise AssertionError("must not represent argument")

    class PrivateDatabaseError(DatabaseError):
        def __init__(self) -> None:
            Exception.__init__(self, "private-subclass")

        def __str__(self) -> str:
            raise AssertionError("must not format subclass")

        @property
        def code(self) -> int:
            raise AssertionError("must not read subclass property")

    error = OverloadedError()
    error.__cause__ = (
        DatabaseError(PrivateValue()) if shape == "argument" else PrivateDatabaseError()
    )
    out = native._retry_failure(error)
    assert out["retry_exception"] == "OverloadedError" and out["native_code"] is None
    assert out["native_exception"] == ("DatabaseError" if shape == "argument" else "other")


@pytest.mark.asyncio
async def test_failed_numeric_observation_cannot_replace_worker_outcome(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.core import ch_errors
    from server.core.errors import OverloadedError

    def unavailable(_: str) -> int:
        raise RuntimeError("private-parser")

    error = OverloadedError()
    error.__cause__ = DatabaseError("Code: 241. private-query")
    calls, delegated, _ = await _install_retry(monkeypatch, error)
    monkeypatch.setattr(ch_errors, "error_code", unavailable)
    request.getfixturevalue("_observe_ingest_retries")
    with pytest.raises(AssertionError) as caught:
        await native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", [])
    observed = ast.literal_eval(str(caught.value).split("\n", 1)[0])[2]
    assert observed["native_exception"] == "unavailable" and observed["native_code"] is None
    assert len(delegated) == 1 and calls == ["connect", "read", "run", "read", "disconnect"]
    assert "private-" not in str(caught.value) and native._retry_attempt.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["org", "namespace", "ingestion_id"])
async def test_retry_observer_excludes_every_other_scope(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, field: str
) -> None:
    from server.core.errors import OverloadedError

    calls, delegated, job = await _install_retry(monkeypatch, OverloadedError("private-error"))
    job[field] = "private-other-scope"
    request.getfixturevalue("_observe_ingest_retries")
    with pytest.raises(AssertionError) as caught:
        await native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", [])
    observed = ast.literal_eval(str(caught.value).split("\n", 1)[0])[2]
    assert "retry_exception" not in observed and "private-" not in str(caught.value)
    assert len(delegated) == 1 and calls == ["connect", "read", "run", "read", "disconnect"]


@pytest.mark.asyncio
async def test_retry_observer_excludes_an_inheriting_sibling_task(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    from server.core.errors import OverloadedError

    error = OverloadedError("private-error")
    calls, delegated, job = await _install_retry(monkeypatch, error)

    async def child() -> str:
        return await ingest_run._retry_or_fail(job, error)

    async def work(_: Any) -> str:
        calls.append("run")
        return await asyncio.create_task(child())

    monkeypatch.setattr(ingest_run, "take_and_run", work)
    request.getfixturevalue("_observe_ingest_retries")
    with pytest.raises(AssertionError) as caught:
        await native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", [])
    observed = ast.literal_eval(str(caught.value).split("\n", 1)[0])[2]
    assert "retry_exception" not in observed and native._retry_attempt.get() is None
    assert len(delegated) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_scope", [False, True])
async def test_overlapping_helpers_keep_observations_separate_without_repatching(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, same_scope: bool
) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    from server.core.errors import BackendUnavailableError, OverloadedError

    calls: list[str] = []
    reads: dict[Any, int] = {}
    entered: list[dict[str, Any]] = []
    delegated: list[tuple[dict[str, Any], Exception]] = []
    both_entered = asyncio.Event()
    errors = [OverloadedError("private-first"), BackendUnavailableError("private-second")]
    for error, code in zip(errors, [241, 210], strict=True):
        error.__cause__ = DatabaseError(f"Code: {code}. private-SQL")

    class Client:
        async def connect(self) -> None:
            calls.append("connect")

        async def disconnect(self) -> None:
            assert native._retry_attempt.get() is None
            calls.append("disconnect")

    async def post(*_: Any, **__: Any) -> httpx.Response:
        return httpx.Response(201, json={"upload_id": "private-upload"})

    async def op(*_: Any, **body: Any) -> dict[str, str]:
        return {"ingestion_id": body["namespace"]}

    async def get(org: str, ns: str, ingestion_id: str) -> dict[str, Any]:
        assert native._retry_attempt.get() is None
        assert ingest_run._retry_or_fail is installed
        task = asyncio.current_task()
        reads[task] = reads.get(task, 0) + 1
        calls.append("read")
        if reads[task] == 1:
            return {"org": org, "namespace": ns, "ingestion_id": ingestion_id}
        return {
            "state": "pending",
            "stage": "embed",
            "error_code": "retrying",
            "attempts": 1,
            "error_message": "private-status",
        }

    async def retry(job: dict[str, Any], error: Exception) -> str:
        delegated.append((job, error))
        return "pending"

    async def work(row: dict[str, Any]) -> str:
        index = len(entered)
        entered.append(row)
        calls.append("run")
        if len(entered) == 2:
            both_entered.set()
        await both_entered.wait()
        return await ingest_run._retry_or_fail(row, errors[index])

    monkeypatch.setattr(ch, "get_client", Client)
    monkeypatch.setattr(native, "_post", post)
    monkeypatch.setattr(native, "_op", op)
    monkeypatch.setattr(ingestions, "get_ingestion", get)
    monkeypatch.setattr(ingest_run, "_retry_or_fail", retry)
    monkeypatch.setattr(ingest_run, "take_and_run", work)
    request.getfixturevalue("_observe_ingest_retries")
    installed = ingest_run._retry_or_fail
    request.getfixturevalue("_observe_ingest_retries")
    assert ingest_run._retry_or_fail is installed
    namespaces = ["private-first", "private-first" if same_scope else "private-second"]
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                native._ingest(None, "private-org", ns, "regions.csv", b"csv", [])
                for ns in namespaces
            ),
            return_exceptions=True,
        ),
        timeout=2,
    )
    observed = []
    for result in results:
        assert isinstance(result, AssertionError)
        assert "private-" not in str(result)
        observed.append(ast.literal_eval(str(result).split("\n", 1)[0])[2])
    assert [(out["retry_exception"], out["native_code"]) for out in observed] == [
        ("OverloadedError", 241),
        ("BackendUnavailableError", 210),
    ]
    assert len(delegated) == 2
    assert {(id(job), id(error)) for job, error in delegated} == {
        (id(entered[i]), id(errors[i])) for i in range(2)
    }
    assert calls.count("connect") == calls.count("disconnect") == calls.count("run") == 2
    assert calls.count("read") == 4 and native._retry_attempt.get() is None
    assert ingest_run._retry_or_fail is installed


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_delegate_exception_and_cancellation_keep_identity_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, cancelled: bool
) -> None:
    from server.core.errors import OverloadedError

    raised = (
        asyncio.CancelledError("private-cancel") if cancelled else RuntimeError("private-error")
    )
    calls, delegated, _ = await _install_retry(monkeypatch, OverloadedError(), raised=raised)
    request.getfixturevalue("_observe_ingest_retries")
    with pytest.raises(type(raised)) as caught:
        await native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", [])
    assert caught.value is raised and native._retry_attempt.get() is None
    assert len(delegated) == 1 and calls == ["connect", "read", "run", "disconnect"]


@pytest.mark.asyncio
async def test_ready_delegate_adds_no_status_read_or_failure_message(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    from server.core.errors import OverloadedError

    calls, delegated, _ = await _install_retry(
        monkeypatch, OverloadedError("private"), result="ready"
    )
    request.getfixturevalue("_observe_ingest_retries")
    await native._ingest(None, "fixture-org", "fixture-ns", "regions.csv", b"csv", [])
    assert len(delegated) == 1 and calls == ["connect", "read", "run", "disconnect"]
    assert native._retry_attempt.get() is None


@pytest.mark.asyncio
async def test_dispatcher_returns_the_same_awaitable_and_fixture_restores_once() -> None:
    original = ingest_run._retry_or_fail
    calls: list[tuple[Any, Any]] = []
    job, error, result = {}, RuntimeError("private"), object()

    class Answer:
        def __await__(self):
            if False:
                yield
            return result

    answer = Answer()

    def delegate(actual_job: Any, actual_error: Any) -> Any:
        calls.append((actual_job, actual_error))
        return answer

    with pytest.MonkeyPatch.context() as boundary:
        boundary.setattr(ingest_run, "_retry_or_fail", delegate)
        native._observe_ingest_retries.__wrapped__(boundary)
        returned = ingest_run._retry_or_fail(job, error)
        assert returned is answer and await returned is result
        assert len(calls) == 1 and calls[0][0] is job and calls[0][1] is error
    assert ingest_run._retry_or_fail is original and native._retry_attempt.get() is None
