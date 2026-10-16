"""The memory side of namespace.rollback, planned and applied in the journal: for each
key whose served set as known at `as_of` differs from what it serves now, one commit
retires what it serves now and serves again what it served then, every claim as it
was (its span, its plans, its kind, its qualifiers). The current-state rows follow
the commits, as after any amendment."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.core.errors import PreconditionFailedError, TemporalUnavailableError, ValidationError
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import temporal_publisher as publisher
from server.memory.app import temporal_reads as reads
from server.memory.domain import temporal_model as tm
from server.memory.domain.temporal_reads import Axes
from server.memory.store import temporal as store
from server.memory.store.redaction import REDACTED_VALUE

log = logging.getLogger(__name__)

Ident = tuple[str, str, str, str]


@dataclass(frozen=True)
class KeyChange:
    """One key the rollback changes: what it served as known at `as_of`, and now.
    `refused`: a claim it served then is one today's rules refuse (a blacklisted or
    redacted value), so the key is left as it is."""

    ident: Ident
    memory_id: str
    then: tuple[tm.Assertion, ...]
    now: tuple[tm.Assertion, ...]
    refused: bool = False

    @property
    def action(self) -> str:
        return "reinstate" if self.then else "remove"

    def sample(self) -> dict[str, Any]:
        agent, uid, sid, key = self.ident
        return {
            "memory_key": key,
            "agent": agent,
            "user_id": uid,
            "session_id": sid,
            "action": "left_out" if self.refused else self.action,
            # A refused key's earlier value is one today's rules refuse to serve.
            "then": ["[withheld]" if self.refused else _shown(a) for a in self.then],
            "now": [_shown(a) for a in self.now],
        }


def _shown(a: tm.Assertion) -> Any:
    """A value as the sample may show it: never a high one or a redacted one."""
    if a.sensitivity == "high" or a.value == REDACTED_VALUE:
        return "[withheld]"
    return reads.decode_value(a.value)


@dataclass(frozen=True)
class Plan:
    changes: list[KeyChange]
    truncated: bool
    # When the namespace's journal begins (None: it has none).
    first_at: datetime | None


# Keys read per page: each page's claims, at both cuts, fit one read.
_PAGE = 50


async def plan(
    org: str, namespace: str, as_of: datetime, *, memory_keys: list[str] | None, limit: int
) -> Plan:
    """The keys whose served set as known at `as_of` differs from now: every key the
    journal has, a page at a time, the first `limit` changes kept (`truncated`
    when more differ). Refused when either cut has a gap: a plan from a partial
    read would remove keys it did not see."""
    from server.memory.store import temporal as journal

    now = datetime.now(UTC)
    then_cut = await reads.cutoff(org, namespace, Axes(valid=now, known=as_of))
    now_cut = await reads.cutoff(org, namespace, Axes(valid=now, known=None))
    _refuse_gaps(then_cut, now_cut)
    keys = list(memory_keys) if memory_keys else await journal.journal_keys(org, namespace)
    changes: list[KeyChange] = []
    for i in range(0, len(keys), _PAGE):
        page = keys[i : i + _PAGE]
        then, refused = await _served(org, then_cut, page)
        current, _ = await _served(org, now_cut, page)
        _refuse_gaps(then_cut, now_cut)
        # A key the journal first saw after as_of, seeded from a row older than it:
        # what it held at as_of was never recorded, so it is left as it is.
        refused |= await journal.seeded_after(org, namespace, page, as_of)
        for ident in sorted(then.keys() | current.keys()):
            was, held = then.get(ident, []), current.get(ident, [])
            if _state(was) == _state(held):
                continue
            if sum(not c.refused for c in changes) == limit:
                return Plan(changes, True, now_cut.first_at)
            changes.append(
                KeyChange(
                    ident=ident,
                    memory_id=next((h.memory_id for h in [*was, *held] if h.memory_id), ""),
                    then=tuple(h.a for h in was),
                    now=tuple(h.a for h in held),
                    refused=ident in refused,
                )
            )
    return Plan(changes, False, now_cut.first_at)


# Rows older than the journal: a gap that never clears. The keys it concerns are the
# seeded ones, which the plan leaves out.
_PERMANENT = ("history_before_journal",)


def _refuse_gaps(*cuts: reads.Cut) -> None:
    gaps = sorted({g for c in cuts for g in c.gaps if not g.startswith(_PERMANENT)})
    if any(g.startswith("truncated") for g in gaps):
        raise ValidationError(
            "the keys read together hold more claims than one rollback read takes; "
            "name fewer memory_keys (a single key past that bound cannot be rolled back)"
        )
    if gaps:
        raise TemporalUnavailableError(
            f"the journal cannot be read whole now ({', '.join(gaps)}); retry"
        )


async def _served(
    org: str, cut: reads.Cut, page: list[str]
) -> tuple[dict[Ident, list[reads.Held]], set[Ident]]:
    """Each identity's claims at the cut, unfiltered, and the identities with a claim
    today's rules refuse (a blacklisted or redacted value): those are never served
    again, so their keys are left as they are."""
    from server.core.ch import sequential_reads

    if not cut.seq:
        return {}, set()
    with sequential_reads():
        raw = await reads._served_at(
            org,
            cut,
            scope=WHOLE_NAMESPACE,
            memory_keys=page,
            agent=None,
            user_id=None,
            session_id=None,
            include_high=True,
            memory_type=None,
            tags=(),
            limit=10_000,
        )
    shown = {id(h) for h in await reads.visible(org, cut.namespace, list(raw.values()))}
    out: dict[Ident, list[reads.Held]] = {}
    refused: set[Ident] = set()
    for h in raw.values():
        ident = (h.agent, h.user_id, h.session_id, h.memory_key)
        out.setdefault(ident, []).append(h)
        if id(h) not in shown or h.a.value == REDACTED_VALUE:
            refused.add(ident)
    return out, refused


def _state(held: list[reads.Held]) -> list[str]:
    """What a reader is told: each claim's whole recorded state but its ids."""
    return sorted(
        repr(sorted((k, v) for k, v in h.a.to_json().items() if k not in _IDS)) for h in held
    )


