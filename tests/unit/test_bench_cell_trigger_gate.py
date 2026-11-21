"""Offline proofs for the fixed cell trigger timing protocol; no fixture is opened."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bench import cell_trigger_gate as gate


@pytest.mark.parametrize("fault", ["", "query", "overflow"])
async def test_physical_cost_diagnostics_are_bounded_and_exclude_query_text(monkeypatch, fault):
    from server.core import ch_names

    monkeypatch.setattr(ch_names, "database", lambda: "gate_cell")
    monkeypatch.setattr(ch_names, "telemetry_database", lambda: "gate_cell_t")
    cost = {
        "shape": 123,
        "calls": 25,
        "elapsed_ms": 900,
        "max_ms": 75,
        "read_rows": 10000,
        "read_bytes": 20000,
        "written_rows": 6,
        "cpu_us": 100000,
        "kind_mask": 1,
        "table_mask": 8,
        "query": "secret-token raw SQL",
        "log_comment": "secret-subject",
    }
    costs = [cost] * (21 if fault == "overflow" else 1)
    observation = RuntimeError("secret-token") if fault == "query" else costs
    ch = SimpleNamespace(query_all=AsyncMock(side_effect=[observation, [{"rows": 1, "parts": 1}]]))
    conn = SimpleNamespace(fetch=AsyncMock(return_value=[]), fetchval=AsyncMock(return_value=1))
    query_costs = await gate._query_costs(ch, ["gate_cell", "gate_cell_t"])
    assert query_costs.get("window_seconds") == 60
    result = await gate._physical(conn, ch, query_costs=query_costs)
    expected = (
        {
            "window_seconds": 60,
            "error_type": "RuntimeError" if fault == "query" else "Invalid",
            "error_code": 0,
        }
        if fault
        else {
            "window_seconds": 60,
            "rows": [{key: value for key, value in cost.items() if type(value) is int}],
        }
    )
    assert result.get("clickhouse_query_costs") == expected
    assert result["clickhouse_parts"] == [{"rows": 1, "parts": 1}]
    assert result["postgres_rows"] == dict.fromkeys(
        ("memory_current", "memory_revisions", "memory_deliveries", "audit_outbox"), 1
    )
    assert "secret" not in json.dumps(result)
    sql = ch.query_all.await_args_list[0].args[0]
    assert "normalized_query_hash" in sql and "LIMIT 20" in sql
    assert "max_execution_time=3" in sql and "max_rows_to_read=1000000" in sql
    assert "max_bytes_to_read=134217728" in sql and "max_result_bytes=16384" in sql
    pg_sql = conn.fetch.await_args.args[0]
    assert "seq_tup_read" in pg_sql and "idx_tup_fetch" in pg_sql


@pytest.mark.parametrize("kind,table", [(1, 8), (2, 0), (4, 0), (7, 8191)])
async def test_query_cost_window_preserves_only_numeric_masked_metrics(kind, table):
    row = dict.fromkeys(
        (
            "shape",
            "calls",
            "elapsed_ms",
            "max_ms",
            "read_rows",
            "read_bytes",
            "written_rows",
            "cpu_us",
        ),
        1,
    )
    row.update(kind_mask=kind, table_mask=table)
    private = {
        "query": "secret raw SQL",
        "log_comment": "secret token",
        "tables": ["foreign.secret"],
        "query_kind": "secret unsupported kind",
    }
    ch = SimpleNamespace(query_all=AsyncMock(return_value=[{**row, **private}]))
    result = await gate._query_costs(ch, ["gate_cell", "gate_cell_t"])
    assert result == {"window_seconds": 60, "rows": [row]}
    assert "secret" not in json.dumps(result)
    assert ch.query_all.await_count == 1


@pytest.mark.parametrize("kind,table", [(True, 0), (8, 0), (1, 8192), (1, -1)])
async def test_query_cost_masks_refuse_unbounded_values(kind, table):
    row = dict.fromkeys(
        (
            "shape",
            "calls",
            "elapsed_ms",
            "max_ms",
            "read_rows",
            "read_bytes",
            "written_rows",
            "cpu_us",
        ),
        0,
    )
    row.update(kind_mask=kind, table_mask=table)
    ch = SimpleNamespace(query_all=AsyncMock(return_value=[row]))
    result = await gate._query_costs(ch, ["gate_cell", "gate_cell_t"])
    assert result == {"window_seconds": 60, "error_type": "Invalid", "error_code": 0}
    assert ch.query_all.await_count == 1


@pytest.mark.parametrize("code", [241, 158, 0, 65536])
async def test_query_cost_failure_emits_only_bounded_numeric_code(code):
    ch = SimpleNamespace(
        query_all=AsyncMock(side_effect=RuntimeError(f"Code: {code}. secret SQL token address"))
    )
    result = await gate._query_costs(ch, ["gate_cell", "gate_cell_t"])
    assert result == {
        "window_seconds": 60,
        "error_type": "RuntimeError",
        "error_code": code if 0 <= code <= 65535 else 0,
    }
    assert "secret" not in json.dumps(result)
    assert ch.query_all.await_count == 1


async def test_query_cost_window_and_fixed_masks_never_select_private_text():
    ch = SimpleNamespace(query_all=AsyncMock(return_value=[]))
    result = await gate._query_costs(ch, ["gate_cell", "gate_cell_t"])
    assert result == {"window_seconds": 60, "rows": []}
    sql, params = ch.query_all.await_args.args
    assert params == {"dbs": ["gate_cell", "gate_cell_t"]}
    assert "INTERVAL 60 SECOND" in sql and "INTERVAL 2 HOUR" not in sql
    assert "query_kind='Select'" in sql and "query_kind='Insert'" in sql
    assert "groupBitOr" in sql and "kind_mask" in sql and "table_mask" in sql
    assert "arrayExists(db -> has(tables, concat(db," in sql
    assert "'.memory_chunks'" in sql and "'.audit_log'" in sql
    assert "ELSE" not in sql
    assert "log_comment" not in sql and "query AS" not in sql
    assert "max_result_rows=20" in sql and "result_overflow_mode='throw'" in sql


async def test_query_cost_observation_preserves_cancellation():
    first = asyncio.CancelledError("first cancellation")
    ch = SimpleNamespace(query_all=AsyncMock(side_effect=first))
    with pytest.raises(asyncio.CancelledError) as caught:
        await gate._query_costs(ch, ["gate_cell", "gate_cell_t"])
    assert caught.value is first
    assert ch.query_all.await_count == 1


async def test_api_cost_sample_precedes_settlement_and_is_reused(monkeypatch):
    from server.core import background, ch_names

    events = []
    costs = {"window_seconds": 60, "rows": []}
    monkeypatch.setattr(ch_names, "database", lambda: "gate_cell")
    monkeypatch.setattr(ch_names, "telemetry_database", lambda: "gate_cell_t")
    monkeypatch.setattr(gate.asyncpg, "connect", AsyncMock(return_value=object()))
    monkeypatch.setattr(gate, "_proof", AsyncMock(return_value={}))
    triggers = SimpleNamespace(tables=(), invariant="fixed", org="fixed", set=AsyncMock())
    monkeypatch.setattr(gate.Triggers, "capture", AsyncMock(return_value=triggers))
    monkeypatch.setattr(gate, "_arm_order", lambda: (True,))

    async def memory_set(*_args, **_kwargs):
        events.append("memory_set")
        return [SimpleNamespace(measured=True, elapsed_ms=12.0)]

    async def query_costs(ch, dbs):
        events.append("query_costs")
        assert dbs == ["gate_cell", "gate_cell_t"]
        return costs

    async def settle(*_args):
        events.append("settlement")

    async def physical(*_args, query_costs):
        assert query_costs is costs
        events.append("physical")
        return {"clickhouse_query_costs": query_costs}

    monkeypatch.setattr(gate, "_memory_set", memory_set)
    monkeypatch.setattr(gate, "_query_costs", query_costs)
    monkeypatch.setattr(gate, "_settle_api", settle)
    monkeypatch.setattr(gate, "_build_block", AsyncMock(return_value=SimpleNamespace(rows=[])))
    monkeypatch.setattr(gate, "_stage", AsyncMock())
    monkeypatch.setattr(gate, "_delivery", AsyncMock(return_value=1.0))
    monkeypatch.setattr(gate, "_verify", AsyncMock())
    monkeypatch.setattr(gate, "_physical", physical)
    monkeypatch.setattr(gate, "_statistics", lambda samples: {})
    monkeypatch.setattr(gate, "_cleanup", AsyncMock())
    monkeypatch.setattr(background, "drain_task_sets", AsyncMock(return_value=True))
    monkeypatch.setattr(background, "flush_registered_buffers", AsyncMock(return_value=True))
    emitted = []

    def emit(kind, **fields):
        events.append(kind)
        emitted.append((kind, fields))

    result = await gate._run(
        argparse.Namespace(pg_dsn="unused"), None, SimpleNamespace(emit=emit), object()
    )
    assert result == {}
    assert events.index("memory_set") < events.index("query_costs")
    assert events.index("query_costs") < events.index("api_costs") < events.index("settlement")
    assert events.index("settlement") < events.index("physical")
    assert events.count("query_costs") == 1
    sample = next(fields for kind, fields in emitted if kind == "api_costs")
    assert sample == {"arm": 0, **costs}


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected real I/O")

    monkeypatch.setattr(gate.asyncpg, "connect", forbidden)
    monkeypatch.setattr(gate.clickhouse_connect, "get_async_client", forbidden)
    monkeypatch.setattr(gate.httpx.AsyncClient, "send", forbidden)


def args(**overrides):
    return argparse.Namespace(
        **{
            "base_url": "http://127.0.0.1:17932",
            "pg_dsn": "postgresql://u:secret@127.0.0.1:15790/gate_cell",
            "token": "secret-token",
            "org": "org",
            "namespace": "ns",
            "launch_manifest": "unused",
            **overrides,
        }
    )


def role_config():
    from server.settings import Settings

    config = SimpleNamespace(**Settings.model_construct().model_dump(mode="json"))
    vars(config).update(
        cell_router_token="",
        router_operator_token="",
        pg_dsn=args().pg_dsn,
        ch_host="127.0.0.1",
        ch_port=18790,
        ch_database="gate_cell",
        ch_telemetry_database="gate_cell_t",
        cell="gate-cell",
        background_workers="off",
        storage_authority="postgres",
        embedding_provider="fake",
        embedding_model="fake-ngram-16",
        embedding_dim=16,
        embedding_base_url="",
        embedding_send_dimensions=False,
        llm_provider="fake",
        llm_model="fake",
        jwt_secret="test-only-root",
        deployment=False,
        instance_heartbeat_seconds=10,
        build_commit="a" * 40,
        pg_delivery_batch_rows=500,
    )

    def dump(**kwargs):
        return {
            k: v
            for k, v in vars(config).items()
            if k != "model_dump" and ("include" not in kwargs or k in kwargs["include"])
        }

    config.model_dump = dump
    return config


def fixture_config():
    config = role_config()
    manifest = gate.Fixture(
        schema=2,
        base_url=args().base_url,
        pg_host="127.0.0.1",
        pg_port=15790,
        pg_database="gate_cell",
        ch_host=config.ch_host,
        ch_port=config.ch_port,
        ch_database=config.ch_database,
        ch_telemetry_database=config.ch_telemetry_database,
        cell=config.cell,
        org="org",
        namespace="ns",
        deployment_id="deployment",
        epoch=1,
        instance_id="instance",
        build_commit=config.build_commit,
        source_tree="b" * 40,
        launch_manifest_sha256="c" * 64,
        ownership_receipt_sha256="d" * 64,
        api_configuration_sha256=gate._digest(
            {**config.model_dump(), "cell_router_token": "lcr_" + "0" * 128}
        ),
        cli_configuration_sha256=gate._digest(config.model_dump()),
        shared_configuration_sha256=gate._digest(
            {k: v for k, v in config.model_dump().items() if k != "cell_router_token"}
        ),
        background_workers="off",
        controller_excluded=True,
        pg_internal_port=5432,
    )
    return manifest, config


def test_gate_keeps_the_original_api_sample_contract():
    assert (gate.CALLS, gate.WARMUP, gate.ROUNDS, gate.BATCHES) == (2000, 50, 5, 5)
    assert not hasattr(gate, "_batch") and not hasattr(gate, "_clean")


@pytest.mark.parametrize(("batch", "expected"), [(500, 45500), (1590, 100000)])
def test_complete_fixture_count_uses_the_unchanged_delivery_batch(batch, expected):
    assert gate._planned_keys(batch) == expected


@pytest.mark.parametrize("batch", [1591, 0, -1, 1.5, True])
def test_invalid_batch_refuses_before_loading(batch):
    with pytest.raises(ValueError):
        gate._planned_keys(batch)


def test_arm_order_alternates_pair_order_and_has_five_of_each():
    order = gate._arm_order()
    assert order == (True, False, False, True, True, False, False, True, True, False)
    assert order.count(True) == order.count(False) == 5


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1234",
        "http://example.com:1234",
        "http://10.0.0.1:1234",
        "http://127.0.0.1",
        "http://127.0.0.1:1234/other",
        "http://u:pw@127.0.0.1:1234",
        "http://127.0.0.1:1234?x=1",
        "http://127.0.0.1:1234#fragment",
    ],
)
def test_http_endpoint_refuses_ambiguous_or_remote_targets(url):
    with pytest.raises(gate.Invalid):
        gate._endpoint(url)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u@localhost:15790/scratch",
        "postgresql://u@127.0.0.1:15790/loom",
        "postgresql://u@127.0.0.1:15790/scratch?host=remote",
        "postgresql:///scratch?service=other",
        "postgresql://u@127.0.0.1,127.0.0.2:15790/scratch",
        "postgresql://u@127.0.0.1:15790/scratch?options=-cdefault_transaction_read_only=off",
    ],
)
def test_pg_endpoint_refuses_overrides_defaults_and_alternate_targets(dsn):
    with pytest.raises(gate.Invalid):
        gate._endpoint(dsn, pg=True)


def test_manifest_binds_published_port_without_conflating_internal_port():
    manifest, config = fixture_config()
    assert manifest.pg_port == 15790 and manifest.pg_internal_port == 5432
    assert manifest.validate(args(), config) == 45500
    with pytest.raises(gate.Invalid, match="manifest PG endpoint"):
        dataclasses.replace(manifest, pg_port=5432).validate(args(), config)


@pytest.mark.parametrize(
    "change",
    [
        {"ch_host": "10.0.0.1"},
        {"ch_database": "loom"},
        {"cell": "other"},
        {"background_workers": "on"},
        {"llm_provider": "openai"},
        {"embedding_provider": "local"},
        {"pg_delivery_batch_rows": 1591},
    ],
)
def test_configuration_drift_and_unsafe_settings_refuse(change):
    manifest, config = fixture_config()
    for k, v in change.items():
        setattr(config, k, v)
    with pytest.raises(gate.Invalid):
        manifest.validate(args(), config)


def test_artifact_never_overwrites_existing_file_or_symlink(tmp_path):
    original = tmp_path / "old.jsonl"
    original.write_text("preserve")
    with pytest.raises(FileExistsError):
        gate.Artifact(str(original))
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(FileExistsError):
        gate.Artifact(str(link))
    assert original.read_text() == "preserve"
    fresh = tmp_path / "fresh"
    artifact = gate.Artifact(str(fresh))
    artifact.emit("test", n=1)
    artifact.close()
    assert fresh.stat().st_mode & 0o777 == 0o600
    assert json.loads(fresh.read_text()) == {"kind": "test", "n": 1}


@pytest.mark.asyncio
async def test_actual_api_loop_sends_unique_four_kib_values_and_full_sample(monkeypatch):
    sent, outcomes = [], []

    class Client:
        def __init__(self, **kw):
            assert kw["follow_redirects"] is False and kw["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, json):
            sent.append(json)
            return httpx.Response(
                200,
                json={
                    "memory_id": str(uuid.uuid4()),
                    "version": len(sent),
                    "admission": "ready",
                    "status": "active",
                    "content_sha256": gate.sha256_value(json["value"]),
                    "memory_key": json["memory_key"],
                    "namespace": json["namespace"],
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(gate.httpx, "AsyncClient", Client)
    rows = await gate._memory_set(
        args(), emit=lambda kind, **fields: outcomes.append((kind, fields))
    )
    assert len(sent) == len(rows) == len(outcomes) == 2050
    assert (
        len({body["memory_key"] for body in sent}) == len({body["value"] for body in sent}) == 2050
    )
    assert all(len(body["value"].encode("utf-8")) == 4096 for body in sent)
    assert sum(row.measured for row in rows) == 2000
    assert all("value" not in fields and "token" not in fields for _, fields in outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,changes",
    [
        (302, {}),
        (503, {}),
        (200, {"status": "duplicate"}),
        (200, {"noop": True}),
        (200, {"content_sha256": "wrong"}),
        (200, {"version": 0}),
    ],
)
async def test_api_refusal_is_recorded_once_and_never_enters_samples(monkeypatch, status, changes):
    events = []

    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, json):
            body = {
                "memory_id": str(uuid.uuid4()),
                "version": 1,
                "status": "active",
                "admission": "ready",
                "memory_key": json["memory_key"],
                "namespace": json["namespace"],
                "content_sha256": gate.sha256_value(json["value"]),
                **changes,
            }
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(gate.httpx, "AsyncClient", Client)
    with pytest.raises(gate.Invalid):
        await gate._memory_set(args(), emit=lambda *a, **kw: events.append((a, kw)))
    assert len(events) == 1 and events[0][0] == ("request_error",)
    assert "secret-token" not in str(events) and "value" not in events[0][1]


def samples(on_p99, off_p99, on_rps=90.1, off_rps=100):
    on, off = iter(on_p99), iter(off_p99)
    return [
        gate.Samples(arm, [next(on) if arm else next(off)] * gate.CALLS, on_rps if arm else off_rps)
        for arm in gate._arm_order()
    ]


def test_statistics_use_difference_of_arm_medians_and_throughput_not_time():
    result = gate._statistics(samples([2, 12, 22, 32, 102], [1, 6, 51, 61, 71]))
    assert result["p99_delta_ms"] == -29
    assert result["paired_p99_differences_ms"] == [1, 6, -29, -29, 31]
    assert result["latency_within_limit"] is True
    # Time increases 10.99%, but the original rows/second gate passes at 9.9% loss.
    assert result["throughput_loss"] == pytest.approx(0.099)
    assert result["throughput_within_limit"] is True
    failed = gate._statistics(samples([20] * 5, [1] * 5, 89, 100))
    assert failed["latency_within_limit"] is failed["throughput_within_limit"] is False
    assert gate._quantile(list(range(2000)), 0.99) == 1979


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_throughput_cannot_become_passing_number(value):
    with pytest.raises(gate.Invalid):
        gate._statistics(samples([1] * 5, [1] * 5, value))


@pytest.mark.asyncio
async def test_actual_production_worker_body_runs_five_times_with_unchanged_batch(monkeypatch):
    from server.core import idempotency, pg
    from server.llm import customer
    from server.memory.app import pg_delivery as publish
    from server.memory.store import pg_memory
    from server.memory.workers import pg_delivery as worker
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_delivery_batch_rows", 2)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(worker, "_read_backlog", AsyncMock())
    claim = AsyncMock(return_value=[object(), object()])
    monkeypatch.setattr(pg_memory, "claim_due", claim)
    attribution = []

    async def publish_claims(claims):
        attribution.append(customer._attribution.get())
        assert len(claims) == 2
        return 2, 0

    monkeypatch.setattr(publish, "publish_claims", publish_claims)
    for module, name in (
        (idempotency, "sweep_expired"),
        (pg_memory, "sweep_erasure_evidence"),
        (pg_memory, "sweep_settled"),
        (publish, "publish_audit_outbox"),
    ):
        monkeypatch.setattr(module, name, AsyncMock(return_value=0))
    times = iter([0, 2_000_000_000])
    monkeypatch.setattr(gate.time, "perf_counter_ns", lambda: next(times))
    events = []
    assert (
        await gate._delivery(args(), 2, emit=lambda kind, **kw: events.append((kind, kw)), arm=0)
        == 5
    )
    assert claim.await_count == 5 and all(call.args == (2,) for call in claim.await_args_list)
    assert len(attribution) == 5
    assert all(
        a.org == "org" and a.namespace == "ns" and a.subject == gate.SUBJECT for a in attribution
    )
    assert [kind for kind, _ in events] == ["worker"] * 5 + ["delivery_block"]
    assert customer._attribution.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"claimed": 1, "published": 1},
        {"claimed": 2, "published": 1},
        {"claimed": 2, "published": 2, "failed": 1},
    ],
)
async def test_worker_count_failure_invalidates_block_without_retries(monkeypatch, result):
    from server.memory.workers import pg_delivery as worker
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_delivery_batch_rows", 2)
    run = AsyncMock(return_value=worker.DeliveryResult(**result))
    monkeypatch.setattr(worker, "run_pass", run)
    with pytest.raises(gate.Invalid):
        await gate._delivery(args(), 2, emit=lambda *_a, **_kw: None, arm=0)
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_canonical_full_rows_audits_and_expected_vectors_use_real_fake_embedder(monkeypatch):
    import server.embedding
    from server.embedding.fake import FakeNGramEmbedder
    from server.llm import customer
    from server.memory.store import pg_memory
    from server.memory.store.rows import MEMORY_PERSISTED_COLS

    embedder = FakeNGramEmbedder(dim=16)
    monkeypatch.setattr(server.embedding, "get_embedder", lambda: embedder)
    # Keep actual require(): refresh its real switch read through a fake store edge.
    import logging

    from server.core.polled_copy import PolledCopy

    monkeypatch.setattr(
        customer,
        "_switch",
        PolledCopy("switch", AsyncMock(return_value=frozenset()), logging.getLogger(__name__)),
    )
    block = await gate._build_block(args(), 1)
    assert len(block.rows) == len(block.audits) == len(block.documents) == 5
    for row, audit, doc in zip(block.rows, block.audits, block.documents, strict=True):
        assert set(MEMORY_PERSISTED_COLS) <= row.keys()
        assert len(json.loads(row["value"]).encode()) == 4096
        assert row["actor"] == gate.SUBJECT and row["source_class"] == "asserted"
        assert row["content_sha256"] == gate.sha256_value(json.loads(row["value"]))
        assert audit["event_type"] == "memory.revision" and len(audit["row_hash"]) == 64
        assert row["value"] not in audit["subject"]
        assert len(doc["embedding"]) == doc["embedding_dim"] == 16
        assert doc["content_sha256"] == row["content_sha256"]
        assert pg_memory.decode_row(pg_memory.encode_row(row)) == row
    assert customer._attribution.get() is None


@pytest.mark.asyncio
async def test_stage_calls_real_commit_interface_with_due_fresh_targets(monkeypatch):
    from server.memory.store import pg_memory

    block = gate.Block(
        [{"memory_id": str(uuid.uuid4()), "org": "org", "namespace": "ns", "memory_key": "key"}],
        [{"row_hash": "hash"}],
        [],
    )
    debt = AsyncMock()
    absent = AsyncMock()
    monkeypatch.setattr(gate, "_debt", debt)
    monkeypatch.setattr(gate, "_absent", absent)
    committed = pg_memory.Committed(
        rows=block.rows, audits=block.audits, seqs=[("org", "ns", 1)], targets=[7]
    )
    commit = AsyncMock(return_value=committed)
    monkeypatch.setattr(pg_memory, "commit_versions", commit)
    conn = SimpleNamespace(fetchval=AsyncMock(side_effect=[0, 1]))
    await gate._stage(conn, object(), block)
    assert commit.await_args.kwargs == {"targets": 7, "publisher_lease": False}
    assert block.seqs == [("org", "ns", 1)]
    assert [call.args[1] for call in debt.await_args_list] == [0, 1]
    absent.assert_awaited_once()


@pytest.mark.parametrize(
    ("field", "expected", "actual"),
    [
        ("value", "right", "wrong"),
        ("version", 2, 1),
        ("content_sha256", "a", "b"),
        ("embedding", [1.0, 0.0], [0.0, 1.0]),
        ("embedding", [1.0], []),
        ("row_hash", "good", "bad"),
    ],
)
def test_stored_payload_vector_version_and_audit_mismatch_refuse(field, expected, actual):
    with pytest.raises(gate.Invalid):
        gate._compare({field: expected}, {field: actual}, [field], "stored")


def stored_bfloat16(vector):
    """Exercise the installed insert writer and query reader without a server."""
    import array

    from clickhouse_connect.datatypes.base import TypeDef
    from clickhouse_connect.datatypes.numeric import BFloat16

    class Encoded:
        def __init__(self, raw):
            self.raw = raw

        def read_array(self, kind, count):
            result = array.array(kind)
            result.frombytes(self.raw)
            assert len(result) == count
            return result

    kind = BFloat16(TypeDef())
    raw = bytearray()
    kind._write_column_binary(vector, raw, SimpleNamespace(column_name="embedding"))
    return kind._read_column_binary(
        Encoded(raw), len(vector), SimpleNamespace(use_numpy=False), None
    )


@pytest.mark.parametrize("field", ["embedding", "memory_key_embedding"])
def test_storage_vector_matches_installed_bfloat16_and_rejects_corrupted_neighbor(field):
    from server.embedding.fake import FakeNGramEmbedder

    vector = FakeNGramEmbedder(dim=1024)._embed_raw(gate._payload("gate-review-owned-1"))
    stored = stored_bfloat16(vector)
    assert sum(a != b for a, b in zip(vector, stored, strict=True)) > 0
    gate._compare({field: vector}, {field: stored}, [field], "stored")
    import struct

    corrupted = list(stored)
    index = next(i for i, value in enumerate(stored) if value > 0)
    bits = struct.unpack("<I", struct.pack("<f", stored[index]))[0]
    corrupted[index] = struct.unpack("<f", struct.pack("<I", bits + (1 << 16)))[0]
    for invalid in (corrupted, stored[:-1], [float("nan")] * 1024, [float("inf")] * 1024):
        with pytest.raises(gate.Invalid):
            gate._compare({field: vector}, {field: invalid}, [field], "stored")
    with pytest.raises(gate.Invalid):
        gate._compare({field: [float("nan")]}, {field: [float("nan")]}, [field], "stored")


class TriggerConn:
    def __init__(self):
        self.on = True
        self.drift = False
        self.statements = []

    def transaction(self):
        return SimpleNamespace(start=AsyncMock(), rollback=AsyncMock(), commit=AsyncMock())

    async def execute(self, sql):
        self.statements.append(sql)
        if sql.startswith("DROP TRIGGER"):
            self.on = False
        elif sql.startswith("CREATE TRIGGER"):
            self.on = True


@pytest.mark.asyncio
async def test_captured_trigger_state_allows_off_to_off_and_restores_exact_tables(monkeypatch):
    conn = TriggerConn()

    async def snapshot(c):
        return (
            ("memory_current", "tenancy"),
            "drift" if c.drift else "invariant",
            "org" if c.on else gate._digest([]),
        )

    monkeypatch.setattr(gate.Triggers, "snapshot", staticmethod(snapshot))
    monkeypatch.setattr(gate.cell_triggers, "missing", AsyncMock(return_value=[]))
    triggers = await gate.Triggers.capture(conn)
    await triggers.set(conn, False)
    await triggers.set(conn, False)
    assert triggers.on is False
    await triggers.restore(conn)
    assert triggers.on is True
    assert conn.statements and all('ON loom."memory_current"' in sql for sql in conn.statements)
    conn.drift = True
    with pytest.raises(gate.Invalid, match="drift"):
        await triggers.set(conn, False)


class ProofPG:
    def __init__(self, fault=""):
        self.fault = fault
        self.closed = False
        self.sql = []

    async def fetchrow(self, sql, *params):
        self.sql.append(sql)
        if "current_database()" in sql:
            return {
                "db": "wrong" if self.fault == "database" else "gate_cell",
                "port": 5432,
                "recovery": self.fault == "recovery",
            }
        if "FROM loom.authority" in sql:
            return {
                "authority": "postgres",
                "deployment_id": "wrong" if self.fault == "pair" else "deployment",
                "epoch": 1,
            }
        if "FROM loom.cell_first" in sql:
            return None if self.fault == "partial" else {"cell": "gate-cell", "is_first": False}
        if "FROM loom.cell_self" in sql:
            return {
                "current_fp": json.dumps({"v": 1, "fields": {}}),
                "next_fp": {} if self.fault == "transition" else None,
            }
        if "AS pending" in sql:
            return {"pending": int(self.fault == "debt"), "audits": 0, "errors": 0}
        raise AssertionError(sql)

    async def fetchval(self, sql, *params):
        self.sql.append(sql)
        if "FROM loom.tenancy_mode" in sql:
            return "recording" if self.fault == "recording" else "enforced"
        if "FROM loom.tenancy WHERE" in sql:
            return self.fault != "held"
        raise AssertionError(sql)

    async def close(self, **kwargs):
        self.closed = True


class ProofCH:
    def __init__(self, fault=""):
        self.fault = fault
        self.closed = False
        self.sql = []

    async def query_all(self, sql, parameters=None):
        self.sql.append(sql)
        if "currentDatabase()" in sql:
            return [{"db": "wrong" if self.fault == "ch_database" else "gate_cell"}]
        if "FROM loom.storage_authority" in sql:
            return [
                {
                    "authority": "postgres",
                    "deployment_id": "wrong" if self.fault == "ch_pair" else "deployment",
                    "epoch": 1,
                }
            ]
        if "FROM loom.namespaces" in sql:
            return [{"default_conflict_policy": "append_only"}]
        if "FROM loom.provider_configuration" in sql:
            return [{"n": int(self.fault == "saved_provider")}]
        if "FROM loom.runtime_overrides" in sql:
            return [{"n": int(self.fault == "overrides")}]
        if "FROM loom.cell_heartbeats" in sql:
            return [
                {
                    "cell": "gate-cell",
                    "tenancy_mode": "enforced",
                    "fingerprint": json.dumps(
                        {"serves": True, "marked": self.fault != "fingerprint"}
                    ),
                }
            ]
        raise AssertionError(sql)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "database",
        "recovery",
        "pair",
        "partial",
        "recording",
        "held",
        "transition",
        "debt",
        "ch_database",
        "ch_pair",
        "saved_provider",
        "overrides",
        "fingerprint",
        "health",
        "competing_worker",
    ],
)
async def test_read_only_preflight_faults_refuse_before_native_setup(monkeypatch, fault):
    import server.settings
    from server.cli import _ch
    from server.core import cell, instances

    fixture, config = fixture_config()
    pg, ch = ProofPG(fault), ProofCH(fault)
    connections = []

    async def connect(dsn, **kwargs):
        connections.append(kwargs)
        assert kwargs["server_settings"] == {"default_transaction_read_only": "on"}
        return pg

    async def close(conn):
        await conn.close()

    async def live(*_args, **_kw):
        return (
            []
            if fault == "competing_worker"
            else [
                {
                    "instance_id": "instance",
                    "build_commit": "a" * 40,
                    "workers_lease": "",
                    "serving": 1,
                    "host": socket.gethostname(),
                    **fingerprints,
                }
            ]
        )

    async def health(*_args):
        if fault == "health":
            raise gate.Invalid("health source")

    native = AsyncMock(side_effect=AssertionError("native setup before proof"))
    monkeypatch.setattr(server.settings, "settings", config)
    monkeypatch.setattr(gate.asyncpg, "connect", connect)
    monkeypatch.setattr(gate.ReadOnlyCH, "open", AsyncMock(return_value=ch))
    monkeypatch.setattr(gate, "_close_pg", close)
    monkeypatch.setattr(gate, "_health", health)
    monkeypatch.setattr(gate, "_source", lambda _: {"clean": True})
    from server.core import deployment_config

    fingerprints = deployment_config.fingerprints(config)
    monkeypatch.setattr(gate, "_launch", lambda *_: {"fingerprints": fingerprints})
    monkeypatch.setattr(instances, "read_live", live)
    monkeypatch.setattr(cell, "mine", lambda: {})
    monkeypatch.setattr(cell, "matches", lambda mine, value: value is not None)
    monkeypatch.setattr(_ch, "run_connected", native)
    with pytest.raises(gate.Invalid):
        await gate._preflight(args(), fixture)
    assert pg.closed and ch.closed
    native.assert_not_called()
    assert connections and all(sql.startswith("SELECT ") for sql in pg.sql + ch.sql)
    assert not any("model_transitions" in sql for sql in ch.sql)


@pytest.mark.asyncio
async def test_readonly_ch_startup_restricts_connection_before_first_query(monkeypatch):
    client = SimpleNamespace(close=AsyncMock())
    opened = AsyncMock(return_value=client)
    monkeypatch.setattr(gate.clickhouse_connect, "get_async_client", opened)
    ch = await gate.ReadOnlyCH.open()
    assert opened.await_args.kwargs["settings"]["readonly"] == 1
    await ch.close()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_never_follows_redirects(monkeypatch):
    fixture, _ = fixture_config()

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url):
            return httpx.Response(
                302,
                headers={"Location": "http://external.invalid"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(gate.httpx, "AsyncClient", Client)
    with pytest.raises(gate.Invalid, match="health HTTP status"):
        await gate._health(args(), fixture)


@pytest.mark.asyncio
async def test_restore_preserves_primary_failure_and_survives_second_cancellation(monkeypatch):
    from server.settings import settings

    fixture, _ = fixture_config()
    conn = ProofPG()
    restoring, release = asyncio.Event(), asyncio.Event()
    failure = ValueError("primary-secret")
    events = []

    class T:
        tables = ("memory_current",)
        invariant = org = "digest"

        async def set(self, *_):
            pass

    async def restore(*_):
        restoring.set()
        await release.wait()

    async def close(c, *_):
        c.closed = True

    monkeypatch.setattr(gate.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(gate.Triggers, "capture", AsyncMock(return_value=T()))
    monkeypatch.setattr(gate, "_proof", AsyncMock(return_value={}))
    monkeypatch.setattr(gate, "_memory_set", AsyncMock(side_effect=failure))
    monkeypatch.setattr(gate, "_restore", restore)
    monkeypatch.setattr(gate, "_close_pg", close)
    artifact = SimpleNamespace(emit=lambda kind, **kw: events.append(kind))
    task = asyncio.create_task(gate._run(args(), fixture, artifact, object()))
    try:
        await asyncio.wait_for(restoring.wait(), timeout=0.5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(ValueError) as raised:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if not done:
                raise TimeoutError("owned test task did not finish")
            await task
        assert raised.value is failure and conn.closed
        assert "triggers_restored" in events
        assert settings.pg_delivery_batch_rows > 0
    except TimeoutError:
        if task.done():
            task.result()
        raise
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_final_verdict_follows_native_cleanup_and_artifact_close(
    monkeypatch, tmp_path, capsys, cleanup_failure
):
    import sys

    import server.settings
    import server.wiring
    from server.cli import _ch

    fixture, config = fixture_config()
    manifest = tmp_path / "manifest"
    manifest.write_text(json.dumps(dataclasses.asdict(fixture)))
    output = tmp_path / "result.jsonl"
    arguments = args()
    argv = ["driver"]
    for name, value in {
        **vars(arguments),
        "fixture_manifest": str(manifest),
        "output": str(output),
    }.items():
        argv += ["--" + name.replace("_", "-"), str(value)]
    order = []
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server.settings, "settings", config)
    monkeypatch.setattr(server.wiring, "wire", lambda: order.append("wire"))
    monkeypatch.setattr(gate, "_preflight", AsyncMock())
    monkeypatch.setattr(gate, "_source", lambda _: {"clean": True})
    monkeypatch.setattr(gate, "_launch", lambda *_: {})

    def native(*_a, **_kw):
        order.append("native_cleanup")
        return {
            "latency_within_limit": True,
            "throughput_within_limit": True,
            "p99_delta_ms": 0.0,
            "throughput_loss": 0.0,
        }

    monkeypatch.setattr(_ch, "run_connected", native)

    def sync(_fd):
        assert capsys.readouterr().out == ""
        assert order == ["wire", "native_cleanup"]
        if cleanup_failure:
            raise OSError("secret-token")

    monkeypatch.setattr(gate.os, "fsync", sync)
    assert gate.main() == int(cleanup_failure)
    final = capsys.readouterr().out
    assert "secret-token" not in final
    assert json.loads(final)["verdict"] == ("INVALID" if cleanup_failure else "PASS")
    assert '"verdict": "PASS"' not in output.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("sparse_storage", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "",
        "projection_value",
        "projection_version",
        "vector",
        "audit_hash",
        "audit_subject",
        "missing_document",
        "targets",
        "attempts",
        "head",
    ],
)
async def test_composed_stored_verification_compares_authority_projection_vectors_and_audits(
    monkeypatch, fault, sparse_storage
):
    import logging

    import server.embedding
    from server.core.polled_copy import PolledCopy
    from server.embedding.fake import FakeNGramEmbedder
    from server.llm import customer
    from server.memory.store import pg_memory

    monkeypatch.setattr(
        customer,
        "_switch",
        PolledCopy("switch", AsyncMock(return_value=frozenset()), logging.getLogger(__name__)),
    )
    monkeypatch.setattr(server.embedding, "get_embedder", lambda: FakeNGramEmbedder(dim=16))
    block = await gate._build_block(args(), 1)
    block.seqs = [("org", "ns", i + 1) for i in range(5)]
    stored = [pg_memory.encode_row(row) for row in block.rows]
    if sparse_storage:
        stored = [pg_memory.sparse_row(row) for row in stored]
    records = [
        {
            "head": json.dumps(stored[i]),
            "revision": json.dumps(stored[i]),
            "audit": json.dumps(pg_memory.encode_audit(audit)),
            "seq": i + 1,
            "targets": 1 if fault == "targets" else 0,
            "attempts": 1 if fault == "attempts" else 0,
            "last_error": "",
        }
        for i, (_row, audit) in enumerate(zip(block.rows, block.audits, strict=True))
    ]
    projections = [dict(row) for row in block.rows]
    documents = [dict(row) for row in block.documents]
    for document in documents:
        for field in ("embedding", "memory_key_embedding"):
            document[field] = stored_bfloat16(document[field])
    audits = [dict(row) for row in block.audits]
    if fault == "projection_value":
        projections[0]["value"] = '"wrong"'
    if fault == "projection_version":
        projections[0]["version"] -= 1
    if fault == "vector":
        documents[0]["embedding"] = [0.0] * 16
    if fault == "audit_hash":
        audits[0]["row_hash"] = "bad"
    if fault == "audit_subject":
        audits[0]["subject"] = "wrong"
    if fault == "missing_document":
        documents.pop()
    if fault == "head":
        records.pop()
    conn = SimpleNamespace(
        fetch=AsyncMock(return_value=records),
        fetchrow=AsyncMock(return_value={"pending": 0, "audits": 0, "errors": 0}),
    )

    async def query(sql, params):
        if "FROM loom.memory FINAL" in sql:
            return projections
        if "FROM loom.memory_embeddings FINAL" in sql:
            return documents
        if "FROM loom.audit_log" in sql:
            return audits
        raise AssertionError(sql)

    ch = SimpleNamespace(query_all=query)
    if fault:
        with pytest.raises(gate.Invalid):
            await gate._verify(conn, ch, args(), block)
    else:
        await gate._verify(conn, ch, args(), block)
    assert all(
        "org=" in call.args[0] and "namespace=" in call.args[0]
        for call in conn.fetch.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sparse_storage", [False, True])
@pytest.mark.parametrize(
    "fault", ["", "memory_key", "value", "version", "content_sha256", "status"]
)
async def test_api_authority_failure_names_only_the_failed_field(
    monkeypatch, fault, sparse_storage
):
    import logging

    import server.embedding
    from server.core.polled_copy import PolledCopy
    from server.embedding.fake import FakeNGramEmbedder
    from server.llm import customer
    from server.memory.store import pg_memory

    monkeypatch.setattr(
        customer,
        "_switch",
        PolledCopy("switch", AsyncMock(return_value=frozenset()), logging.getLogger(__name__)),
    )
    monkeypatch.setattr(server.embedding, "get_embedder", lambda: FakeNGramEmbedder(dim=16))
    block = await gate._build_block(args(), 1)
    row, audit = block.rows[0], block.audits[0]
    expected = gate.RequestRow(
        str(row["memory_key"]),
        str(row["memory_id"]),
        row["version"],
        row["content_sha256"],
        1.0,
        True,
        "derived_ready",
    )
    head = dict(row)
    if fault:
        head[fault] = {
            "memory_key": "changed-key",
            "value": json.dumps("changed-value"),
            "version": row["version"] + 1,
            "content_sha256": "0" * 64,
            "status": "conflicted",
        }[fault]
    encoded = pg_memory.encode_row(head)
    stored = json.dumps(pg_memory.sparse_row(encoded) if sparse_storage else encoded)
    conn = SimpleNamespace(
        fetch=AsyncMock(
            return_value=[
                {
                    "head": stored,
                    "revision": stored,
                    "audit": json.dumps(pg_memory.encode_audit(audit)),
                    "seq": 1,
                    "targets": 0,
                    "attempts": 0,
                    "last_error": "",
                }
            ]
        ),
        fetchrow=AsyncMock(return_value={"pending": 0, "audits": 0, "errors": 0}),
    )
    document = await gate._expected_document(args(), row)
    for field in ("embedding", "memory_key_embedding"):
        document[field] = stored_bfloat16(document[field])

    async def query(sql, params):
        if "FROM loom.memory FINAL" in sql:
            return [row]
        if "FROM loom.memory_embeddings FINAL" in sql:
            return [document]
        if "FROM loom.audit_log" in sql:
            return [audit]
        raise AssertionError(sql)

    ch = SimpleNamespace(query_all=query)
    if fault:
        with pytest.raises(gate.Invalid) as raised:
            await gate._verify(conn, ch, args(), [expected])
        assert str(raised.value) == "authority API " + fault
    else:
        await gate._verify(conn, ch, args(), [expected])


@pytest.mark.asyncio
async def test_real_customer_policy_can_refuse_expected_vectors_without_bypass(monkeypatch):
    import logging

    import server.embedding
    from server.core.polled_copy import PolledCopy
    from server.embedding.fake import FakeNGramEmbedder
    from server.llm import customer

    monkeypatch.setattr(
        customer,
        "_switch",
        PolledCopy(
            "switch", AsyncMock(return_value=frozenset({"org"})), logging.getLogger(__name__)
        ),
    )
    monkeypatch.setattr(server.embedding, "get_embedder", lambda: FakeNGramEmbedder(dim=16))
    with pytest.raises(customer.ModelCallsDisabled):
        await gate._build_block(args(), 1)
    assert customer._attribution.get() is None


@pytest.mark.asyncio
async def test_partial_trigger_toggle_is_restored_on_original_failure(monkeypatch):
    conn = TriggerConn()
    original = ValueError("DDL failed")
    writes = 0

    async def execute(sql):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise original
        conn.on = sql.startswith("CREATE TRIGGER")

    async def snapshot(c):
        return ("memory_current", "tenancy"), "invariant", "org" if c.on else gate._digest([])

    monkeypatch.setattr(conn, "execute", execute)
    monkeypatch.setattr(gate.Triggers, "snapshot", staticmethod(snapshot))
    monkeypatch.setattr(gate.cell_triggers, "missing", AsyncMock(return_value=[]))
    triggers = await gate.Triggers.capture(conn)
    with pytest.raises(ValueError) as raised:
        try:
            await triggers.set(conn, False)
        finally:
            await triggers.restore(conn)
    assert raised.value is original and conn.on and triggers.on


def test_manifest_unknown_fields_and_credentials_refuse(tmp_path):
    fixture, _ = fixture_config()
    path = tmp_path / "manifest"
    path.write_text(json.dumps({**dataclasses.asdict(fixture), "password": "secret"}))
    with pytest.raises(gate.Invalid, match="fixture manifest"):
        gate.Fixture.read(str(path))


@pytest.mark.parametrize("fault", ["", "head", "tree", "dirty", "refused"])
def test_source_provenance_uses_only_fixed_bounded_readonly_git(monkeypatch, fault):
    fixture, _ = fixture_config()
    calls = []

    def git(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "git" and kwargs["timeout"] == 5 and kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["cwd"] == Path(__file__).resolve().parents[2]
        assert "shell" not in kwargs
        if argv[1:] == ["rev-parse", "HEAD"]:
            value = "wrong" if fault == "head" else fixture.build_commit
        elif argv[1:] == ["rev-parse", "HEAD^{tree}"]:
            value = "wrong" if fault == "tree" else fixture.source_tree
        else:
            assert argv[1:] == ["status", "--porcelain"]
            value = " M file" if fault == "dirty" else ""
        return SimpleNamespace(returncode=int(fault == "refused"), stdout=value)

    monkeypatch.setattr(gate.subprocess, "run", git)
    if fault:
        with pytest.raises(gate.Invalid):
            gate._source(fixture)
    else:
        assert gate._source(fixture) == {
            "head": fixture.build_commit,
            "tree": fixture.source_tree,
            "clean": True,
        }
        assert len(calls) == 3


@pytest.mark.asyncio
async def test_restoration_has_finite_deadline_and_closes_owned_connection(monkeypatch):
    conn = ProofPG()

    class Stuck:
        async def restore(self, _):
            await asyncio.Event().wait()

    monkeypatch.setattr(gate, "RESTORE_S", 0.01)

    async def close(owned, *_):
        assert owned is conn
        await conn.close()

    monkeypatch.setattr(gate, "_close_pg", close)
    artifact = SimpleNamespace(emit=lambda *_a, **_kw: None)
    with pytest.raises(TimeoutError):
        await gate._cleanup(Stuck(), conn, artifact)
    assert conn.closed


@pytest.mark.asyncio
async def test_review_delivery_timestamps_pass_real_clickhouse_serialization(monkeypatch):
    import logging

    from clickhouse_connect.datatypes.registry import get_from_name
    from clickhouse_connect.driver.insert import InsertContext

    import server.embedding
    from server.core.ch import _insert_data
    from server.core.polled_copy import PolledCopy
    from server.embedding.fake import FakeNGramEmbedder
    from server.llm import customer

    monkeypatch.setattr(
        customer,
        "_switch",
        PolledCopy("switch", AsyncMock(return_value=frozenset()), logging.getLogger(__name__)),
    )
    monkeypatch.setattr(server.embedding, "get_embedder", lambda: FakeNGramEmbedder(dim=16))
    block = await gate._build_block(args(), 1)
    for col in ("created_at", "updated_at", "last_reinforced_at"):
        actual_data = _insert_data(block.rows, [col], ["DateTime"])
        kind = get_from_name("DateTime")
        context = InsertContext("loom.memory", [col], [kind])
        context.start_column(col)
        serialized = bytearray()
        kind.write_column([row[0] for row in actual_data], serialized, context)
        assert len(serialized) == 4 * len(block.rows)
    assert all(
        row["created_at"] == row["updated_at"] == row["last_reinforced_at"] for row in block.rows
    )


@pytest.mark.asyncio
async def test_review_trailing_slash_is_normalized_for_actual_health_and_api_routes(monkeypatch):
    from starlette.responses import JSONResponse
    from starlette.routing import Route, Router

    fixture, _ = fixture_config()
    paths = []

    async def health(request):
        paths.append(request.url.path)
        return JSONResponse({})

    app = Router(
        routes=[
            Route("/v1/health", health),
            Route("/v1/health/deployment", health),
            Route("/v1/memory.set", health, methods=["POST"]),
        ]
    )
    transport = httpx.ASGITransport(app=app)

    async def send(self, request, **kwargs):
        response = await transport.handle_async_request(request)
        await response.aread()
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with pytest.raises(gate.Invalid):
        await gate._health(args(base_url=fixture.base_url + "/"), fixture)
    with pytest.raises(gate.Invalid):
        await gate._memory_set(args(base_url=fixture.base_url + "/"))
    assert paths == ["/v1/health", "/v1/health/deployment", "/v1/memory.set"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(302, ""), (503, "PUBLICATION_PENDING")])
async def test_review_failed_request_keeps_raw_status_elapsed_and_classification(
    monkeypatch, status, code
):
    async def send(self, request, **kwargs):
        return httpx.Response(
            status, json={"detail": {"code": code, "message": "secret-token"}}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    clock = iter([10_000_000, 17_000_000])
    monkeypatch.setattr(gate.time, "perf_counter_ns", lambda: next(clock))
    events = []
    with pytest.raises(gate.Invalid):
        await gate._memory_set(args(), emit=lambda kind, **record: events.append((kind, record)))
    assert len(events) == 1 and events[0][0] == "request_error"
    record = events[0][1]
    assert record["status"] == status and record["code"] == code
    assert record["elapsed_ms"] == 7 and record["measured"] is False
    assert "secret-token" not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "",
        "settings_fp",
        "keys_fp",
        "config_fp",
        "llm_provider",
        "llm_model",
        "embedding_model",
        "embedding_dim_configured",
        "embedding_dim_probed",
        "host",
        "instance",
        "uuid",
        "namespace_policy",
    ],
)
async def test_review_composed_proof_matches_actual_server_fingerprints_and_health(
    monkeypatch, tmp_path, fault
):
    import server.settings
    from server.core import cell, deployment_config

    fixture, config = fixture_config()
    fixture, launch_path, _, launch, _ = launch_files(tmp_path, fixture, config)
    monkeypatch.setattr(gate, "_owned_server", lambda _: None)
    mine = cell.mine()
    mine["deployment_id"] = fixture.deployment_id
    fingerprints = deployment_config.fingerprints(config)

    class PG(ProofPG):
        async def fetchrow(self, sql, *params):
            if "FROM loom.cell_self" in sql:
                return {"current_fp": json.dumps(cell.as_value(mine)), "next_fp": None}
            return await super().fetchrow(sql, *params)

    class CH(ProofCH):
        async def query_all(self, sql, params=None):
            if "FROM loom.namespaces" in sql:
                assert params == {"org": args().org, "ns": args().namespace}
                return [
                    {
                        "default_conflict_policy": "auto_reinforce"
                        if fault == "namespace_policy"
                        else "append_only"
                    }
                ]
            if "FROM loom.instances" in sql:
                return [
                    {
                        "instance_id": fixture.instance_id,
                        "build_commit": fixture.build_commit,
                        "workers_lease": "",
                        "serving": 1,
                        "host": "other" if fault == "host" else socket.gethostname(),
                        **fingerprints,
                        **({fault: "wrong"} if fault.endswith("_fp") else {}),
                    }
                ]
            if "FROM loom.cell_heartbeats" in sql:
                return [
                    {
                        "cell": fixture.cell,
                        "tenancy_mode": "enforced",
                        "fingerprint": json.dumps(
                            {**cell.as_value(mine), "serves": True, "marked": True}
                        ),
                    }
                ]
            return await super().query_all(sql, params)

    async def send(self, request, **kwargs):
        body = (
            {"deployment_id": fixture.deployment_id}
            if request.url.path.endswith("/deployment")
            else {
                "build_commit": fixture.build_commit,
                "instance": "other:4242"
                if fault == "instance"
                else launch["server"]["health_instance"],
                "deployment_config": {
                    "instance_id": "other" if fault == "uuid" else fixture.instance_id
                },
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
                **(
                    {fault: 999 if "dim" in fault else "wrong"}
                    if fault and not fault.endswith("_fp")
                    else {}
                ),
            }
        )
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(server.settings, "settings", config)
    monkeypatch.setattr(gate, "_source", lambda _: {"clean": True})
    monkeypatch.setattr(cell, "mine", lambda: dict(mine))
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    if fault:
        with pytest.raises(gate.Invalid):
            await gate._proof(PG(), CH(), args(launch_manifest=str(launch_path)), fixture)
    else:
        proof = await gate._proof(PG(), CH(), args(launch_manifest=str(launch_path)), fixture)
        assert proof["api_configuration_sha256"] == fixture.api_configuration_sha256
        assert proof["cli_configuration_sha256"] == fixture.cli_configuration_sha256
        assert proof["shared_configuration_sha256"] == fixture.shared_configuration_sha256
        assert proof["shared_configuration_excludes"] == ["cell_router_token"]
        assert proof["credential_posture"] == receipt_posture()
        assert proof["api_configuration_attested"] is True


def receipt_posture():
    return {
        "api_cell_router_token_present": True,
        "cli_cell_router_token_present": False,
        "api_router_operator_token_present": False,
        "cli_router_operator_token_present": False,
    }


def launch_files(tmp_path, fixture, config):
    import os
    import socket

    from server.core import deployment_config

    public = {
        k: v
        for k, v in config.model_dump().items()
        if k.startswith("pg_")
        and k != "pg_dsn"
        or k.startswith("memory_conflict")
        or k
        in {
            "storage_authority",
            "background_workers",
            "embedding_provider",
            "embedding_model",
            "embedding_dim",
            "embedding_send_dimensions",
            "embedding_worker_enabled",
            "embedding_cache_shared",
            "embedding_cache_shared_max_chars",
            "embedding_cache_size",
            "embedding_cache_ttl_s",
            "llm_provider",
            "llm_model",
        }
    }
    configuration = {
        "schema": 2,
        "api_configuration_sha256": fixture.api_configuration_sha256,
        "cli_configuration_sha256": fixture.cli_configuration_sha256,
        "shared_configuration_sha256": fixture.shared_configuration_sha256,
        "credential_posture": receipt_posture(),
        "workload": public,
        "fingerprints": deployment_config.fingerprints(config),
    }
    artifact_path = tmp_path / "configuration.json"
    artifact_bytes = json.dumps(configuration).encode()
    artifact_path.write_bytes(artifact_bytes)
    source_path = str(__import__("pathlib").Path(gate.__file__).resolve().parents[1])
    launch = {
        "schema": 2,
        "server": {
            "pid": 4242,
            "start_ticks": "123",
            "boot_id": "fake-boot-id",
            "uid": os.geteuid(),
            "cwd": source_path,
            "health_instance": f"{socket.gethostname()}:4242",
            "instance_id": fixture.instance_id,
        },
        "source": {"head": fixture.build_commit, "tree": fixture.source_tree},
        "configuration_artifact": {
            "path": str(artifact_path),
            "sha256": gate.hashlib.sha256(artifact_bytes).hexdigest(),
        },
    }
    launch_path = tmp_path / "launch.json"
    launch_bytes = json.dumps(launch).encode()
    launch_path.write_bytes(launch_bytes)
    fixture = dataclasses.replace(
        fixture, launch_manifest_sha256=gate.hashlib.sha256(launch_bytes).hexdigest()
    )
    return fixture, launch_path, artifact_path, launch, configuration


@pytest.mark.parametrize(
    "fault",
    [
        "",
        "launch_hash",
        "artifact_hash",
        "config",
        "workload",
        "workload_type",
        "fingerprints",
        "source",
        "instance",
        "symlink",
    ],
)
def test_launch_receipt_binds_named_owned_process_settings_and_nonsecret_artifact(
    monkeypatch, tmp_path, fault
):
    fixture, config = fixture_config()
    fixture, path, artifact_path, launch, configuration = launch_files(tmp_path, fixture, config)
    checked = []
    monkeypatch.setattr(gate, "_owned_server", lambda server: checked.append(server), raising=False)
    if fault in {"config", "workload", "workload_type", "fingerprints"}:
        if fault == "config":
            configuration["cli_configuration_sha256"] = "0" * 64
        if fault == "workload":
            configuration["workload"]["pg_delivery_batch_rows"] = 501
        if fault == "workload_type":
            configuration["workload"]["pg_delivery_batch_rows"] = 500.0
        if fault == "fingerprints":
            configuration["fingerprints"]["keys_fp"] = "wrong"
        blob = json.dumps(configuration).encode()
        artifact_path.write_bytes(blob)
        launch["configuration_artifact"]["sha256"] = gate.hashlib.sha256(blob).hexdigest()
    if fault == "artifact_hash":
        launch["configuration_artifact"]["sha256"] = "0" * 64
    if fault == "source":
        launch["source"]["tree"] = "0" * 40
    if fault == "instance":
        launch["server"]["instance_id"] = "other"
    blob = json.dumps(launch).encode()
    path.write_bytes(blob)
    fixture = dataclasses.replace(
        fixture,
        launch_manifest_sha256="0" * 64
        if fault == "launch_hash"
        else gate.hashlib.sha256(blob).hexdigest(),
    )
    if fault == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    if fault:
        with pytest.raises((gate.Invalid, OSError)):
            gate._launch(args(launch_manifest=str(path)), fixture, config)
    else:
        result = gate._launch(args(launch_manifest=str(path)), fixture, config)
        assert result["health_instance"] == launch["server"]["health_instance"]
        assert result["fingerprints"] == configuration["fingerprints"]
        assert checked == [launch["server"]]


@pytest.mark.parametrize(
    "fault",
    ["", "uid", "actual_uid", "pid", "start", "reuse", "boot", "cwd", "stamp_pid", "stamp_host"],
)
def test_owned_server_reads_only_exact_named_birth_metadata(monkeypatch, tmp_path, fault):
    import os
    from pathlib import Path

    from server.core import provider_process

    fixture, config = fixture_config()
    _, _, _, launch, _ = launch_files(tmp_path, fixture, config)
    server = dict(launch["server"])
    if fault == "uid":
        server["uid"] += 1
    if fault == "pid":
        server["pid"] = True
    if fault == "stamp_pid":
        server["health_instance"] = f"{socket.gethostname()}:4243"
    if fault == "stamp_host":
        server["health_instance"] = "other:4242"
    paths, births = [], []
    real_stat = Path.stat

    def metadata(path, **kwargs):
        if str(path).startswith("/proc"):
            assert str(path) == "/proc/4242"
            paths.append(str(path))
            return SimpleNamespace(st_uid=os.geteuid() + int(fault == "actual_uid"))
        return real_stat(path, **kwargs)

    def start(pid):
        assert type(pid) is int and pid == 4242
        births.append(pid)
        return "wrong" if fault == "start" or fault == "reuse" and len(births) == 2 else "123"

    real_read = Path.read_text

    def read(path, **kwargs):
        if str(path).startswith("/proc"):
            assert str(path) == "/proc/sys/kernel/random/boot_id"
            paths.append(str(path))
            return "wrong" if fault == "boot" else "fake-boot-id"
        return real_read(path, **kwargs)

    real_link = os.readlink

    def link(path, **kwargs):
        if str(path).startswith("/proc"):
            assert str(path) == "/proc/4242/cwd"
            paths.append(str(path))
            return "other" if fault == "cwd" else server["cwd"]
        return real_link(path, **kwargs)

    monkeypatch.setattr(Path, "stat", metadata)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(provider_process, "_start_ticks", start)
    monkeypatch.setattr(os, "readlink", link)
    if fault:
        with pytest.raises(gate.Invalid):
            gate._owned_server(server)
    else:
        gate._owned_server(server)
        assert births == [4242, 4242]
        assert paths == ["/proc/4242", "/proc/sys/kernel/random/boot_id", "/proc/4242/cwd"]


@pytest.mark.asyncio
async def test_failed_network_request_retains_elapsed_with_no_invented_status(monkeypatch):
    async def send(self, request, **kwargs):
        raise httpx.ConnectError("secret-token", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    clock = iter([1_000_000, 3_000_000])
    monkeypatch.setattr(gate.time, "perf_counter_ns", lambda: next(clock))
    records = []
    with pytest.raises(httpx.ConnectError):
        await gate._memory_set(args(), emit=lambda kind, **record: records.append(record))
    assert len(records) == 1 and records[0]["elapsed_ms"] == 2
    assert records[0]["status"] is None and records[0]["code"] == ""
    assert "secret-token" not in json.dumps(records)


def test_nonregular_receipt_open_is_nonblocking_before_type_check(monkeypatch):
    import os

    def open_receipt(path, flags):
        assert flags & os.O_NONBLOCK
        assert flags & os.O_NOFOLLOW
        raise gate.Invalid("nonregular receipt")

    monkeypatch.setattr(gate.os, "open", open_receipt)
    with pytest.raises(gate.Invalid, match="nonregular"):
        gate._receipt("named-owned-receipt")


def test_launch_refusal_precedes_any_native_or_store_setup(monkeypatch, tmp_path, capsys):
    import sys

    import server.settings
    import server.wiring
    from server.cli import _ch

    fixture, config = fixture_config()
    fixture, launch_path, _, _, _ = launch_files(tmp_path, fixture, config)
    fixture = dataclasses.replace(fixture, launch_manifest_sha256="0" * 64)
    manifest = tmp_path / "fixture.json"
    manifest.write_text(json.dumps(dataclasses.asdict(fixture)))
    arguments = vars(args(launch_manifest=str(launch_path)))
    arguments.update(fixture_manifest=str(manifest), output=str(tmp_path / "output.jsonl"))
    argv = ["driver"]
    for name, value in arguments.items():
        argv += ["--" + name.replace("_", "-"), str(value)]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("launch refusal must precede native/process setup")

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server.settings, "settings", config)
    monkeypatch.setattr(gate, "_source", lambda _: {"clean": True})
    monkeypatch.setattr(gate, "_owned_server", forbidden)
    monkeypatch.setattr(gate, "_preflight", forbidden)
    monkeypatch.setattr(server.wiring, "wire", forbidden)
    monkeypatch.setattr(_ch, "run_connected", forbidden)
    assert gate.main() == 1
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == "INVALID" and verdict["reason"] == "launch receipt hash"
    assert not (tmp_path / "output.jsonl").exists()


@pytest.mark.asyncio
async def test_restore_closes_before_installed_transaction_rollback_can_overrun(monkeypatch):
    from asyncpg.transaction import Transaction

    from server.cli import cell_attach_pg as apg
    from server.core import pg
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock()
    for module in (gate, apg, pg):
        monkeypatch.setattr(module, "time", clock)
    events = []
    release, rolling_back = asyncio.Event(), asyncio.Event()

    class Connection:
        _pool_release_ctr = 0
        _top_xact = None
        _protocol = SimpleNamespace(is_in_transaction=lambda: False)

        def __init__(self):
            self.closed = False
            self.timeouts = []

        def is_closed(self):
            return self.closed

        def transaction(self):
            return Transaction(self, None, False, False)

        async def execute(self, sql, *args):
            if sql.startswith("DROP TRIGGER"):
                raise ValueError("restoration DDL failed")
            if sql.startswith("ROLLBACK"):
                rolling_back.set()
                try:
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            continue
                finally:
                    events.append("rollback ended")

        async def close(self, **kwargs):
            self.timeouts.append(kwargs["timeout"])
            events.append("public close")
            self.closed = True
            release.set()

        def terminate(self):
            self.closed = True
            release.set()

    conn = Connection()
    monkeypatch.setattr(gate, "RESTORE_S", 0.04)
    artifact = SimpleNamespace(emit=lambda *a, **k: None)
    task = asyncio.create_task(
        gate._cleanup(gate.Triggers(("memory_current",), "", ""), conn, artifact)
    )
    try:
        await asyncio.wait_for(rolling_back.wait(), 1)
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert done, "restoration deadline waited for rollback before closing"
        with pytest.raises(TimeoutError):
            task.result()
        assert conn.closed
        assert events.index("public close") < events.index("rollback ended")
        assert conn.timeouts and all(0 < bound <= 0.04 for bound in conn.timeouts)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["SystemExit", "KeyboardInterrupt"])
@pytest.mark.parametrize("phase", ["restore", "close"])
def test_driver_cleanup_native_failure_stays_inside_its_owner(kind, phase):
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent(
        """
        import argparse
        import asyncio
        import sys
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from bench import cell_trigger_gate as gate
        kind, phase = sys.argv[1:]
        original = ValueError('original operation failure')
        secondary = {'SystemExit': SystemExit, 'KeyboardInterrupt': KeyboardInterrupt}[kind](
            'cleanup failed')
        events = []
        class Connection:
            async def close(self, **kwargs):
                events.append('close')
                if phase == 'close': raise secondary
            def terminate(self): events.append('abort')
        class Triggers:
            tables = ('memory_current',)
            invariant = org = 'hash'
            async def set(self, *args): pass
            async def restore(self, *args):
                events.append('restore')
                if phase == 'restore': raise secondary
        async def main():
            gate.asyncpg.connect = AsyncMock(return_value=Connection())
            gate._proof = AsyncMock(return_value={})
            gate.Triggers.capture = AsyncMock(return_value=Triggers())
            gate._memory_set = AsyncMock(side_effect=original)
            artifact = SimpleNamespace(emit=lambda *a, **k: None)
            try:
                await gate._run(argparse.Namespace(pg_dsn='unused'), None, artifact, object())
            except BaseException as error:
                assert error is original, (type(error).__name__, events)
                assert events[0] == 'restore' and 'close' in events, events
                await asyncio.sleep(0)
                print('original-after-driver-cleanup')
                return
            raise AssertionError('operation failure was swallowed')
        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program, kind, phase],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "original-after-driver-cleanup" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_kind", [ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]
)
@pytest.mark.parametrize(
    "close_kind", [RuntimeError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]
)
@pytest.mark.parametrize("diagnostic_kind", [OSError, SystemExit, KeyboardInterrupt])
async def test_cleanup_diagnostic_cannot_replace_restoration_error(
    first_kind, close_kind, diagnostic_kind
):
    from server.cli import cell_attach_pg as apg

    first = first_kind("restoration failed first")
    events = []

    class Triggers:
        async def restore(self, conn):
            raise first

    class Conn:
        async def close(self, **kwargs):
            events.append("close")
            raise close_kind("close failed second")

        def terminate(self):
            events.append("abort")

    class Artifact:
        def emit(self, event, **kwargs):
            events.append(event)
            raise diagnostic_kind("diagnostic failed third")

    async def owner():
        try:
            await gate._cleanup(Triggers(), Conn(), Artifact())
        except BaseException as error:  # noqa: BLE001 — assert the actual owner failure.
            assert error is first
            assert "connection_cleanup_error" in events
            return
        raise AssertionError("restoration failure was swallowed")

    result = await apg._work_result(owner)
    assert result is None


