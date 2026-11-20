"""Driver work retains its error through secondary cleanup and artifact failures."""

import asyncio
import dataclasses
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from asyncpg.transaction import Transaction

from bench import cell_trigger_gate as gate
from tests.unit.test_bench_cell_trigger_gate import args, fixture_config, receipt_posture

FIRST = [None, ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]
SECOND = [None, OSError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]


def failure(kind):
    return None if kind is None else kind("injected failure")


async def observed(work):
    try:
        await work()
    except BaseException as error:  # noqa: BLE001 — native errors stay inside this test.
        return error
    return None


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected external I/O")

    monkeypatch.setattr(gate.asyncpg, "connect", forbidden)
    monkeypatch.setattr(gate.clickhouse_connect, "get_async_client", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def artifact_output(monkeypatch, tmp_path):
    artifacts = []

    def configure(errors):
        artifact = gate.Artifact(str(tmp_path / f"raw-{len(artifacts)}.jsonl"))
        artifacts.append(artifact)
        fixture, _ = fixture_config()
        artifact.emit(
            "configuration",
            manifest=dataclasses.asdict(fixture),
            shared_configuration_excludes=["cell_router_token"],
            credential_posture=receipt_posture(),
            api_configuration_attested=True,
        )
        original = artifact.file
        events = []

        class File:
            def write(self, line):
                kind = json.loads(line)["kind"]
                events.append(kind)
                if (error := errors.get(kind)) is not None:
                    raise error
                return original.write(line)

            def __getattr__(self, name):
                return getattr(original, name)

        artifact.file = File()
        return artifact, events

    yield configure
    for artifact in artifacts:
        artifact.close()


@pytest.fixture
def http_edges(monkeypatch):
    import server.settings

    fixture, config = fixture_config()
    assert fixture.validate(args(), config) == 45500
    assert config.cell_router_token == config.router_operator_token == ""
    monkeypatch.setattr(server.settings, "settings", config)
    monkeypatch.setattr(gate, "CALLS", 1)
    monkeypatch.setattr(gate, "WARMUP", 0)
    client_class = httpx.AsyncClient

    def configure(first=None, closing=None, *, bad_status=False):
        events = []

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                events.append("request")
                if first is not None:
                    raise first
                if request.url.path.endswith("/memory.set"):
                    sent = json.loads(request.content)
                    body = {
                        "memory_id": "00000000-0000-0000-0000-000000000001",
                        "version": 1,
                        "status": "active",
                        "memory_key": sent["memory_key"],
                        "namespace": sent["namespace"],
                        "content_sha256": gate.sha256_value(sent["value"]),
                    }
                elif request.url.path.endswith("/deployment"):
                    body = {"deployment_id": fixture.deployment_id}
                else:
                    body = {
                        "build_commit": fixture.build_commit,
                        "deployment_config": {"instance_id": fixture.instance_id},
                        "ch_ok": True,
                        "pg_ok": True,
                        "status": "ok",
                        "fake_embedding": True,
                        "fake_llm": True,
                        "pending_migrations": [],
                        "storage_authority": "postgres",
                        "llm_provider": config.llm_provider,
                        "llm_model": config.llm_model,
                        "embedding_model": config.embedding_model,
                        "embedding_dim_configured": config.embedding_dim,
                        "embedding_dim_probed": config.embedding_dim,
                        "instance": "test:1",
                    }
                return httpx.Response(503 if bad_status else 200, json=body)

            async def aclose(self):
                events.append("http close")
                if closing is not None:
                    raise closing

        def client(**kwargs):
            return client_class(**kwargs, transport=Transport())

        monkeypatch.setattr(gate.httpx, "AsyncClient", client)
        return fixture, events

    return configure


@pytest.mark.asyncio
@pytest.mark.parametrize("first_kind", FIRST)
@pytest.mark.parametrize("ch_kind", SECOND)
@pytest.mark.parametrize("pg_kind", SECOND)
async def test_preflight_retains_first_error_and_attempts_both_closes(
    monkeypatch, first_kind, ch_kind, pg_kind
):
    first, ch_error, pg_error = map(failure, (first_kind, ch_kind, pg_kind))
    events = []

    async def proof(*_args):
        events.append("proof")
        if first is not None:
            raise first

    async def close_ch():
        events.append("ch close")
        if ch_error is not None:
            raise ch_error

    async def close_pg(*_args):
        events.append("pg close")
        if pg_error is not None:
            raise pg_error

    monkeypatch.setattr(gate.asyncpg, "connect", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        gate.ReadOnlyCH, "open", AsyncMock(return_value=SimpleNamespace(close=close_ch))
    )
    monkeypatch.setattr(gate, "_proof", proof)
    monkeypatch.setattr(gate, "_close_pg", close_pg)
    assert await observed(lambda: gate._preflight(args(), None)) is (first or ch_error or pg_error)
    assert events == ["proof", "ch close", "pg close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["health", "memory"])
@pytest.mark.parametrize("first_kind", FIRST)
@pytest.mark.parametrize("close_kind", SECOND)
async def test_http_client_exit_retains_work_failure_and_requires_successful_close(
    http_edges, consumer, first_kind, close_kind
):
    first, closing = failure(first_kind), failure(close_kind)
    fixture, events = http_edges(first, closing)

    async def work():
        if consumer == "health":
            await gate._health(args(), fixture, {"health_instance": "test:1"})
        else:
            await gate._memory_set(args())

    assert await observed(work) is (first or closing)
    assert events[-1] == "http close"


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["health", "memory"])
async def test_semantic_http_refusal_survives_client_close_error(http_edges, consumer):
    fixture, events = http_edges(closing=OSError("close failed"), bad_status=True)

    async def work():
        if consumer == "health":
            await gate._health(args(), fixture, {"health_instance": "test:1"})
        else:
            await gate._memory_set(args())

    field = "health HTTP status" if consumer == "health" else "memory.set HTTP status"
    error = await observed(work)
    assert isinstance(error, gate.Invalid) and str(error) == field
    assert events[-1] == "http close"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["request", "request_record"])
