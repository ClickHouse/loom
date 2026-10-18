"""Every replica serves one similarity calibration: adopted from
loom.embedder_calibration at boot (probing only when no row exists), kept in
step by a refresh tick, and a per-namespace row read on another replica
serves here within its TTL. CH-free: the store and the probe are faked."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from typing import Any

import pytest

from server.memory.app import embedder_calibration as ec

MODEL = "text-embedding-3-large"


def _cal(conflict: float = 0.8) -> ec.CalibrationResult:
    c = ec.derive_thresholds([0.7, 0.8, 0.9], [0.4, 0.5, 0.6], [0.1, 0.2], embedder_model=MODEL)
    return replace(c, conflict_threshold=conflict)


class _Store:
    """loom.embedder_calibration as one dict; `fail` raises like a missing table."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.fail = False
        self.writes = 0

    async def fetch(self, model: str, *, ch: Any = None) -> dict[str, Any] | None:
        if self.fail:
            raise RuntimeError("Code: 60. Table loom.embedder_calibration does not exist")
        return self.rows.get(model)

    async def write(self, row: dict[str, Any], *, ch: Any = None) -> None:
        if self.fail:
            raise RuntimeError("Code: 60. Table loom.embedder_calibration does not exist")
        self.writes += 1
        self.rows[row["embedder_model"]] = dict(row)


@pytest.fixture
def env(monkeypatch):
    store = _Store()
    probes: list[ec.CalibrationResult | None] = []
    calls = {"probe": 0}

    async def _probe(*a: Any, **k: Any) -> ec.CalibrationResult | None:
        calls["probe"] += 1
        return probes.pop(0) if probes else _cal()

    monkeypatch.setattr("server.memory.store.calibration.fetch_global_calibration", store.fetch)
    monkeypatch.setattr("server.memory.store.calibration.write_global_calibration", store.write)
    monkeypatch.setattr(ec, "calibrate_embedder", _probe)
    monkeypatch.setattr(ec, "_served_model", lambda: MODEL)
    monkeypatch.setattr(ec.settings, "adaptive_thresholds", True)
    monkeypatch.setattr(ec, "_shared_read_warned", False)
    ec.set_calibration(None)
    yield store, probes, calls
    ec.set_calibration(None)
    ec.clear_ns_overrides()


@pytest.mark.asyncio
async def test_boot_adopts_the_shared_row_without_probing(env) -> None:
    store, _probes, calls = env
    shared = _cal(conflict=0.91)
    await ec.publish_calibration(shared)
    assert await ec.adopt_or_probe() == shared
    assert calls["probe"] == 0
    assert ec.calibration_source() == "shared"
    assert ec.current_calibration_hash() == ec.calibration_hash(shared)
    assert ec.calibration_ready()


@pytest.mark.asyncio
async def test_the_first_replica_probes_and_publishes_and_the_next_adopts(env) -> None:
    store, _probes, calls = env
    first = await ec.adopt_or_probe()
    assert calls["probe"] == 1 and store.writes == 1
    ec.set_calibration(None)
    assert await ec.adopt_or_probe() == first
    assert calls["probe"] == 1


@pytest.mark.asyncio
async def test_a_refresh_adopts_a_recalibration_published_elsewhere(env) -> None:
    store, _probes, _calls = env
    await ec.adopt_or_probe()
    newer = _cal(conflict=0.77)
    await ec.publish_calibration(newer)
    await ec.refresh_shared()
    assert ec.get_calibration() == newer
    assert ec.current_calibration_hash() == ec.calibration_hash(newer)


@pytest.mark.asyncio
async def test_a_failed_boot_probe_is_retried_on_the_refresh_tick(env) -> None:
    store, probes, calls = env
    probes.append(None)  # a 429 at boot
    assert await ec.adopt_or_probe() is None
    assert not ec.calibration_ready()
    await ec.refresh_shared()
    assert ec.get_calibration() is not None and ec.calibration_ready()
    assert store.writes == 1  # and published for the others


@pytest.mark.asyncio
async def test_a_missing_table_falls_back_to_the_local_probe_then_publishes(env) -> None:
    store, _probes, calls = env
    store.fail = True
    c = await ec.adopt_or_probe()
    assert c is not None and ec.calibration_source() == "probe"
    store.fail = False  # the migration ran
    await ec.refresh_shared()
    assert store.writes == 1 and ec.calibration_source() == "shared"
    assert calls["probe"] == 1  # the local result is published, not re-probed