@pytest.mark.asyncio
async def test_successful_restoration_still_fails_when_artifact_write_fails():
    first = OSError("artifact failed")
    conn = SimpleNamespace(close=AsyncMock(), terminate=lambda: None)
    artifact = SimpleNamespace(emit=lambda *a, **k: (_ for _ in ()).throw(first))
    triggers = SimpleNamespace(restore=AsyncMock())
    with pytest.raises(OSError) as observed:
        await gate._cleanup(triggers, conn, artifact)
    assert observed.value is first
    conn.close.assert_awaited_once()


def rewrite_launch(fixture, path, artifact_path, launch, configuration, *, raw=None, level=None):
    configuration_raw = raw if level == "configuration" else json.dumps(configuration).encode()
    artifact_path.write_bytes(configuration_raw)
    launch["configuration_artifact"]["sha256"] = gate.hashlib.sha256(configuration_raw).hexdigest()
    launch_raw = raw if level == "launch" else json.dumps(launch).encode()
    path.write_bytes(launch_raw)
    return dataclasses.replace(
        fixture, launch_manifest_sha256=gate.hashlib.sha256(launch_raw).hexdigest()
    )


def test_schema_two_local_pair_has_only_one_fixed_credential_difference(monkeypatch, tmp_path):
    from server.core import deployment_config

    fixture, config = fixture_config()
    fixture, path, _, _, configuration = launch_files(tmp_path, fixture, config)
    checked = []
    monkeypatch.setattr(gate, "_owned_server", lambda value: checked.append(value))
    assert fixture.validate(args(), config) == 45500
    assert gate._launch(args(launch_manifest=str(path)), fixture, config)
    actual = config.model_dump(mode="json")
    api = {**actual, "cell_router_token": "lcr_" + "0" * 128}
    assert gate._digest(api) == fixture.api_configuration_sha256
    assert gate._digest(actual) == fixture.cli_configuration_sha256
    assert fixture.api_configuration_sha256 != fixture.cli_configuration_sha256
    assert gate._shared_configuration(config) == {
        key: value for key, value in api.items() if key != "cell_router_token"
    }
    assert configuration["fingerprints"] == deployment_config.fingerprints(config)
    assert len(checked) == 1


