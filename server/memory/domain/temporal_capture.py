"""From a loom.memory version row to the amendment it makes, and the write intent
a handler can declare for it.

Every memory writer goes through `insert_memory_versions`, which hands each row
here. A row carries the full state of the key after the write; comparing it with
what the key serves decides the change: a new claim, an assessment of the same
claim, or a retraction. A handler that means something more specific (a
correction over an interval, a transition at an effective time) declares it on
the intent first.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from server.memory.domain import temporal_model as tm
from server.memory.domain.embed_text import stored_embedding_texts

# Statuses that keep a row in the served belief set (`proposed` is served only to
# a read that opts in, so it stays an assertion with that status).
_SERVED = frozenset({"active", "conflicted", "duplicate", "proposed"})
# Fields an assessment may change without asserting anything new about the world
# (classification and the entities the claim names included).
ASSESS_FIELDS: tuple[str, ...] = (
    "memory_type",
    "entities",
    "status",
    "confidence",
    "expires_at",
    "pinned",
    "lock_mode",
    "lock_until",
    "lock_reason",
    "locked_by",
    "locked_at",
    "verified_by",
    "verified_at",
    "verified_sha256",
    "grounding",
    "sensitivity",
    "tags",
)


@dataclass(frozen=True)
class WriteIntent:
    """What a handler means by the rows it is about to write.

    `change_kind` overrides the inferred kind (`transition` needs `effective_at`,
    `correct` takes `[valid_from, valid_to)` from the row). The temporal shape
    fields fill the new assertion; an omitted one keeps the served assertion's.
    `expected_revision` is the assertion id the writer read; a key whose served
    set no longer holds it refuses the write. `memory_key` names the key the
    intent is about: rows of other keys written in the same request (conflict
    peers, supersessions) are captured with no intent. `journaled` marks rows of
    that key whose commit the handler already published."""

    memory_key: str = ""
    change_kind: str = ""
    effective_at: datetime | None = None
    temporal_kind: str = ""
    validity_basis: str = ""
    valid_text: str = ""
    valid_precision: str = ""
    valid_tz: str = ""
    modality: str = ""
    qualifiers: Mapping[str, str] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()
    expected_revision: str = ""
    journaled: bool = False
    # The write changes the memory it read (a lock, pin, verify, touch or update by
    # id): refused if the journal no longer serves the key, never a re-assertion.
    existing: bool = False
    # The memory id a new key's current-state row takes (the one already journaled).
    memory_id: str = ""
    # The write holds only while the key is still expired (an expiry sweep: a key
    # touched since keeps its new expiry, the write is left out).
    conditional: bool = False
    # A value write that must not replace a pinned current value it did not pin
    # (memory.set, bulk).
    pin_guard: bool = False
    # A lock that may only tighten the lock in force (memory.lock).
    tighten_lock: bool = False


_intent: ContextVar[WriteIntent | None] = ContextVar("loom_temporal_intent", default=None)


@contextmanager
def write_intent(intent: WriteIntent) -> Iterator[None]:
    token = _intent.set(intent)
    try:
        yield
    finally:
        _intent.reset(token)


def current_intent() -> WriteIntent:
    return _intent.get() or WriteIntent()


_NONE = WriteIntent()

# Per (org, namespace, agent, user_id, session_id, memory_key): the columns a
# metadata write sets (a lock, a pin, an expiry, a status flip). Its row is built,
# when it is planned, from the key's state in the journal with only those set: it
# never carries a stale copy of anything else.
_declared: ContextVar[Mapping[tuple[str, ...], frozenset[str]] | None] = ContextVar(
    "loom_temporal_declared", default=None
)


@contextmanager
def declare_fields(fields: Mapping[tuple[str, ...], frozenset[str]]) -> Iterator[None]:
    token = _declared.set(fields)
    try:
        yield
    finally:
        _declared.reset(token)


def declared_fields(row: Mapping[str, Any]) -> frozenset[str] | None:
    """The columns a metadata write of `row` sets (None: a write of its own row)."""
    return (_declared.get() or {}).get(row_key(row))


# Per row_key: why the journal left a row of this block out (superseded, stale,
# locked, restated), for a writer that re-reads and retries.
_left_out: ContextVar[dict[tuple[str, ...], str] | None] = ContextVar(
    "loom_temporal_left_out", default=None
)


@contextmanager
def left_out_reasons() -> Iterator[dict[tuple[str, ...], str]]:
    reasons: dict[tuple[str, ...], str] = {}
    token = _left_out.set(reasons)
    try:
        yield reasons
    finally:
        _left_out.reset(token)


def note_left_out(key: tuple[str, ...], why: str) -> None:
    reasons = _left_out.get()
    if reasons is not None:
        reasons[key] = why


def row_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get(c) or "")
        for c in ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
    )


def intent_for(row: Mapping[str, Any], intent: WriteIntent) -> WriteIntent:
    """The intent that applies to one row: the declared one for its key (or for
    every row when it names none), else none."""
    if not intent.memory_key or intent.memory_key == str(row.get("memory_key") or ""):
        return intent
    return _NONE


def embed_input_sha256(memory_type: str, value: str, memory_key: str) -> str:
    """sha256 of the exact text memory.set embeds for this value and key."""
    _, text, _ = stored_embedding_texts(memory_type, value, memory_key)
    return hashlib.sha256(text.encode()).hexdigest()


def assertion_from_row(row: Mapping[str, Any], intent: WriteIntent) -> tm.Assertion:
    """The claim a version row makes. The valid-time bound is the writer's
    `valid_at`, else the event date extraction found (`temporal_anchor`), else
    unknown — disclosed as such, never taken from the ingest clock."""
    valid_from = tm.utc(row.get("valid_at"))
    basis = "stated"
    if valid_from is None and row.get("temporal_anchor") is not None:
        valid_from, basis = tm.utc(row["temporal_anchor"]), "extracted"
    elif valid_from is None:
        basis = "unknown"
    elif str(row.get("source_class") or "") == "extracted":
        basis = "extracted"
    value = str(row.get("value") or "")
    memory_type = str(row.get("memory_type") or "semantic")
    key = str(row.get("memory_key") or "")
    return tm.first(
        value,
        memory_type=memory_type,
        temporal_kind=intent.temporal_kind or "state",
        validity_basis=intent.validity_basis or basis,
        valid_from=valid_from,
        valid_to=tm.utc(row.get("valid_to")),
        valid_text=intent.valid_text,
        valid_precision=intent.valid_precision,
        valid_tz=intent.valid_tz,
        modality=intent.modality or "asserted",
        status=str(row.get("status") or "active"),
        confidence=_confidence(row),
        expires_at=_seconds(tm.utc(row.get("expires_at"))),
        pinned=bool(row.get("pinned")),
        lock_mode=str(row.get("lock_mode") or ""),
        lock_until=_seconds(tm.utc(row.get("lock_until"))),
        lock_reason=str(row.get("lock_reason") or ""),
        locked_by=str(row.get("locked_by") or ""),
        locked_at=_seconds(tm.utc(row.get("locked_at"))),
        verified_by=str(row.get("verified_by") or ""),
        verified_at=_seconds(tm.utc(row.get("verified_at"))),
        verified_sha256=str(row.get("verified_sha256") or ""),
        grounding=str(row.get("grounding") or ""),
        source_class=str(row.get("source_class") or ""),
        actor=str(row.get("actor") or ""),
        sensitivity=str(row.get("sensitivity") or ""),
        source_version=int(row.get("version") or 0),
        embed_input_sha256=embed_input_sha256(memory_type, value, key),
        qualifiers=dict(intent.qualifiers),
        tags=tuple(row.get("tags") or ()),
        entities=tuple(row.get("entities") or ()),
        evidence_ids=intent.evidence_ids,
        derived_from=intent.derived_from,
    )


def _seconds(d: datetime | None) -> datetime | None:
    """To the second, as loom.memory's DateTime keeps it: the journal's lock lapses
    when the current row's does."""
    return d.replace(microsecond=0) if d is not None else None


