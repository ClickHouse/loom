"""Three memory-plane workers that had no pass-level tests: projection_repair,
recalibration and reflection_distill.

Each test runs the worker's `run_pass` with its collaborators faked. It checks
what the pass counts, which failures it survives, and what it leaves behind:
the worker registry's outcome, the calibration singleton, or the per-namespace
result rows. The worker registry and the calibration singleton are process
state, and each test swaps in its own copy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from server.memory.app import embedder_calibration as ec
from server.memory.app.embedder_calibration import CalibrationResult
from server.memory.app.projection_failures import (
    ProjectionFailureRetryResult,
    ProjectionFailureRow,
)
from server.workers import status as worker_status


@pytest.fixture(autouse=True)
def _own_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[worker_status.WorkerStatusRegistry]:
    registry = worker_status.WorkerStatusRegistry()
    monkeypatch.setattr(worker_status, "_registry", registry)
    yield registry


@pytest.fixture(autouse=True)
def _calibration_left_as_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec, "_calibration", ec.get_calibration())
    monkeypatch.setattr(ec, "_ns_overrides", {})
    monkeypatch.setattr(ec, "_ns_negative_at", {})


# --- projection_repair -----------------------------------------------------------


def _fake_retry(monkeypatch: pytest.MonkeyPatch, retry: Any) -> list[dict[str, Any]]:
    """Each failure retried by `retry`; the marks are recorded (their params) instead of run."""
    from server.memory.app import projection_failures as pf

    marks: list[dict[str, Any]] = []

    class CH:
        async def execute(self, sql: str, params: Any = None) -> None:
            marks.append(params)

    async def _retry(f: ProjectionFailureRow, **_: Any) -> ProjectionFailureRetryResult:
        return await retry(f)

    monkeypatch.setattr(pf, "_retry", _retry)
    monkeypatch.setattr(pf, "get_client", lambda: CH())
    return marks


def _failure(i: int, projection: str) -> ProjectionFailureRow:
    return ProjectionFailureRow(
        failure_id=f"f{i}",
        created_at=datetime(2026, 1, 1),
        org="acme",
        namespace="shop",
        agent="a",
        projection=projection,
        source_operation="memory.set",
        source_resource_id="",
        memory_keys=[f"k{i}"],
        error_type="TimeoutError",
        error_message="",
    )


async def test_a_repair_pass_counts_each_outcome_and_survives_a_poison_row(
    monkeypatch: pytest.MonkeyPatch, _own_registry: worker_status.WorkerStatusRegistry
) -> None:
    from server.memory.app import projection_failures as pf
    from server.memory.workers import projection_repair

    rows = [
        _failure(1, "memory_chunks"),
        _failure(2, "legacy_projection"),
        _failure(3, "entity_edges"),
        _failure(4, "memory_belief_sources"),
    ]
    limits: list[int] = []

    async def _scan(*, limit: int, **_: Any) -> list[ProjectionFailureRow]:
        limits.append(limit)
        return rows

    async def _retry(f: ProjectionFailureRow, **_: Any) -> ProjectionFailureRetryResult:
        if f.projection == "legacy_projection":
            return ProjectionFailureRetryResult(
                f.failure_id, f.projection, False, False, reason="unsupported_projection"
            )
        if f.projection == "entity_edges":
            return ProjectionFailureRetryResult(
                f.failure_id, f.projection, True, False, error="E" * 2000
            )
        return ProjectionFailureRetryResult(f.failure_id, f.projection, True, True)

    monkeypatch.setattr(pf, "scan_active_projection_failures", _scan)
    _fake_retry(monkeypatch, _retry)

    result = await projection_repair.run_pass(
        projection_repair.ProjectionRepairConfig(max_failures_per_pass=0)
    )
    assert limits[-1] == 1, "a zero cap still scans one row, never an unbounded batch"
    assert (result.scanned, result.retried, result.resolved, result.unsupported) == (4, 3, 2, 1)
    # The failing row is reported, bounded, and the row after it was still retried.
    (failed,) = result.failed
    assert failed["failure_id"] == "f3" and len(failed["error"]) == 512
    outcome = _own_registry.snapshot()["projection_repair"]["last_outcome"]
    assert outcome == {
        "scanned": 4,
        "retried": 3,
        "resolved": 2,
        "unsupported": 1,
        "abandoned": 0,
        "failed": 1,
    }


async def test_a_pass_reads_the_erasure_per_failure_and_marks_its_failures_once(
    monkeypatch: pytest.MonkeyPatch, _own_registry: worker_status.WorkerStatusRegistry
) -> None:
    from server.memory.app import projection_failures as pf
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers import projection_repair

    queued = [_failure(i, "memory_chunks") for i in range(30)]
    mutations: list[str] = []

    class CH:
        async def execute(self, sql: str, params: Any = None) -> None:
            mutations.append(sql)

    async def _scan(*, limit: int, **_: Any) -> list[ProjectionFailureRow]:
        return queued

    async def _mirrored(f: ProjectionFailureRow, **_: Any) -> str:
        return ""

    reads: list[str] = []

    async def no(org: str, **kw: Any) -> bool:
        reads.append(org)
        return False

    monkeypatch.setattr(pf, "scan_active_projection_failures", _scan)
    monkeypatch.setattr(pf, "_retry_memory_chunks", _mirrored)
    monkeypatch.setattr(pf, "_younger_than", lambda f, s: False)
    monkeypatch.setattr(pf, "get_client", lambda: CH())
    monkeypatch.setattr(queue, "under_erasure", no)
    result = await projection_repair.run_pass()
    assert result.resolved == 30
    assert sum("ALTER TABLE" in m for m in mutations) == 1
    assert reads == ["acme"] * 30, "the erasure is read right before each failure's write"


async def test_an_erasure_requested_mid_batch_stops_the_remaining_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import projection_failures as pf
    from server.memory.store import conflict_resolver as queue

    failures = [_failure(i, "memory_chunks") for i in range(5)]
    marked: list[Any] = []
    written: list[str] = []

    class CH:
        async def execute(self, sql: str, params: Any = None) -> None:
            marked.append(params["failure_ids"])

    async def _mirrored(f: ProjectionFailureRow, **_: Any) -> str:
        written.append(f.failure_id)
        return ""

    reads = 0

    async def erasing_after_two(org: str, **kw: Any) -> bool:
        nonlocal reads
        reads += 1
        return reads > 2

    monkeypatch.setattr(pf, "_retry_memory_chunks", _mirrored)
    monkeypatch.setattr(pf, "_younger_than", lambda f, s: False)
    monkeypatch.setattr(queue, "under_erasure", erasing_after_two)
    out = await pf.retry_failures(failures, ch=CH())
    assert written == ["f0", "f1"]
    assert [r.reason for r in out[2:]] == ["pending"] * 3
    assert marked == [["f0", "f1"]], "the failures left under the erasure stay active"


async def _resolved(f: ProjectionFailureRow) -> ProjectionFailureRetryResult:
    return ProjectionFailureRetryResult(f.failure_id, f.projection, True, True)


async def test_a_mark_sends_at_most_mark_chunk_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ids are one HTTP parameter, capped at 128 KiB: 3,500 UUIDs in one go is refused."""
    from server.memory.app import projection_failures as pf

    failures = [_failure(i, "memory_chunks") for i in range(3500)]
    done = [(f, await _resolved(f)) for f in failures]
    marks = _fake_retry(monkeypatch, _resolved)
    await pf.mark_retried(done)
    assert [len(m["failure_ids"]) for m in marks] == [1000, 1000, 1000, 500]