@pytest.mark.parametrize("field", ["cell_router_token", "router_operator_token", "jwt_secret"])
@pytest.mark.parametrize("shape", ["missing", "extra"])
def test_complete_settings_shape_refuses_missing_or_extra_fields(field, shape):
    config = role_config()
    if shape == "missing":
        delattr(config, field)
    else:
        config.unexpected_setting = "not-an-exclusion"
    with pytest.raises(gate.Invalid, match="Settings fields"):
        gate._configuration(config)


def test_every_other_effective_setting_stays_in_the_common_digest():
    from server.settings import Settings

    fixture, config = fixture_config()
    original = config.model_dump(mode="json")
    fields = set(original) - {"cell_router_token"}
    assert fields == set(Settings.model_fields) - {"cell_router_token"}
    configuration = {
        "schema": 2,
        "api_configuration_sha256": fixture.api_configuration_sha256,
        "cli_configuration_sha256": fixture.cli_configuration_sha256,
        "shared_configuration_sha256": fixture.shared_configuration_sha256,
        "workload": {},
        "fingerprints": {},
        "credential_posture": receipt_posture(),
    }
    for field in sorted(fields):
        try:
            setattr(config, field, ["changed-type-and-value", original[field]])
            changed = dataclasses.replace(
                fixture, cli_configuration_sha256=gate._digest(config.model_dump(mode="json"))
            )
            configuration["cli_configuration_sha256"] = changed.cli_configuration_sha256
            with pytest.raises(gate.Invalid, match="launch shared configuration"):
                gate._attested_configuration(configuration, changed, config)
        finally:
            setattr(config, field, original[field])