_IDS = tm.IDENTITY_FIELDS | {"embed_input_sha256"}


async def apply(org: str, namespace: str, changes: list[KeyChange]) -> list[publisher.Receipt]:
    """Commit each change a key's state allows, as one batch; the receipts of the keys
    that committed. A refused key commits nothing, nor does one whose lock in force
    refuses the change. The current rows follow with `project`."""
    kept = [c for c in changes if not c.refused]
    restored = {a.assertion_id: a.value for c in kept for a in c.then}

    async def not_redacted_since() -> None:
        # A redaction scrubs the journal's payloads without a commit: under the lock,
        # each value this plan restores must still be what the journal holds.
        held = await store.journal_values(org, namespace, [c.ident for c in kept], list(restored))
        if any(held.get(aid) != value for aid, value in restored.items()):
            raise PreconditionFailedError(
                "a value this rollback restores was redacted since it was planned; "
                "plan the rollback again"
            )

    return await publisher.publish_amendments(
        org,
        namespace,
        [(c.ident, c.memory_id, _build(c)) for c in kept],
        operation="namespace.rollback",
        recheck=not_redacted_since,
    )


async def project(org: str, namespace: str, receipts: list[publisher.Receipt]) -> None:
    """The committed keys' current rows, rewritten to what now applies."""
    from server.memory.app.temporal_projection import project_keys

    idents = [r.ident for r in receipts if r.commit_seq]
    if idents:
        await project_keys(org, namespace, idents, read_token=publisher.read_token(receipts))


def _build(c: KeyChange) -> publisher.Build:
    def build(served: list[tm.Assertion]) -> tm.Amendment:
        now = datetime.now(UTC)
        if publisher.locked_against(served, "value" if c.then else "delete", now):
            return tm.Amendment("rollback")
        # A lock is today's control, not part of what the key said then: the one in
        # force stays, and one lifted since is not taken again.
        lock = publisher._lock_in_force(tm.current_claims(served, now), now)
        held = {
            "lock_mode": str(lock["lock_mode"]),
            "lock_until": tm.utc(lock["lock_until"]),
            "lock_reason": str(lock["lock_reason"]),
            "locked_by": str(lock["locked_by"]),
            "locked_at": tm.utc(lock["locked_at"]),
        }
        return tm.Amendment(
            "rollback",
            tuple(a.assertion_id for a in served),
            tuple(tm.fresh(a, **held) for a in c.then),
        )

    return build