async def test_a_repair_pass_whose_scan_fails_records_a_failure(
    monkeypatch: pytest.MonkeyPatch, _own_registry: worker_status.WorkerStatusRegistry
) -> None:
    from server.memory.app import projection_failures as pf
    from server.memory.workers import projection_repair

    async def _scan(**_: Any) -> list[ProjectionFailureRow]:
        raise ConnectionError("clickhouse unreachable")

    monkeypatch.setattr(pf, "scan_active_projection_failures", _scan)
    with pytest.raises(ConnectionError):
        await projection_repair.run_pass()
    status = _own_registry.snapshot()["projection_repair"]
    assert status["consecutive_failures"] == 1
    assert "clickhouse unreachable" in status["last_error"]


# --- recalibration ---------------------------------------------------------------


def _calibration(**over: float) -> CalibrationResult:
    base: dict[str, Any] = {
        "similar_pairs_n": 20,
        "distractor_pairs_n": 20,
        "similar_p10": 0.60,
        "similar_p25": 0.65,
        "similar_p50": 0.70,
        "similar_p75": 0.75,
        "distractor_p50": 0.20,
        "distractor_p90": 0.30,
        "noise_floor": 0.32,
        "rerank_skip_above": 0.80,
        "hyde_fallback_threshold": 0.45,
        "query_rewrite_threshold": 0.40,
        "abstain_threshold": 0.35,
        "conflict_threshold": 0.85,
        "duplicate_threshold": 0.95,
        "embedder_model": "text-embedding-3-small",
        "is_fake": False,
    }
    base.update(over)
    return CalibrationResult(**base)