@pytest.mark.parametrize("value", [True, 1, 1.0, "1", None, [], {}])
def test_common_digest_keeps_json_value_types(value):
    _, config = fixture_config()
    config.pg_delivery_batch_rows = value
    assert gate._digest(gate._shared_configuration(config)) != gate._digest(
        {**gate._shared_configuration(config), "pg_delivery_batch_rows": "unchanged"}
    )
    hashes = {gate._digest({"value": v}) for v in [True, 1, 1.0, "1", None, [], {}]}
    assert len(hashes) == 7


@pytest.mark.parametrize("location", ["fixture", "launch", "configuration"])
@pytest.mark.parametrize("version", [None, 1, True, 2.0, "2"])
def test_old_missing_and_noninteger_receipt_versions_refuse(
    monkeypatch, tmp_path, location, version
):
    fixture, config = fixture_config()
    fixture, path, artifact_path, launch, configuration = launch_files(tmp_path, fixture, config)
    manifest = dataclasses.asdict(fixture)
    target = {"fixture": manifest, "launch": launch, "configuration": configuration}[location]
    if version is None:
        del target["schema"]
    else:
        target["schema"] = version
    if location != "server":
        monkeypatch.setattr(
            gate, "_owned_server", lambda _: pytest.fail("invalid receipt reached birth")
        )
    if location == "fixture":
        path.write_text(json.dumps(manifest))
        with pytest.raises(gate.Invalid):
            gate.Fixture.read(str(path))
    else:
        fixture = rewrite_launch(fixture, path, artifact_path, launch, configuration)
        with pytest.raises(gate.Invalid):
            gate._launch(args(launch_manifest=str(path)), fixture, config)


