"""The memory side of namespace.rollback, planned and applied in the journal: for each
key whose served set as known at `as_of` differs from what it serves now, one commit
retires what it serves now and serves again what it served then, every claim as it
was (its span, its plans, its kind, its qualifiers). The current-state rows follow
the commits, as after any amendment."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import server.memory.store as store
from server.core.errors import TemporalUnavailableError
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import temporal_publisher as publisher
from server.memory.app import temporal_reads as reads
from server.memory.domain import temporal_model as tm
from server.memory.domain.temporal_reads import Axes
from server.memory.store.redaction import REDACTED_VALUE

log = logging.getLogger(__name__)

Ident = tuple[str, str, str, str]


@dataclass(frozen=True)
class KeyChange:
    """One key the rollback changes: what it served as known at `as_of`, and now."""

    ident: Ident
    memory_id: str
    then: tuple[tm.Assertion, ...]
    now: tuple[tm.Assertion, ...]

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
            "action": self.action,
            "then": [reads.decode_value(a.value) for a in self.then],
            "now": [reads.decode_value(a.value) for a in self.now],
        }


@dataclass(frozen=True)
class Plan:
    changes: list[KeyChange]
    truncated: bool
    # When the namespace's journal begins (None: it has none).
    first_at: datetime | None


async def plan(
    org: str, namespace: str, as_of: datetime, *, memory_keys: list[str] | None, limit: int
) -> Plan:
    """The keys whose served set as known at `as_of` differs from now (up to `limit`)."""
    now = datetime.now(UTC)
    then_cut = await reads.cutoff(org, namespace, Axes(valid=now, known=as_of))
    now_cut = await reads.cutoff(org, namespace, Axes(valid=now, known=None))
    if now_cut.watermark < now_cut.seq:
        raise TemporalUnavailableError("the journal's projection is catching up; retry")
    then = await _served(org, then_cut, memory_keys)
    current = await _served(org, now_cut, memory_keys)
    truncated = any(g.startswith("truncated") for g in (*then_cut.gaps, *now_cut.gaps))
    changes: list[KeyChange] = []
    for ident in sorted(then.keys() | current.keys()):
        was, held = then.get(ident, []), current.get(ident, [])
        if _state(was) == _state(held):
            continue
        if len(changes) == limit:
            truncated = True
            break
        changes.append(
            KeyChange(
                ident=ident,
                memory_id=next((h.memory_id for h in [*was, *held] if h.memory_id), ""),
                # A redacted claim is never served again as a value.
                then=tuple(h.a for h in was if h.a.value != REDACTED_VALUE),
                now=tuple(h.a for h in held),
            )
        )
    return Plan(changes, truncated, now_cut.first_at)


async def _served(
    org: str, cut: reads.Cut, memory_keys: list[str] | None
) -> dict[Ident, list[reads.Held]]:
    out: dict[Ident, list[reads.Held]] = {}
    for h in await reads.served(
        org, cut, scope=WHOLE_NAMESPACE, memory_keys=memory_keys, include_high=True
    ):
        out.setdefault((h.agent, h.user_id, h.session_id, h.memory_key), []).append(h)
    return out


def _state(held: list[reads.Held]) -> list[str]:
    """What a reader is told: each claim's whole recorded state but its ids."""
    return sorted(
        repr(sorted((k, v) for k, v in h.a.to_json().items() if k not in _IDS)) for h in held
    )


_IDS = tm.IDENTITY_FIELDS | {"embed_input_sha256"}


async def apply(org: str, namespace: str, changes: list[KeyChange]) -> list[KeyChange]:
    """Commit each change, then project the current rows; returns the changes that
    committed. A key whose lock in force refuses the change commits nothing, nor does
    one whose earlier value a blacklist added since refuses."""
    from server.memory.app.blacklist_guard import enforce_on_insert
    from server.memory.app.temporal_projection import project_keys

    allowed = []
    for c in changes:
        try:
            for a in c.then:
                await enforce_on_insert(org=org, namespace=namespace, value=a.value)
        except Exception as e:  # noqa: BLE001 — a refused value leaves its key as it is
            log.info("namespace.rollback: %s left as it is: %s", c.ident[3], e)
            continue
        allowed.append(c)
    receipts = await publisher.publish_amendments(
        org,
        namespace,
        [(c.ident, c.memory_id, _build(c)) for c in allowed],
        operation="namespace.rollback",
    )
    committed = {r.ident for r in receipts if r.commit_seq}
    done = [c for c in allowed if c.ident in committed]
    if done:
        await project_keys(
            org, namespace, [c.ident for c in done], read_token=publisher.read_token(receipts)
        )
    return done


def _build(c: KeyChange) -> publisher.Build:
    def build(served: list[tm.Assertion]) -> tm.Amendment:
        if publisher._locked_against(served, "value" if c.then else "delete", datetime.now(UTC)):
            return tm.Amendment("rollback")
        return tm.Amendment(
            "rollback",
            tuple(a.assertion_id for a in served),
            tuple(tm.fresh(a) for a in c.then),
        )

    return build


def stub_embedding_row(
    row: store.MemoryRow,
    embedder: Any,
    *,
    version: int,
    tombstoned_at: datetime | None,
    created_at: datetime,
) -> dict[str, Any]:
    """A dimensionless embedding row for a re-emitted version row: the embedding
    worker fills its vector. `version` is the new memory row's, so a concurrent
    'high' write's fence outranks it."""
    from server.memory.domain.excerpt import build_excerpt
    from server.memory.domain.trust import attests

    serialized = row.value if isinstance(row.value, str) else json.dumps(row.value, default=str)
    return store.build_embedding_row(
        org=row.org,
        namespace=row.namespace,
        agent=row.agent,
        user_id=row.user_id,
        session_id=row.session_id,
        run_id=row.run_id or "-",
        memory_key=row.memory_key,
        memory_type=row.memory_type,
        scope="private",
        content_excerpt=build_excerpt(serialized, memory_key=row.memory_key),
        embedding_model=embedder.model_name,
        embedding_dim=0,
        embedding=[0.0] * int(embedder.dim),
        tombstoned_at=tombstoned_at,
        created_at=created_at,
        version=version,
        tags=list(row.tags or []),
        entities=list(row.entities or []),
        status=str(row.status or "active"),
        confidence=float(row.confidence),
        temporal_anchor=row.temporal_anchor,
        temporal_confidence=float(row.temporal_confidence or 0.0),
        valid_at=row.valid_at,
        valid_to=row.valid_to,
        invalid_at=row.invalid_at,
        reinforcement_count=int(getattr(row, "reinforcement_count", 1) or 1),
        source_resource_id=getattr(row, "source_resource_id", "") or "",
        source_class=getattr(row, "source_class", "") or "",
        expires_at=row.expires_at,
        files=list(getattr(row, "files", None) or []),
        concepts=list(getattr(row, "concepts", None) or []),
        # The version's attestation, if it still binds its content.
        verified=int(
            attests(
                getattr(row, "verified_by", ""),
                getattr(row, "verified_sha256", ""),
                getattr(row, "content_sha256", ""),
            )
        ),
    )