@pytest.mark.parametrize("first_kind", FIRST[1:])
@pytest.mark.parametrize("output_kind", SECOND)
async def test_request_diagnostic_retains_request_or_required_record_failure(
    http_edges, artifact_output, source, first_kind, output_kind
):
    first, output = failure(first_kind), failure(output_kind)
    _, http_events = http_edges(first if source == "request" else None)
    errors = {"request_error": output}
    if source == "request_record":
        errors["request"] = first
    artifact, events = artifact_output(errors)
    assert await observed(lambda: gate._memory_set(args(), emit=artifact.emit)) is first
    assert events[-1] == "request_error" and http_events[-1] == "http close"


@pytest.mark.asyncio
@pytest.mark.parametrize("record", ["worker_error", "worker"])
@pytest.mark.parametrize("first_kind", FIRST[1:])
@pytest.mark.parametrize("output_kind", SECOND)
async def test_worker_diagnostics_and_prior_results_retain_worker_failure(
    monkeypatch, artifact_output, record, first_kind, output_kind
):
    from server.memory.workers import pg_delivery
    from server.settings import settings

    first, output = failure(first_kind), failure(output_kind)
    good = pg_delivery.DeliveryResult(claimed=1, published=1)
    outcomes = [good, first] if record == "worker" else [first]
    monkeypatch.setattr(gate, "BATCHES", len(outcomes))
    monkeypatch.setattr(settings, "pg_delivery_batch_rows", 1)
    worker = AsyncMock(side_effect=outcomes)
    monkeypatch.setattr(pg_delivery, "run_pass", worker)
    artifact, events = artifact_output({record: output})
    assert await observed(lambda: gate._delivery(args(), 1, emit=artifact.emit, arm=0)) is first
    assert worker.await_count == len(outcomes) and "worker_error" in events
    if record == "worker":
        assert events == ["worker_error", "worker"]


@pytest.mark.asyncio
@pytest.mark.parametrize("output_kind", SECOND)
async def test_successful_worker_work_still_requires_result_output(
    monkeypatch, artifact_output, output_kind
):
    from server.memory.workers import pg_delivery
    from server.settings import settings

    output = failure(output_kind)
    monkeypatch.setattr(gate, "BATCHES", 1)
    monkeypatch.setattr(settings, "pg_delivery_batch_rows", 1)
    worker = AsyncMock(return_value=pg_delivery.DeliveryResult(claimed=1, published=1))
    monkeypatch.setattr(pg_delivery, "run_pass", worker)
    ticks = iter([0, 1_000_000_000])
    monkeypatch.setattr(gate.time, "perf_counter_ns", lambda: next(ticks))
    artifact, events = artifact_output({"worker": output})
    assert await observed(lambda: gate._delivery(args(), 1, emit=artifact.emit, arm=0)) is output
    assert worker.await_count == 1
    assert events == (["worker"] if output is not None else ["worker", "delivery_block"])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["set", "restore"])
@pytest.mark.parametrize("first_kind", FIRST)
@pytest.mark.parametrize("exit_kind", SECOND)
async def test_trigger_work_retains_error_through_transaction_exit_and_owner_cleanup(
    monkeypatch, method, first_kind, exit_kind
):
    first, exit_error = failure(first_kind), failure(exit_kind)
    events = []

    class Connection:
        _pool_release_ctr = 0
        _top_xact = None
        _protocol = SimpleNamespace(is_in_transaction=lambda: False)

        def __init__(self):
            self.closed = False

        def transaction(self):
            return Transaction(self, None, False, False)

        def is_closed(self):
            return self.closed

        async def execute(self, sql):
            events.append(sql.split()[0])
            if sql.startswith("DROP") and first is not None:
                raise first
            if sql.startswith(("ROLLBACK", "COMMIT")) and exit_error is not None:
                raise exit_error

        async def close(self, **kwargs):
            events.append("public close")
            self.closed = True

        def terminate(self):
            events.append("abort")
            self.closed = True

    triggers = gate.Triggers(("memory_current",), "invariant", "org")
    monkeypatch.setattr(triggers, "verify", AsyncMock())
    monkeypatch.setattr(gate.cell_triggers, "missing", AsyncMock(return_value=[]))
    conn = Connection()

    async def work():
        if method == "set":
            await triggers.set(conn, False)
        else:
            await gate._restore(triggers, conn, time.monotonic() + 1)

    assert await observed(work) is (first or exit_error)
    if method == "restore" and (first is not None or exit_error is not None):
        assert conn.closed and "public close" in events