@pytest.mark.parametrize(
    "location",
    [
        "fixture",
        "launch",
        "server",
        "source",
        "configuration_artifact",
        "configuration",
        "workload",
        "fingerprints",
        "credential_posture",
    ],
)
@pytest.mark.parametrize("fault", ["duplicate", "unknown", "missing"])
def test_closed_receipt_objects_reject_duplicate_and_unknown_fields(
    monkeypatch, tmp_path, location, fault
):
    fixture, config = fixture_config()
    fixture, path, artifact_path, launch, configuration = launch_files(tmp_path, fixture, config)
    manifest = dataclasses.asdict(fixture)
    root = (
        manifest
        if location == "fixture"
        else configuration
        if location in {"configuration", "workload", "fingerprints", "credential_posture"}
        else launch
    )
    target = root if location in {"fixture", "launch", "configuration"} else root[location]
    if fault == "unknown":
        target["unexpected"] = True
        blob = json.dumps(root).encode()
    elif fault == "missing":
        del target[next(iter(target))]
        blob = json.dumps(root).encode()
    else:
        key = next(iter(target))
        original = json.dumps(target)
        duplicate = original[:-1] + ", " + json.dumps(key) + ": " + json.dumps(target[key]) + "}"
        blob = json.dumps(root).replace(original, duplicate, 1).encode()
    if location != "server":
        monkeypatch.setattr(
            gate, "_owned_server", lambda _: pytest.fail("invalid receipt reached birth")
        )
    if location == "fixture":
        path.write_bytes(blob)
        with pytest.raises(gate.Invalid):
            gate.Fixture.read(str(path))
    else:
        fixture = rewrite_launch(
            fixture,
            path,
            artifact_path,
            launch,
            configuration,
            raw=blob,
            level="configuration" if root is configuration else "launch",
        )
        with pytest.raises(gate.Invalid):
            gate._launch(args(launch_manifest=str(path)), fixture, config)