def _wire_probe(monkeypatch: pytest.MonkeyPatch, answers: list[Any]) -> list[Any]:
    """`calibrate_embedder` answers from `answers` in call order (an exception
    instance is raised); returns the kwargs of each call."""
    calls: list[Any] = []

    async def _probe(**kw: Any) -> CalibrationResult | None:
        calls.append(kw)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(ec, "calibrate_embedder", _probe)
    return calls


async def test_a_failed_probe_fails_the_pass_and_keeps_the_current_calibration(
    monkeypatch: pytest.MonkeyPatch, _own_registry: worker_status.WorkerStatusRegistry
) -> None:
    from server.memory.workers import recalibration

    current = _calibration()
    ec.set_calibration(current)
    _wire_probe(monkeypatch, [TimeoutError("vendor slow")])
    with pytest.raises(TimeoutError):
        await recalibration.run_pass()
    assert ec.get_calibration() is current
    status = _own_registry.snapshot()["recalibration"]
    assert status["consecutive_failures"] == 1 and "vendor slow" in status["last_error"]


async def test_an_unavailable_calibration_is_a_pass_that_changed_nothing(
    monkeypatch: pytest.MonkeyPatch, _own_registry: worker_status.WorkerStatusRegistry
) -> None:
    from server.memory.workers import recalibration

    current = _calibration()
    ec.set_calibration(current)
    _wire_probe(monkeypatch, [None])
    unavailable = await recalibration.run_pass()
    assert (unavailable.ran, unavailable.skipped_reason) == (False, "calibration unavailable")
    assert ec.get_calibration() is current
    status = _own_registry.snapshot()["recalibration"]
    assert status["consecutive_failures"] == 0 and status["last_error"] is None
    assert status["last_outcome"] == {
        "ran": False,
        "skipped_reason": "calibration unavailable",
        "drift_warnings": 0,
    }


def test_the_recalibrate_command_exits_1_on_a_failed_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.cli import ops

    async def _fail() -> Any:
        raise TimeoutError("vendor slow")

    monkeypatch.setattr("server.memory.workers.recalibration.run_pass", _fail)
    monkeypatch.setattr(ops, "run_connected", lambda run, **_declared: asyncio.run(run(None)))
    assert ops._cmd_recalibrate(None) == 1  # type: ignore[arg-type]
    assert "recalibration failed: probe error: vendor slow" in capsys.readouterr().err


async def test_drift_past_five_points_warns_and_less_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.workers import recalibration

    ec.set_calibration(_calibration(noise_floor=0.25))
    shifted = _calibration(noise_floor=0.375, conflict_threshold=0.85 - 0.049)
    _wire_probe(monkeypatch, [shifted])
    result = await recalibration.run_pass()
    assert result.ran is True and ec.get_calibration() is shifted
    (warning,) = result.drift_warnings
    assert warning.startswith("derived noise floor drifted 0.250 → 0.375")


