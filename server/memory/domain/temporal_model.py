"""Bi-temporal assertions: the amendments a commit makes and how a read reduces them.

A logical memory is a set of assertions. Each has a valid-time interval
`[valid_from, valid_to)` (None = open) and is born at one commit sequence and
retired at a later one. A commit retires some assertions and adds complete new
ones; nothing is edited in place. A read at cutoff C serves the assertions born
at or before C and not retired by C, then keeps those valid at V.

Pure: no ClickHouse, no clock. The publisher stamps sequences and times.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from server.core.hashing import sha256_value_json

PAYLOAD_SCHEMA = 1

ValidityPolicy = Literal["compat", "strict"]


@dataclass(frozen=True)
class Assertion:
    """One recorded claim about one logical memory over one valid-time span."""

    assertion_id: str
    origin_assertion_id: str
    value: str
    memory_type: str = "semantic"
    parent_assertion_id: str | None = None
    temporal_kind: str = "state"
    validity_basis: str = "unknown"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    # The date as written ("March 2024"), its precision and its zone.
    valid_text: str = ""
    valid_precision: str = ""
    valid_tz: str = ""
    modality: str = "asserted"
    status: str = "active"
    confidence: float = 1.0
    expires_at: datetime | None = None
    pinned: bool = False
    lock_mode: str = ""
    # When the lock lapses (None: it holds until unlocked).
    lock_until: datetime | None = None
    verified_by: str = ""
    verified_at: datetime | None = None
    verified_sha256: str = ""
    grounding: str = ""
    source_class: str = ""
    actor: str = ""
    sensitivity: str = ""
    source_version: int = 0
    embed_input_sha256: str = ""
    qualifiers: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()

    @property
    def value_sha256(self) -> str:
        return hashlib.sha256(self.value.encode()).hexdigest()

    @property
    def content_sha256(self) -> str:
        """The canonical-JSON hash loom.memory keeps: what `if_match_sha256` and an
        attestation's `verified_sha256` compare with."""
        return sha256_value_json(self.value)

    def valid_at(self, v: datetime, *, policy: ValidityPolicy = "compat") -> bool:
        """True when the assertion applies at V. A state or rule holds over
        `[valid_from, valid_to)`; an event or snapshot is a dated occurrence that
        applies once it has happened, never an open-ended state. Under `strict`, an
        unknown or inferred bound never admits a row."""
        if policy == "strict" and self.validity_basis in ("unknown", "inferred"):
            return False
        lo, hi = self.valid_from, self.valid_to
        if self.temporal_kind in ("event", "snapshot"):
            return lo <= v if lo is not None else policy == "compat"
        return (lo is None or lo <= v) and (hi is None or v < hi)

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        for k in ("valid_from", "valid_to", "expires_at", "lock_until", "verified_at"):
            out[k] = _iso(out[k])
        for k in ("tags", "entities", "evidence_ids", "derived_from"):
            out[k] = list(out[k])
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Assertion:
        known = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in known}
        for k in ("valid_from", "valid_to", "expires_at", "lock_until", "verified_at"):
            d[k] = _parse(d.get(k))
        for k in ("tags", "entities", "evidence_ids", "derived_from"):
            d[k] = tuple(d.get(k) or ())
        d["qualifiers"] = dict(d.get("qualifiers") or {})
        return cls(**d)