@pytest.mark.parametrize("key", list(receipt_posture()))
@pytest.mark.parametrize("fault", ["wrong", "integer", "missing"])
def test_posture_requires_exact_boolean_literals(monkeypatch, tmp_path, key, fault):
    fixture, config = fixture_config()
    fixture, path, artifact_path, launch, configuration = launch_files(tmp_path, fixture, config)
    if fault == "missing":
        del configuration["credential_posture"][key]
    else:
        configuration["credential_posture"][key] = (
            not receipt_posture()[key] if fault == "wrong" else int(receipt_posture()[key])
        )
    fixture = rewrite_launch(fixture, path, artifact_path, launch, configuration)
    monkeypatch.setattr(gate, "_owned_server", lambda _: pytest.fail("bad posture reached birth"))
    with pytest.raises(gate.Invalid, match="credential posture"):
        gate._launch(args(launch_manifest=str(path)), fixture, config)


@pytest.mark.parametrize("fault", ["equal", "cli", "shared", "swapped"])
def test_role_digests_cannot_be_equal_swapped_or_mismatched(fault):
    fixture, config = fixture_config()
    if fault == "equal":
        fixture = dataclasses.replace(
            fixture, api_configuration_sha256=fixture.cli_configuration_sha256
        )
    elif fault == "swapped":
        fixture = dataclasses.replace(
            fixture,
            api_configuration_sha256=fixture.cli_configuration_sha256,
            cli_configuration_sha256=fixture.api_configuration_sha256,
        )
    else:
        fixture = dataclasses.replace(fixture, **{fault + "_configuration_sha256": "0" * 64})
    with pytest.raises(gate.Invalid):
        fixture.validate(args(), config)