async def test_a_pass_drops_the_cached_per_namespace_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass probes the global calibration only; the per-namespace cache is
    dropped so rows `code.calibrate` wrote from another process are re-read."""
    from server.memory.workers import recalibration

    ec.set_calibration(_calibration())
    calls = _wire_probe(monkeypatch, [_calibration()])
    ec._ns_overrides[("acme", "shop")] = None
    result = await recalibration.run_pass()
    assert result.ran is True and calls == [{}]
    assert ec._ns_overrides == {}


# --- reflection_distill ----------------------------------------------------------


async def test_the_distill_pass_reads_every_setting_and_records_its_counts(
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Any,
    _own_registry: worker_status.WorkerStatusRegistry,
) -> None:
    from server.memory.app.session_outcomes import DistillConfig, DistillResult
    from server.memory.workers import reflection_distill

    settings_override(
        reflection_distill_lookback_days=3,
        reflection_distill_max_judged_per_pass=7,
        reflection_distill_max_contrasts_per_pass=2,
        reflection_distill_min_confidence=0.9,
        reflection_distill_min_pair_similarity=0.6,
        reflection_distill_model="gpt-4o-mini",
    )
    seen: list[DistillConfig] = []

    async def _distill(cfg: DistillConfig) -> DistillResult:
        seen.append(cfg)
        return DistillResult(
            sessions_scanned=5,
            labelled=4,
            by_outcome={"success": 3, "failure": 1},
            contrast_candidates=1,
            reflections_written=1,
            unpaired=0,
            failures=["judge timeout"],
        )

    monkeypatch.setattr(reflection_distill, "run_distillation", _distill)
    await reflection_distill.run_pass()
    assert seen == [DistillConfig(3, 7, 2, 0.9, 0.6, "gpt-4o-mini")]
    assert _own_registry.snapshot()["reflection_distill"]["last_outcome"] == {
        "sessions_scanned": 5,
        "labelled": 4,
        "by_outcome": {"success": 3, "failure": 1},
        "contrast_candidates": 1,
        "reflections_written": 1,
        "unpaired": 0,
        "failures": 1,
    }


def test_worker_configs_read_settings_when_built_not_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.workers import conversation_titler
    from server.memory.workers import projection_repair
    from server.settings import settings

    monkeypatch.setattr(settings, "conversation_titler_max_threads_per_pass", 7)
    monkeypatch.setattr(settings, "projection_repair_max_failures_per_pass", 3)
    assert conversation_titler.ConversationTitlerConfig().max_threads_per_pass == 7
    assert projection_repair.ProjectionRepairConfig().max_failures_per_pass == 3


@pytest.mark.asyncio
async def test_code_calibrate_writes_the_workers_row_through_the_attributed_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code.calibrate writes the row shape the per-namespace reader selects,
    through the client's insert (query tag, timeout, retry) rather than the raw
    driver."""
    import server.core.ch as ch_mod
    from server.memory.app import embedder_calibration as ec

    class _InsertCH:
        def __init__(self) -> None:
            self.inserted: list[dict[str, Any]] = []

        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            assert table == "loom.namespace_calibration_result"
            self.inserted.extend(rows)

    ch = _InsertCH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    await ec.persist_namespace_calibration("o", "code-x", _calibration())
    [row] = ch.inserted
    assert row == {
        **ec.namespace_calibration_row("o", "code-x", _calibration()),
        **{k: row[k] for k in ("calibrated_at", "version")},
    }
    assert row["noise_floor"] == _calibration().noise_floor and row["version"] > 0


async def test_a_fresh_follow_owed_intent_is_left_to_its_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver records and follows its retirement itself, right after the supersede:
    a repair replaying the intent then would write a second record beside the writer's."""
    import dataclasses
    from datetime import timedelta

    from server.core.datetimes import naive_utc_now
    from server.memory.app import projection_failures as pf
    from server.memory.store import conflict_resolver as queue

    replayed: list[str] = []

    async def follow(f: ProjectionFailureRow, **_: Any) -> str:
        replayed.append(f.failure_id)
        return ""

    monkeypatch.setattr(pf, "_retry_follow_rows", follow)
    fresh = dataclasses.replace(_failure(1, pf.FOLLOW_OWED), created_at=naive_utc_now())
    (out,) = await pf.retry_failures([fresh], ch=object())
    assert (out.retried, out.reason) == (False, "pending") and replayed == []
    past_grace = timedelta(seconds=pf.OWED_GRACE_S + 1)
    aged = dataclasses.replace(fresh, created_at=naive_utc_now() - past_grace)
    marks: list[Any] = []

    class CH:
        async def execute(self, sql: str, params: Any = None) -> None:
            marks.append(params)

    async def no(org: str, **kw: Any) -> bool:
        return False

    monkeypatch.setattr(queue, "under_erasure", no)
    (out,) = await pf.retry_failures([aged], ch=CH())
    assert out.resolved and replayed == ["f1"]