@pytest.mark.asyncio
async def test_another_models_row_is_not_adopted(env) -> None:
    store, _probes, calls = env
    other = replace(_cal(), embedder_model="gemini-embedding-001")
    store.rows[MODEL] = {"thresholds_json": json.dumps(asdict(other))}
    await ec.adopt_or_probe()
    assert calls["probe"] == 1


def test_a_payload_from_another_build_reads(env) -> None:
    data = asdict(_cal())
    data["field_from_a_newer_build"] = 1
    for k in ("question_fact_p10", "question_fact_pairs_n"):
        data.pop(k)  # an older build's payload
    c = ec.calibration_from_payload(json.dumps(data))
    assert c is not None and c.question_fact_p10 == 0.0
    assert ec.calibration_from_payload("{}") is None


def test_ready_when_adaptive_gates_do_not_apply(env, monkeypatch) -> None:
    monkeypatch.setattr(ec.settings, "adaptive_thresholds", False)
    assert ec.calibration_ready()
    monkeypatch.setattr(ec.settings, "adaptive_thresholds", True)
    monkeypatch.setattr(ec, "_served_model", lambda: "fake-ngram-3072")
    assert ec.calibration_ready()


@pytest.mark.asyncio
async def test_a_found_namespace_row_expires_so_another_replicas_write_serves(
    monkeypatch,
) -> None:
    clock = {"t": 1000.0}
    answers = [_cal(conflict=0.8), _cal(conflict=0.6)]

    class _CH:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            c = answers.pop(0)
            return {k: v for k, v in asdict(c).items()}

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(ec.time, "monotonic", lambda: clock["t"])
    ec.clear_ns_overrides()
    first = await ec.get_namespace_calibration("o", "n")
    assert first is not None and first.conflict_threshold == 0.8
    clock["t"] += ec._NS_TTL_S + 1
    second = await ec.get_namespace_calibration("o", "n")
    assert second is not None and second.conflict_threshold == 0.6
    ec.clear_ns_overrides()


def test_health_reports_the_calibration_hash(env) -> None:
    from server.health import _calibration_block

    assert _calibration_block(False)["calibration_hash"] is None
    c = _cal()
    ec.set_calibration(c, source="shared")
    block = _calibration_block(False)
    assert block["calibration_hash"] == ec.calibration_hash(c)
    assert block["source"] == "shared"


@pytest.mark.asyncio
async def test_each_replica_rereads_the_shared_calibration_on_its_interval(monkeypatch) -> None:
    from server.settings import settings

    naps: list[float] = []
    reads: list[int] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) > 2:
            raise asyncio.CancelledError

    async def reread() -> None:
        reads.append(1)

    monkeypatch.setattr(settings, "calibration_refresh_seconds", 42.0)
    monkeypatch.setattr(ec.asyncio, "sleep", nap)
    monkeypatch.setattr(ec, "refresh_shared", reread)
    ec.stop_refresh_loop()
    with pytest.raises(asyncio.CancelledError):
        await ec.start_refresh_loop()
    assert naps == [42.0, 42.0, 42.0] and len(reads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    [("embedding_dim", 1024), ("embedding_base_url", "https://other.example/v1")],
)
async def test_the_same_label_in_another_space_probes_instead_of_adopting(
    env, monkeypatch: pytest.MonkeyPatch, name: str, value: Any
) -> None:
    store, _probes, calls = env
    await ec.publish_calibration(_cal(conflict=0.91))
    monkeypatch.setattr(ec.settings, name, value)
    ec.set_calibration(None)
    await ec.adopt_or_probe()
    assert calls["probe"] == 1 and ec.calibration_source() == "shared"
    assert len(store.rows) == 2  # each space keeps its own row


@pytest.mark.asyncio
async def test_a_row_keyed_by_the_model_alone_is_never_adopted(env) -> None:
    store, _probes, calls = env
    store.rows[MODEL] = {
        "thresholds_json": json.dumps(asdict(_cal(conflict=0.91)), sort_keys=True),
        "calibration_hash": "x",
        "calibrated_by": "another process",
        "calibrated_at": "",
        "version": 1,
    }
    await ec.adopt_or_probe()
    assert calls["probe"] == 1