@pytest.mark.parametrize("location", ["fixture", "configuration"])
def test_removed_configuration_digest_is_not_a_compatibility_field(monkeypatch, tmp_path, location):
    fixture, config = fixture_config()
    fixture, path, artifact_path, launch, configuration = launch_files(tmp_path, fixture, config)
    target = dataclasses.asdict(fixture) if location == "fixture" else configuration
    target["configuration_sha256"] = fixture.cli_configuration_sha256
    if location == "fixture":
        path.write_text(json.dumps(target))
        with pytest.raises(gate.Invalid):
            gate.Fixture.read(str(path))
    else:
        fixture = rewrite_launch(fixture, path, artifact_path, launch, configuration)
        with pytest.raises(gate.Invalid):
            gate._launch(args(launch_manifest=str(path)), fixture, config)


@pytest.mark.parametrize("location", ["fixture", "launch", "configuration"])
def test_duplicate_json_fields_refuse_without_any_schema_assumption(tmp_path, location):
    path = tmp_path / "receipt.json"
    path.write_text('{"' + location + '":0,"' + location + '":1}')
    with pytest.raises(gate.Invalid, match="duplicate receipt field"):
        gate._receipt(str(path))


@pytest.mark.parametrize(
    "field", ["api_configuration_sha256", "cli_configuration_sha256", "shared_configuration_sha256"]
)
@pytest.mark.parametrize("value", [None, True, 64, "A" * 64, "f" * 63, "g" * 64])
def test_role_digest_fields_are_lowercase_sha256_strings(field, value):
    fixture, config = fixture_config()
    reason = "fixture receipt digest" if type(value) is str else "fixture field types"
    with pytest.raises(gate.Invalid, match=reason):
        dataclasses.replace(fixture, **{field: value}).validate(args(), config)


