"""Bind a bounded manual refresh offer to an exact current source selection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from server.knowledge.domain.mcp_identity import resource_digest
from server.knowledge.domain.mcp_worker_refresh import MAX_DOCUMENTS_PER_JOB, MAX_IDENTIFIER_BYTES
from server.knowledge.domain.mcp_worker_schedule import (
    RefreshDemand,
    ScheduleBinding,
    ScheduleRejected,
    plan_manual_refresh,
)

_MAX_SEQUENCE = (1 << 63) - 1
_OFFER_LIFETIME = timedelta(minutes=5)
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True, repr=False)
class ManualPosition:
    incarnation: UUID
    sequence: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.incarnation, UUID)
            or self.incarnation.int == 0
            or type(self.sequence) is not int
            or not 0 <= self.sequence <= _MAX_SEQUENCE
        ):
            raise ScheduleRejected("invalid manual refresh position")

    def next(self) -> ManualPosition:
        if self.sequence == _MAX_SEQUENCE:
            raise ScheduleRejected("manual refresh sequence exhausted")
        return ManualPosition(self.incarnation, self.sequence + 1)

    def key(self) -> str:
        return f"mcp-refresh-offer-v1:{self.incarnation}:{self.sequence}"


@dataclass(frozen=True, slots=True, repr=False)
class ManualSelection:
    binding: ScheduleBinding
    owner: str
    actor: str
    profile_digest: str
    destination_id: str
    selected: tuple[str, ...]

    def fingerprint(self) -> str:
        for value in (self.owner, self.actor, self.destination_id):
            if (
                not isinstance(value, str)
                or not 0 < len(value) <= 1024
                or not value.isprintable()
                or value in {"", "-"}
                or value != value.strip()
                or len(value.encode()) > 1024
            ):
                raise ScheduleRejected("invalid manual refresh selection")
        if (
            not isinstance(self.binding, ScheduleBinding)
            or not isinstance(self.profile_digest, str)
            or _DIGEST.fullmatch(self.profile_digest) is None
            or not isinstance(self.selected, tuple)
            or not 0 < len(self.selected) <= MAX_DOCUMENTS_PER_JOB
        ):
            raise ScheduleRejected("invalid manual refresh selection")
        try:
            selected = sorted(_refresh_resource_digest(resource) for resource in self.selected)
        except (ValueError, TypeError):
            raise ScheduleRejected("invalid manual refresh selection") from None
        if len(set(selected)) != len(selected):
            raise ScheduleRejected("duplicate manual refresh resource")
        payload = json.dumps(
            (
                "mcp-refresh-manifest-v1",
                astuple(self.binding),
                self.owner,
                self.actor,
                self.profile_digest,
                self.destination_id,
                selected,
            ),
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def _refresh_resource_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_IDENTIFIER_BYTES
        or not value.isprintable()
        or len(value.encode()) > MAX_IDENTIFIER_BYTES
    ):
        raise ScheduleRejected("invalid manual refresh resource")
    return resource_digest(value)


@dataclass(frozen=True, slots=True, repr=False)
class ManualOffer:
    position: ManualPosition
    fingerprint: str
    issued_at: datetime
    expires_at: datetime
    accepted_items: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.position, ManualPosition)
            or self.position.sequence == 0
            or not isinstance(self.fingerprint, str)
            or _DIGEST.fullmatch(self.fingerprint) is None
            or _clock(self.expires_at) - _clock(self.issued_at) != _OFFER_LIFETIME
            or (
                self.accepted_items is not None
                and (
                    type(self.accepted_items) is not int
                    or not 1 <= self.accepted_items <= MAX_DOCUMENTS_PER_JOB
                )
            )
        ):
            raise ScheduleRejected("invalid manual refresh offer")

    def current(self, now: datetime) -> bool:
        return self.issued_at <= _clock(now) < self.expires_at


def _clock(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ScheduleRejected("invalid manual refresh clock")
    return value.astimezone(UTC)


def next_offer(
    position: ManualPosition,
    previous: ManualOffer | None,
    selection: ManualSelection,
    now: datetime,
) -> ManualOffer:
    """Return the still-current offer, or rotate an expired slot with the same incarnation."""
    now = _clock(now)
    fingerprint = selection.fingerprint()
    if previous is not None:
        if previous.position != position:
            raise ScheduleRejected("manual refresh position changed")
        if previous.current(now):
            if previous.fingerprint != fingerprint:
                raise ScheduleRejected("manual refresh selection changed")
            return previous
    return ManualOffer(position.next(), fingerprint, now, now + _OFFER_LIFETIME)


def manual_demand(
    offer: ManualOffer, requested: ManualPosition, selection: ManualSelection, *, now: datetime
) -> RefreshDemand:
    """Use the original server offer timestamp; retired or replaced positions always refuse."""
    if (
        requested != offer.position
        or not offer.current(now)
        or selection.fingerprint() != offer.fingerprint
    ):
        raise ScheduleRejected("manual refresh offer unavailable")
    demand = plan_manual_refresh(
        selection.binding,
        active_binding=selection.binding,
        request_id=offer.position.key(),
        requested_at=offer.issued_at,
    )
    if demand is None:
        raise ScheduleRejected("manual refresh offer unavailable")
    return demand
