"""Plan selected refresh work from store snapshots without claiming or writing it.

The store supplies an active binding only after checking current authority. A
demand is not a lease or permission to read, upload, or retire a document.
"""

import hashlib
import json
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta

MAX_REFRESH_INTERVAL_SECONDS = 366 * 24 * 60 * 60
_MAX_IDENTIFIER_BYTES = 1024
_MAX_GENERATION = (1 << 63) - 1


class ScheduleRejected(ValueError):
    """The scheduling metadata or clock cannot be used."""


def _require_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_IDENTIFIER_BYTES
        or not value.isprintable()
        or not value.strip()
        or len(value.encode()) > _MAX_IDENTIFIER_BYTES
    ):
        raise ScheduleRejected("invalid refresh schedule identifier")


def _utc(value: datetime) -> datetime:
    try:
        if isinstance(value, datetime) and value.utcoffset() is not None:
            return value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        pass
    raise ScheduleRejected("invalid refresh schedule timestamp")


@dataclass(frozen=True, slots=True)
class ScheduleBinding:
    """Store identity of an exact approved selection and worker assignment."""

    org: str
    namespace: str
    source_id: str
    connection_id: str
    worker_id: str
    connection_generation: int
    selection_generation: int
    worker_generation: int

    def __post_init__(self) -> None:
        for identifier in (
            self.org,
            self.namespace,
            self.source_id,
            self.connection_id,
            self.worker_id,
        ):
            _require_identifier(identifier)
        for generation in (
            self.connection_generation,
            self.selection_generation,
            self.worker_generation,
        ):
            if type(generation) is not int or not 1 <= generation <= _MAX_GENERATION:
                raise ScheduleRejected("invalid refresh schedule generation")


@dataclass(frozen=True, slots=True)
class PeriodicSchedule:
    binding: ScheduleBinding
    interval_seconds: int
    next_due_at: datetime
    paused: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ScheduleBinding) or type(self.paused) is not bool:
            raise ScheduleRejected("invalid refresh schedule state")
        if (
            type(self.interval_seconds) is not int
            or not 1 <= self.interval_seconds <= MAX_REFRESH_INTERVAL_SECONDS
        ):
            raise ScheduleRejected("invalid refresh schedule interval")
        object.__setattr__(self, "next_due_at", _utc(self.next_due_at))


@dataclass(frozen=True, slots=True)
class RefreshDemand:
    """Key and enqueue time for a job over the store's bounded approved selection."""

    binding: ScheduleBinding
    job_id: str
    due_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduledRefresh:
    demand: RefreshDemand
    next_due_at: datetime


def _demand(binding: ScheduleBinding, kind: str, anchor: str, due_at: datetime) -> RefreshDemand:
    key = json.dumps(
        ("mcp-refresh-schedule-v1", kind, astuple(binding), anchor), separators=(",", ":")
    )
    return RefreshDemand(binding, hashlib.sha256(key.encode()).hexdigest(), due_at)


def plan_scheduled_refresh(
    schedule: PeriodicSchedule,
    *,
    active_binding: ScheduleBinding | None,
    now: datetime,
) -> ScheduledRefresh | None:
    """Coalesce elapsed periods into one demand keyed by the persisted due time."""
    now = _utc(now)
    due_at = schedule.next_due_at
    if schedule.paused or schedule.binding != active_binding or now < due_at:
        return None
    interval = timedelta(seconds=schedule.interval_seconds)
    try:
        next_due_at = due_at + ((now - due_at) // interval + 1) * interval
    except OverflowError:
        raise ScheduleRejected("next refresh schedule timestamp is out of range") from None
    return ScheduledRefresh(
        _demand(schedule.binding, "periodic", due_at.isoformat(), due_at), next_due_at
    )


def plan_manual_refresh(
    binding: ScheduleBinding,
    *,
    active_binding: ScheduleBinding | None,
    request_id: str,
    requested_at: datetime,
) -> RefreshDemand | None:
    """Use the store's original request identity and time, without a periodic cursor."""
    _require_identifier(request_id)
    due_at = _utc(requested_at)
    if active_binding is None or binding != active_binding:
        return None
    return _demand(binding, "manual", request_id, due_at)