def test_schema_two_requires_explicit_internal_port_and_preserves_existing_bytes(tmp_path):
    fixture, config = fixture_config()
    value = dataclasses.asdict(fixture)
    del value["pg_internal_port"]
    original = json.dumps(value).encode()
    path = tmp_path / "fixture.json"
    path.write_bytes(original)
    with pytest.raises(gate.Invalid):
        gate.Fixture.read(str(path))
    assert path.read_bytes() == original
    value["pg_internal_port"] = None
    path.write_text(json.dumps(value))
    assert gate.Fixture.read(str(path)).validate(args(), config) == 45500


@pytest.mark.parametrize("target", ["fixture", "launch", "configuration"])
@pytest.mark.parametrize("fault", ["symlink", "oversize", "owner"])
def test_all_receipt_roles_keep_file_safety(monkeypatch, tmp_path, target, fault):
    import os

    fixture, config = fixture_config()
    fixture, path, artifact_path, launch, configuration = launch_files(tmp_path, fixture, config)
    manifest_path = tmp_path / "fixture.json"
    manifest_path.write_text(json.dumps(dataclasses.asdict(fixture)))
    selected = {"fixture": manifest_path, "launch": path, "configuration": artifact_path}[target]
    original = selected.read_bytes()
    if fault == "symlink":
        saved = tmp_path / "saved"
        saved.write_bytes(original)
        selected.unlink()
        selected.symlink_to(saved)
    elif fault == "oversize":
        selected.write_bytes(b" " * 65_537)
    else:
        actual = os.fstat

        def metadata(fd):
            result = actual(fd)
            return SimpleNamespace(st_mode=result.st_mode, st_uid=os.geteuid() + 1)

        monkeypatch.setattr(gate.os, "fstat", metadata)
    with pytest.raises((gate.Invalid, OSError, ValueError)):
        if target == "fixture":
            gate.Fixture.read(str(manifest_path))
        else:
            gate._launch(args(launch_manifest=str(path)), fixture, config)
    if fault != "oversize":
        assert selected.read_bytes() == original


def native_guard_edges(monkeypatch, config, events):
    from contextlib import asynccontextmanager

    import server.settings
    from server.auth import tenancy
    from server.code.domain.index import repos
    from server.core import background, cell, ch, pg, pg_profile
    from server.insights.app import provider_command
    from server.settings import Settings

    class Client:
        connected = False

        async def connect(self):
            events.append("native client")

    @asynccontextmanager
    async def model_command():
        yield

    native_settings = Settings.model_construct(**config.model_dump(mode="json"))

    async def state(sql):
        assert sql == cell._READ
        return {
            "first": {"is_first": True, "cell": config.cell},
            "mode": "enforced",
            "own": {"current_fp": cell.as_value(cell.mine()), "next_fp": None},
            "tenancy": True,
            "marked": True,
        }

    async def held(sql, org):
        assert sql == tenancy._HOLDS and org == "org"
        events.append("native held-org guard")
        return 1

    monkeypatch.setattr(server.settings, "settings", config)
    monkeypatch.setattr(cell, "settings", native_settings)
    monkeypatch.setattr(tenancy, "settings", native_settings)
    monkeypatch.setattr(repos, "settings", native_settings)
    monkeypatch.setattr(cell, "_view", cell._View())
    monkeypatch.setattr(cell, "_deployment_id", "")
    monkeypatch.setattr(cell, "_code_orgs", None)
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})
    monkeypatch.setattr(ch, "get_client", Client)
    monkeypatch.setattr(pg_profile, "check_configured", AsyncMock())
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="deployment"))
    monkeypatch.setattr(pg, "fetchrow_reset", state)
    monkeypatch.setattr(pg, "fetchval", held)
    monkeypatch.setattr(provider_command, "model_command", model_command)
    monkeypatch.setattr(background, "register_buffer_drainer", lambda *_: None)
    monkeypatch.setattr("server.cli._ch.disconnect", AsyncMock())


@pytest.mark.parametrize("credential", ["", "lcr_" + "0" * 128])
def test_role_pair_enters_the_real_native_guard_without_changing_it(monkeypatch, credential):
    from server.cli import _ch

    fixture, config = fixture_config()
    events = []
    native_guard_edges(monkeypatch, config, events)
    assert fixture.validate(args(), config) == 45500
    config.cell_router_token = credential

    async def work(_):
        events.append("native work")
        return 17

    if credential:
        with pytest.raises(_ch.OrgNotHeld, match="LOOM_CELL_ROUTER_TOKEN"):
            _ch.run_connected(work, orgs=("org",))
        assert events == ["native client"]
    else:
        assert _ch.run_connected(work, orgs=("org",)) == 17
        assert events == ["native client", "native held-org guard", "native work"]


@pytest.mark.parametrize(
    "fault",
    [
        "",
        "api_configuration",
        "operator_token",
        "arbitrary_setting",
        "legacy_field",
        "duplicate",
        "schema_type",
        "pg_port_type",
    ],
)
def test_receipt_orchestration_refuses_before_clients_and_keeps_the_real_native_guard(
    monkeypatch, tmp_path, capsys, fault
):
    import sys

    import server.wiring

    fixture, config = fixture_config()
    fixture, launch_path, _, _, _ = launch_files(tmp_path, fixture, config)
    manifest = dataclasses.asdict(fixture)
    if fault == "api_configuration":
        config.cell_router_token = "lcr_" + "0" * 128
    elif fault == "operator_token":
        config.router_operator_token = "lro_" + "1" * 128
    elif fault == "arbitrary_setting":
        config.instance_heartbeat_seconds += 1
        manifest["cli_configuration_sha256"] = gate._digest(config.model_dump(mode="json"))
    elif fault == "legacy_field":
        manifest["configuration_sha256"] = fixture.cli_configuration_sha256
    elif fault == "schema_type":
        manifest["schema"] = 2.0
    elif fault == "pg_port_type":
        manifest["pg_port"] = float(manifest["pg_port"])
    raw = json.dumps(manifest)
    if fault == "duplicate":
        raw = raw[:-1] + ', "schema":2}'
    manifest_path = tmp_path / "fixture.json"
    manifest_path.write_text(raw)
    output = tmp_path / "output.jsonl"
    argv = ["driver"]
    for key, value in vars(
        args(
            launch_manifest=str(launch_path),
            fixture_manifest=str(manifest_path),
            output=str(output),
        )
    ).items():
        argv += ["--" + key.replace("_", "-"), str(value)]
    events = []
    native_guard_edges(monkeypatch, config, events)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(gate, "_source", lambda _: {"clean": True})
    monkeypatch.setattr(gate, "_owned_server", lambda _: None)
    monkeypatch.setattr(
        gate, "_preflight", AsyncMock(side_effect=lambda *_: events.append("readonly"))
    )
    monkeypatch.setattr(server.wiring, "wire", lambda: events.append("wire"))
    summary = {
        "latency_within_limit": True,
        "throughput_within_limit": True,
        "p99_delta_ms": 0.0,
        "throughput_loss": 0.0,
    }

    async def run(*_):
        events.append("native work")
        return summary

    monkeypatch.setattr(gate, "_run", run)
    assert gate.main() == int(bool(fault))
    text = capsys.readouterr().out
    assert json.loads(text)["verdict"] == ("INVALID" if fault else "PASS")
    assert all(
        value not in text
        for value in [
            args().pg_dsn,
            "secret-token",
            "test-only-root",
            "lcr_" + "0" * 128,
            "lro_" + "1" * 128,
        ]
    )
    if fault:
        assert events == [] and not output.exists()
    else:
        assert events == [
            "readonly",
            "wire",
            "native client",
            "native held-org guard",
            "native work",
        ]
        record = json.loads(output.read_text().splitlines()[0])
        assert record["manifest"]["schema"] == 2
        assert record["shared_configuration_excludes"] == ["cell_router_token"]
        assert record["credential_posture"] == receipt_posture()


def test_unversioned_single_configuration_fixture_is_no_longer_accepted(tmp_path):
    legacy = {
        "base_url": args().base_url,
        "pg_host": "127.0.0.1",
        "pg_port": 15790,
        "pg_database": "gate_cell",
        "ch_host": "127.0.0.1",
        "ch_port": 18790,
        "ch_database": "gate_cell",
        "ch_telemetry_database": "gate_cell_t",
        "cell": "gate-cell",
        "org": "org",
        "namespace": "ns",
        "deployment_id": "deployment",
        "epoch": 1,
        "instance_id": "instance",
        "build_commit": "a" * 40,
        "source_tree": "b" * 40,
        "launch_manifest_sha256": "c" * 64,
        "ownership_receipt_sha256": "d" * 64,
        "configuration_sha256": "e" * 64,
        "background_workers": "off",
        "controller_excluded": True,
        "pg_internal_port": 5432,
    }
    path = tmp_path / "legacy.json"
    raw = json.dumps(legacy).encode()
    path.write_bytes(raw)
    with pytest.raises(gate.Invalid):
        gate.Fixture.read(str(path))
    assert path.read_bytes() == raw


@pytest.mark.parametrize("field", ["pg_port", "ch_port", "epoch"])
@pytest.mark.parametrize("kind", [bool, float, str])
def test_fixture_integer_fields_never_coerce_json_types(field, kind):
    fixture, config = fixture_config()
    with pytest.raises(gate.Invalid, match="fixture field types"):
        dataclasses.replace(fixture, **{field: kind(getattr(fixture, field))}).validate(
            args(), config
        )