@dataclass(frozen=True)
class Amendment:
    """One complete change to one logical memory: what it retires, what it adds."""

    # assert | correct | transition | retract | assess | redact
    change_kind: str
    retired: tuple[str, ...] = ()
    added: tuple[Assertion, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.retired and not self.added

    def payload_sha256(self) -> str:
        """Hash of the change's meaning, not its ids or times: two retries of one
        request hash alike; a different request under the same key does not."""
        semantic = [
            {k: v for k, v in a.to_json().items() if k not in IDENTITY_FIELDS} for a in self.added
        ]
        body = json.dumps(
            {"kind": self.change_kind, "retired": sorted(self.retired), "added": semantic},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(body.encode()).hexdigest()


IDENTITY_FIELDS = frozenset(
    {"assertion_id", "origin_assertion_id", "parent_assertion_id", "source_version"}
)


def new_id() -> str:
    return str(uuid.uuid4())


def fresh(a: Assertion, **changes: Any) -> Assertion:
    """A new revision of `a`: a new id, `a` as parent, the same origin."""
    return replace(
        a,
        assertion_id=new_id(),
        parent_assertion_id=a.assertion_id,
        origin_assertion_id=a.origin_assertion_id,
        **changes,
    )


def first(value: str, **fields: Any) -> Assertion:
    aid = new_id()
    return Assertion(assertion_id=aid, origin_assertion_id=aid, value=value, **fields)


# -- amendments ---------------------------------------------------------------


def assert_value(
    active: Sequence[Assertion], new: Assertion, *, now: datetime | None = None
) -> Amendment:
    """A plain write under its qualifiers (memory.amend shapes history explicitly).
    A state with a start fills [start, end): it corrects that span, keeps every other
    segment, and stops at a state the key holds from later on (late evidence never
    reverts the present). A state with no start revises the state that holds now,
    keeping its start and stopping at a later plan; closed past segments and plans
    stay. An event or snapshot joins the key's others (one at the same instant is
    replaced). An unchanged claim is an empty amendment (a restatement)."""
    active = _peers(active, new)
    if new.valid_from is not None and new.valid_to is not None and new.valid_to <= new.valid_from:
        raise EmptyInterval(f"valid_to {new.valid_to.isoformat()} is not after its start")
    if new.temporal_kind in ("event", "snapshot"):
        at = [
            a
            for a in active
            if a.temporal_kind == new.temporal_kind and a.valid_from == new.valid_from
        ]
        return _replace(at, new)
    states = [a for a in active if a.temporal_kind in ("state", "rule")]
    if new.valid_from is not None:
        return _fill_span(states, new)
    now = now or datetime.now(UTC)
    held = [a for a in states if a.valid_at(now)]
    plans = sorted(a.valid_from for a in states if a.valid_from is not None and a.valid_from > now)
    end = min((t for t in (new.valid_to, *plans[:1]) if t is not None), default=None)
    if len(held) == 1 and held[0].valid_from is not None:
        new = replace(new, valid_from=held[0].valid_from, validity_basis=held[0].validity_basis)
    elif not held:
        # Nothing holds now: the claim starts where the key's last closed segment
        # ended, so it never overlaps the history before it.
        ended = [a.valid_to for a in states if a.valid_to is not None and a.valid_to <= now]
        if ended:
            new = replace(new, valid_from=max(ended), validity_basis="inferred")
    new = replace(new, valid_to=end)
    if new.valid_from is not None and end is not None and end <= new.valid_from:
        raise EmptyInterval(
            f"valid_to {end.isoformat()} is not after the start of the state it revises "
            f"({new.valid_from.isoformat()})"
        )
    return _replace(held, new)


class EmptyInterval(ValueError):
    """A write whose interval would end at or before it starts."""


def _replace(old: list[Assertion], new: Assertion) -> Amendment:
    if len(old) == 1 and _same_claim(old[0], new):
        return Amendment("assert")
    if old:
        new = replace(
            new,
            parent_assertion_id=old[0].assertion_id,
            origin_assertion_id=old[0].origin_assertion_id,
        )
    return Amendment("assert", tuple(a.assertion_id for a in old), (new,))


def _fill_span(states: list[Assertion], new: Assertion) -> Amendment:
    lo = new.valid_from
    assert lo is not None
    later = sorted(a.valid_from for a in states if a.valid_from is not None and a.valid_from > lo)
    end = new.valid_to
    if later:
        end = later[0] if end is None else min(end, later[0])
    # A span an assertion of the same value already covers says nothing new about
    # the world: at most new metadata for that assertion.
    for a in states:
        if a.value == new.value and (a.valid_from is None or a.valid_from <= lo):
            if a.valid_to is None or (end is not None and a.valid_to >= end):
                return _restate(a, new)
    return replace(correct(states, lo, end, new), change_kind="assert")


_VALIDITY = frozenset(
    {"valid_from", "valid_to", "validity_basis", "valid_text", "valid_precision", "valid_tz"}
)


def _restate(a: Assertion, new: Assertion) -> Amendment:
    """`new` restates `a`'s value inside `a`'s interval: an assessment carrying the
    metadata it changes (empty when it changes none)."""
    keep = IDENTITY_FIELDS | _VALIDITY | {"actor"}
    changes = {k: getattr(new, k) for k in new.to_json() if k not in keep}
    if replace(a, **changes) == a:
        return Amendment("assert")
    return assess([a], **changes, actor=new.actor)


def current_claims(after: Iterable[Assertion], now: datetime) -> list[Assertion]:
    """What the current row shows: what applies now (of events, the latest that has
    happened: a late-recorded older one fills the past), else (when nothing does)
    the latest that has begun."""
    after = list(after)
    held = latest_events(valid_now(after, now))
    if held:
        return held
    begun = [a for a in after if a.valid_from is None or a.valid_from <= now]
    return [max(begun, key=lambda a: a.valid_from or MIN)] if begun else []


def latest_events(held: Sequence[Assertion]) -> list[Assertion]:
    """`held` with only the latest event per qualifier set (the current row shows
    what last happened; an older one recorded late fills the past)."""
    last: dict[tuple[tuple[str, str], ...], Assertion] = {}
    for a in held:
        if a.temporal_kind == "event":
            q = tuple(sorted(a.qualifiers.items()))
            if q not in last or (a.valid_from or MIN, a.source_version) > (
                last[q].valid_from or MIN,
                last[q].source_version,
            ):
                last[q] = a
    keep = {id(a) for a in last.values()}
    return [a for a in held if a.temporal_kind != "event" or id(a) in keep]


def row_start(a: Assertion) -> datetime | None:
    """The start the current row shows: an inferred one stays in the journal."""
    return None if a.validity_basis == "inferred" else a.valid_from


def correct(
    active: Sequence[Assertion],
    start: datetime | None,
    end: datetime | None,
    new: Assertion | None,
) -> Amendment:
    """Replace `[start, end)` with `new` (None = nothing holds there) and keep every
    supported segment outside it, as fresh revisions of their originals. States and
    rules are trimmed; an event or snapshot inside the span is retired whole."""
    if new is not None:
        active = _peers(active, new)
        new = replace(new, valid_from=start, valid_to=end)
    # The span may already hold exactly this claim: it stays, the rest is trimmed.
    same = [a for a in active if new is not None and _same_claim(a, new)]
    retired: list[str] = []
    added: list[Assertion] = []
    for a in active:
        if a in same or not _overlaps(a, start, end):
            continue
        retired.append(a.assertion_id)
        if a.temporal_kind not in ("state", "rule"):
            continue
        if start is not None and (a.valid_from is None or a.valid_from < start):
            added.append(fresh(a, valid_to=start))
        if end is not None and (a.valid_to is None or a.valid_to > end):
            added.append(fresh(a, valid_from=end))
    if same:
        return Amendment("correct", tuple(retired), tuple(added))
    if new is not None:
        if retired:
            parent = next(a for a in active if a.assertion_id == retired[0])
            new = replace(
                new,
                parent_assertion_id=parent.assertion_id,
                origin_assertion_id=parent.origin_assertion_id,
            )
        added.append(new)
    return Amendment("correct", tuple(retired), tuple(added))


def transition(active: Sequence[Assertion], at: datetime, new: Assertion) -> Amendment:
    """The served state (or rule) ends at `at` and `new` holds from `at` on. One
    already closed before `at` is untouched; one that starts after `at` was a
    future expectation and is retired whole (a cancelled plan stays in history).
    A transition the key already serves is not added again, so a retry adds
    nothing. `at` is a declared bound, so an unknown basis becomes `stated`."""
    basis = "stated" if new.validity_basis == "unknown" else new.validity_basis
    kind = "rule" if new.temporal_kind == "rule" else "state"
    new = replace(new, valid_from=at, temporal_kind=kind, validity_basis=basis)
    active = _peers(active, new)
    same = [a for a in active if _same_claim(a, new)]
    retired: list[str] = []
    added: list[Assertion] = []
    for a in active:
        if a in same or a.temporal_kind not in ("state", "rule"):
            continue
        if a.valid_to is not None and a.valid_to <= at:
            continue
        retired.append(a.assertion_id)
        if a.valid_from is None or a.valid_from < at:
            added.append(fresh(a, valid_to=at))
    return Amendment("transition", tuple(retired), (*added, *([] if same else [new])))


def retract(active: Sequence[Assertion], ids: Iterable[str] | None = None) -> Amendment:
    """Remove assertions from the served set (all of the key's by default)."""
    wanted = set(ids) if ids is not None else None
    return Amendment(
        "retract",
        tuple(a.assertion_id for a in active if wanted is None or a.assertion_id in wanted),
    )


def assess(active: Sequence[Assertion], kind: str = "assess", **changes: Any) -> Amendment:
    """Revise trust or serving metadata of every served assertion, keeping content
    and interval. No change is an empty amendment."""
    retired: list[str] = []
    added: list[Assertion] = []
    for a in active:
        b = replace(a, **changes)
        if b == a:
            continue
        retired.append(a.assertion_id)
        added.append(fresh(a, **changes))
    return Amendment(kind, tuple(retired), tuple(added))


def apply(active: dict[str, Assertion], amendment: Amendment) -> dict[str, Assertion]:
    """The served set after one amendment. Raises when it retires an assertion
    that is not served: a commit built against a stale revision."""
    out = dict(active)
    for aid in amendment.retired:
        if aid not in out:
            raise ValueError(f"amendment retires {aid}, which is not served")
        del out[aid]
    for a in amendment.added:
        out[a.assertion_id] = a
    return out


def valid_now(
    assertions: Iterable[Assertion], v: datetime, *, policy: ValidityPolicy = "compat"
) -> list[Assertion]:
    """The assertions that apply at V, earliest start first. Of a key's snapshots
    under one set of qualifiers only the latest taken by V applies (a balance is
    not a running state); another qualifier's snapshot is another balance."""
    held = [a for a in assertions if a.valid_at(v, policy=policy)]
    latest: dict[tuple[tuple[str, str], ...], Assertion] = {}

    def taken(a: Assertion) -> tuple[datetime, int, str]:
        return (a.valid_from or MIN, a.source_version, a.assertion_id)  # ties: newest revision

    for a in held:
        if a.temporal_kind == "snapshot":
            q = tuple(sorted(a.qualifiers.items()))
            if q not in latest or taken(a) > taken(latest[q]):
                latest[q] = a
    keep = {id(a) for a in latest.values()}
    held = [a for a in held if a.temporal_kind != "snapshot" or id(a) in keep]
    return sorted(held, key=lambda a: (a.valid_from or MIN, a.assertion_id))


def disputed(held: Sequence[Assertion]) -> bool:
    """More than one distinct value holds at the same V under the same qualifiers:
    contradicting claims, kept and shown, not resolved by arrival order."""
    by_q: dict[tuple[tuple[str, str], ...], set[str]] = {}
    for a in held:
        if a.modality == "disputed" or a.status == "conflicted":
            return True
        by_q.setdefault(tuple(sorted(a.qualifiers.items())), set()).add(a.value_sha256)
    return any(len(v) > 1 for v in by_q.values())


def _peers(active: Sequence[Assertion], new: Assertion) -> list[Assertion]:
    """The served assertions `new` can supersede: claims under different
    qualifiers never supersede each other."""
    return [a for a in active if a.qualifiers == new.qualifiers]


def _same_claim(a: Assertion, b: Assertion) -> bool:
    keep = IDENTITY_FIELDS | {"actor"}
    da = {k: v for k, v in a.to_json().items() if k not in keep}
    db = {k: v for k, v in b.to_json().items() if k not in keep}
    return da == db


def _overlaps(a: Assertion, start: datetime | None, end: datetime | None) -> bool:
    lo, hi = a.valid_from, a.valid_to
    if a.temporal_kind in ("event", "snapshot"):
        if lo is None:
            return start is None and end is None
        return (start is None or lo >= start) and (end is None or lo < end)
    return (end is None or lo is None or lo < end) and (start is None or hi is None or hi > start)


MIN = datetime(1, 1, 1, tzinfo=UTC)


def utc(d: datetime | None) -> datetime | None:
    """Aware UTC; a naive datetime is read as UTC (how loom stores them)."""
    if d is None:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def _iso(d: datetime | None) -> str | None:
    d = utc(d)
    return d.isoformat() if d is not None else None


def _parse(s: Any) -> datetime | None:
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return utc(s)
    return utc(datetime.fromisoformat(str(s)))
