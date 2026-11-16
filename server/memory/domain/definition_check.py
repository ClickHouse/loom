"""Whether a shared definition is governed, and which one holds a name two hold. Pure.

A shared definition is governed while it was promoted from exactly one row, the
namespace-wide row under its key, and that row is live (`active`, not retired by a
supersession, not past its `expires_at` or `valid_to`), neither labelled nor graded
`medium` or `high`, and holds the text the shared copy holds (the served hash of both).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from server.core.hashing import served_content_sha256
from server.memory.domain.definitions import definition_names, fold_name, written_names
from server.memory.domain.promotion_source import promotion_source
from server.memory.domain.sensitivity import HIGH, MEDIUM, max_sensitivity

LapseReason = Literal[
    "not_namespace_wide",
    "no_term",
    "gone",
    "superseded",
    "conflicted",
    "duplicate",
    "not_active",
    "expired",
    "ended",
    "changed",
    "sensitive",
]


@dataclass(frozen=True)
class SharedDefinition:
    memory_key: str
    value: Any  # decoded
    promoted_from: tuple[str, ...]
    promoted_at: datetime
    promotion_event: str


@dataclass(frozen=True)
class Source:
    """The namespace-wide row a definition was promoted from, as its head reads now."""

    status: str
    invalid_at: datetime | None
    expires_at: datetime | None
    valid_to: datetime | None
    served_sha256: str


def copy_lapse(shared: SharedDefinition, *, org: str, namespace: str) -> LapseReason | None:
    """Why `shared` is not governed whatever its source holds, or None."""
    if shared.promoted_from != (
        promotion_source(org, namespace, "-", "-", "-", shared.memory_key),
    ):
        return "not_namespace_wide"
    if definition_names(shared.value) is None:
        return "no_term"
    return None


def source_lapse(
    shared: SharedDefinition, source: Source | None, *, now: datetime
) -> LapseReason | None:
    """Why `shared` is not governed given its source, sensitivity aside
    (`sensitivity_lapse` checks that last), or None. `source` is None when the
    namespace-wide row is gone; `now` is naive UTC."""
    if source is None:
        return "gone"
    reason = liveness_lapse(
        status=source.status,
        invalid_at=source.invalid_at,
        expires_at=source.expires_at,
        valid_to=source.valid_to,
        now=now,
    )
    if reason is None and source.served_sha256 != served_content_sha256(shared.value):
        return "changed"
    return reason


def liveness_lapse(
    *,
    status: str,
    invalid_at: datetime | None,
    expires_at: datetime | None,
    valid_to: datetime | None,
    now: datetime,
) -> LapseReason | None:
    """Why a source row is not live, or None: `memory.promote` refuses the same rows. The
    times are naive UTC."""
    # Each names the way back: a supersession is restored, a conflict resolved, a
    # duplicate verdict resolved; any other status is written again.
    if status == "superseded" or invalid_at is not None:
        return "superseded"
    if status == "conflicted":
        return "conflicted"
    if status == "duplicate":
        return "duplicate"
    if status != "active":
        return "not_active"
    if expires_at is not None and expires_at <= now:
        return "expired"
    if valid_to is not None and valid_to <= now:
        return "ended"
    return None


def sensitivity_lapse(label: str, graded: str) -> LapseReason | None:
    """`sensitive` when the source is labelled or its value graded `medium` or `high`; the
    last check, so a source another reason lapses is never graded."""
    if max_sensitivity([label, graded]) in (MEDIUM, HIGH):
        return "sensitive"
    return None


def lost_names(governed: list[SharedDefinition]) -> dict[str, list[tuple[str, str]]]:
    """For each governed definition that shares a name with another, the names it loses,
    as written, with the key that holds each: the latest `promoted_at` wins, a tie settled
    by `promotion_event`."""
    held: list[tuple[SharedDefinition, dict[str, str]]] = []
    winners: dict[str, SharedDefinition] = {}
    for d in sorted(governed, key=lambda d: (d.promoted_at, d.promotion_event)):
        names: dict[str, str] = {}  # folded name -> the name as first written
        for written in written_names(d.value):
            if name := fold_name(written):
                names.setdefault(name, written)
        held.append((d, names))
        for name in names:
            winners[name] = d
    lost: dict[str, list[tuple[str, str]]] = {}
    for d, names in held:
        for name, written in names.items():
            if winners[name] is not d:
                lost.setdefault(d.memory_key, []).append((written, winners[name].memory_key))
    return lost