def removes(row: Mapping[str, Any]) -> bool:
    """A row that takes the key out of the served belief set."""
    return (
        row.get("tombstoned_at") is not None
        or row.get("redacted_at") is not None
        or str(row.get("status") or "active") not in _SERVED
    )


def amendment_for_row(
    row: Mapping[str, Any],
    served: Sequence[tm.Assertion],
    intent: WriteIntent,
    head: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> tm.Amendment:
    """The change one version row makes to what its key serves. `head` is the
    loom.memory row it replaces: a row that carries the head's value and validity
    forward (verify, lock, pin, tag, a status flip) assesses the served
    assertions and keeps every segment a correction left."""
    if removes(row):
        kind = "redact" if row.get("redacted_at") is not None else "retract"
        return replace(tm.retract(served), change_kind=kind)
    new = assertion_from_row(row, intent)
    # An explicit kind applies once: a later row of the same key in the request (a
    # status flip, a supersession) finds its claim already served and is captured plainly.
    if intent.change_kind == "transition" and intent.effective_at is not None:
        at = tm.utc(intent.effective_at) or intent.effective_at
        if not _serves(served, new.value, at, None):
            return tm.transition(served, at, new)
    elif intent.change_kind == "correct":
        if not _serves(served, new.value, new.valid_from, new.valid_to):
            return tm.correct(served, new.valid_from, new.valid_to, new)
    if served and _carries_forward(row, head, served, new, now) and not _reshapes(intent, served):
        shown = _shown(served, new, str(row.get("content_sha256") or ""), head, now)
        am = _assess(shown, {f: getattr(new, f) for f in ASSESS_FIELDS})
        # A new type changes the embed text (a procedure embeds its steps, not its JSON).
        key = str(row.get("memory_key") or "")
        added = tuple(
            replace(a, embed_input_sha256=embed_input_sha256(a.memory_type, a.value, key))
            for a in am.added
        )
        return replace(am, added=added)
    return tm.assert_value(served, inherit_shape(new, served, intent), now=now)


_SHAPE_FIELDS = (
    "temporal_kind",
    "validity_basis",
    "valid_text",
    "valid_precision",
    "valid_tz",
    "modality",
    "qualifiers",
)
_ATTESTATION = ("verified_by", "verified_at", "verified_sha256")


def _shown(
    served: Sequence[tm.Assertion],
    new: tm.Assertion,
    sha: str,
    head: Mapping[str, Any] | None,
    now: datetime | None,
) -> list[tm.Assertion]:
    """What a metadata write assesses: the served claims of the row's content under
    the write's own qualifiers (a sensitivity follows the text into that lineage's
    history). A write naming none, on a key holding that content under several
    qualifier sets, assesses the lineage of the claim the current row shows (by
    its validity). Other values and qualifiers keep their own metadata."""
    same = [a for a in served if a.content_sha256 in (new.content_sha256, sha)]
    mine = [a for a in same if a.qualifiers == new.qualifiers]
    if not mine:
        current = tm.current_claims(same, now or datetime.now(UTC))
        if head is not None:
            shows = (_seconds(_start(head)), _seconds(tm.utc(head.get("valid_to"))))
            current = [
                a for a in current if (_seconds(tm.row_start(a)), _seconds(a.valid_to)) == shows
            ] or current
        lineages = {tuple(sorted(a.qualifiers.items())) for a in current}
        if len(lineages) == 1:
            mine = [a for a in same if tuple(sorted(a.qualifiers.items())) in lineages]
    return mine or same or list(served)


def _reshapes(intent: WriteIntent, served: Sequence[tm.Assertion]) -> bool:
    """The write declares a temporal shape a served assertion does not have: a
    revision of the claim, not an assessment of it."""
    return any(
        getattr(intent, f) and getattr(intent, f) != getattr(a, f)
        for f in _SHAPE_FIELDS
        for a in served
    )


def assess_claims(
    claims: Sequence[tm.Assertion], changes: dict[str, Any], memory_key: str
) -> tm.Amendment:
    """An assessment of `claims` with `changes`; a new type re-derives the embed text
    (a procedure embeds its steps, not its JSON)."""
    am = _assess(claims, changes)
    added = tuple(
        replace(a, embed_input_sha256=embed_input_sha256(a.memory_type, a.value, memory_key))
        for a in am.added
    )
    return replace(am, added=added)


def _assess(served: Sequence[tm.Assertion], changes: dict[str, Any]) -> tm.Amendment:
    """Assess the served assertions. An attestation binds content, so it lands only
    on the assertions whose value it hashes; the others keep their own."""
    sha = changes.get("verified_sha256")
    bound = [a for a in served if not sha or a.content_sha256 == sha]
    rest = [a for a in served if a not in bound]
    out = tm.assess(bound, **changes)
    if not rest:
        return out
    other = tm.assess(rest, **{k: v for k, v in changes.items() if k not in _ATTESTATION})
    return tm.Amendment("assess", out.retired + other.retired, out.added + other.added)


def _serves(
    served: Sequence[tm.Assertion], value: str, lo: datetime | None, hi: datetime | None
) -> bool:
    return any((a.value, a.valid_from, a.valid_to) == (value, lo, hi) for a in served)


def _start(r: Mapping[str, Any]) -> datetime | None:
    """The valid-time start a row asserts (`assertion_from_row`'s)."""
    return tm.utc(r.get("valid_at")) or tm.utc(r.get("temporal_anchor"))


def _carries_forward(
    row: Mapping[str, Any],
    head: Mapping[str, Any] | None,
    served: Sequence[tm.Assertion],
    new: tm.Assertion,
    now: datetime | None,
) -> bool:
    """The row restates the content it replaces: the head's value and validity
    when there is a live head that shows what the journal holds now, else the
    single served assertion's. (A head behind the journal, its plan begun but not
    yet projected, is no claim to restate.)"""
    shown = {a.value for a in tm.current_claims(served, now or datetime.now(UTC))}
    if head is not None and head.get("tombstoned_at") is None and head.get("value") in shown:
        return (
            str(row.get("content_sha256") or "") == str(head.get("content_sha256") or "")
            and _start(row) == _start(head)
            and tm.utc(row.get("valid_to")) == tm.utc(head.get("valid_to"))
        )
    if len(served) != 1:
        return False
    a = served[0]
    return (a.value, a.valid_from, a.valid_to) == (new.value, new.valid_from, new.valid_to)


def inherit_shape(
    new: tm.Assertion, served: Sequence[tm.Assertion], intent: WriteIntent
) -> tm.Assertion:
    """A revision keeps the temporal shape the key already declared unless the
    write declares a new one: the qualifiers every served claim shares, and the
    kind and modality of the claims under those qualifiers when they agree. A
    write naming qualifiers takes the rest from the claims under them only. The
    same write sent again inherits the same shape."""
    if intent.qualifiers:
        served = [a for a in served if a.qualifiers == dict(intent.qualifiers)]
    quals = {tuple(sorted(a.qualifiers.items())) for a in served}
    qualifiers = dict(intent.qualifiers) or (dict(next(iter(quals))) if len(quals) == 1 else {})
    peers = [a for a in served if a.qualifiers == qualifiers]
    shapes = {(a.temporal_kind, a.modality) for a in peers}
    kind, modality = next(iter(shapes)) if len(shapes) == 1 else (new.temporal_kind, new.modality)
    return replace(
        new,
        temporal_kind=intent.temporal_kind or kind,
        modality=intent.modality or modality,
        qualifiers=qualifiers,
    )


def _confidence(row: Mapping[str, Any]) -> float:
    c = row.get("confidence")
    return 1.0 if c is None else float(c)
