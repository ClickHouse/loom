from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from server.knowledge.domain.mcp_worker_schedule import (
    MAX_REFRESH_INTERVAL_SECONDS,
    MIN_REFRESH_INTERVAL_SECONDS,
    PeriodicSchedule,
    ScheduleBinding,
    ScheduleRejected,
    plan_scheduled_refresh,
)

_BINDING = ScheduleBinding("org", "namespace", "source", "connection", "worker", 2, 11, 3, 4)
_DUE = datetime(2026, 1, 1, tzinfo=UTC)
_SCHEDULE = PeriodicSchedule(_BINDING, MIN_REFRESH_INTERVAL_SECONDS, _DUE)


def test_rollback_and_late_retry_keep_the_exact_enqueue_identity() -> None:
    first = plan_scheduled_refresh(_SCHEDULE, active_binding=_BINDING, now=_DUE)
    assert first is not None
    offset = timezone(timedelta(hours=-5))
    restored = replace(_SCHEDULE, next_due_at=_DUE.astimezone(offset))
    retry = plan_scheduled_refresh(restored, active_binding=_BINDING, now=_DUE + timedelta(days=1))
    assert retry is not None
    assert retry.demand == first.demand
    assert retry.demand.due_at == _DUE
    assert retry.next_due_at > first.next_due_at


def test_overdue_periods_coalesce_without_shifting_cadence() -> None:
    now = _DUE + timedelta(days=7, seconds=59, microseconds=999_999)
    plan = plan_scheduled_refresh(_SCHEDULE, active_binding=_BINDING, now=now)
    assert plan is not None
    assert plan.demand.due_at == _DUE
    assert plan.next_due_at == _DUE + timedelta(days=7, minutes=5)

    advanced = replace(_SCHEDULE, next_due_at=plan.next_due_at)
    assert plan_scheduled_refresh(advanced, active_binding=_BINDING, now=now) is None
    assert plan_scheduled_refresh(advanced, active_binding=_BINDING, now=_DUE) is None
    next_plan = plan_scheduled_refresh(advanced, active_binding=_BINDING, now=plan.next_due_at)
    assert next_plan is not None
    assert next_plan.demand.job_id != plan.demand.job_id
    assert next_plan.demand.due_at == plan.next_due_at
    assert next_plan.next_due_at == plan.next_due_at + timedelta(minutes=5)


def test_periodic_pause_does_not_spend_the_cursor() -> None:
    paused = replace(_SCHEDULE, paused=True)
    assert plan_scheduled_refresh(paused, active_binding=_BINDING, now=_DUE) is None
    assert paused.next_due_at == _DUE


@pytest.mark.parametrize(
    "changes",
    [
        None,
        {"org": "other-org"},
        {"namespace": "other-namespace"},
        {"source_id": "other-source"},
        {"connection_id": "other-connection"},
        {"connection_generation": 3},
        {"source_generation": 12},
        {"selection_generation": 4},
        {"worker_id": "replacement-worker"},
        {"worker_generation": 5},
    ],
)
def test_revoked_or_changed_binding_does_not_schedule(
    changes: dict[str, Any] | None,
) -> None:
    active = replace(_BINDING, **changes) if changes is not None else None
    assert plan_scheduled_refresh(_SCHEDULE, active_binding=active, now=_DUE) is None
    if active is not None:
        prior = plan_scheduled_refresh(_SCHEDULE, active_binding=_BINDING, now=_DUE)
        current = plan_scheduled_refresh(
            replace(_SCHEDULE, binding=active), active_binding=active, now=_DUE
        )
        assert prior is not None and current is not None
        assert prior.demand.job_id != current.demand.job_id


@pytest.mark.parametrize("identifier", ["", "  ", "bad\nvalue", "é" * 513, True])
def test_invalid_binding_ids_are_rejected(identifier: Any) -> None:
    with pytest.raises(ScheduleRejected, match="identifier"):
        replace(_BINDING, source_id=identifier)


@pytest.mark.parametrize("generation", [0, -1, True, 1.5, 1 << 63])
def test_invalid_generation_is_rejected(generation: Any) -> None:
    with pytest.raises(ScheduleRejected, match="generation"):
        replace(_BINDING, worker_generation=generation)


@pytest.mark.parametrize(
    "interval",
    [0, -1, True, 1.5, MIN_REFRESH_INTERVAL_SECONDS - 1, MAX_REFRESH_INTERVAL_SECONDS + 1],
)
def test_invalid_interval_is_rejected(interval: Any) -> None:
    with pytest.raises(ScheduleRejected, match="interval"):
        replace(_SCHEDULE, interval_seconds=interval)


@pytest.mark.parametrize("changes", [{"binding": None}, {"paused": 0}, {"paused": "false"}])
def test_invalid_schedule_state_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ScheduleRejected, match="state"):
        replace(_SCHEDULE, **changes)


@pytest.mark.parametrize("timestamp", [_DUE.replace(tzinfo=None), "not a timestamp"])
def test_invalid_clock_is_rejected(timestamp: Any) -> None:
    with pytest.raises(ScheduleRejected, match="timestamp"):
        replace(_SCHEDULE, next_due_at=timestamp)
    with pytest.raises(ScheduleRejected, match="timestamp"):
        plan_scheduled_refresh(_SCHEDULE, active_binding=_BINDING, now=timestamp)


def test_periodic_clock_overflow_does_not_emit_a_demand() -> None:
    last = datetime.max.replace(tzinfo=UTC)
    schedule = replace(_SCHEDULE, next_due_at=last)
    with pytest.raises(ScheduleRejected, match="out of range"):
        plan_scheduled_refresh(schedule, active_binding=_BINDING, now=last)
