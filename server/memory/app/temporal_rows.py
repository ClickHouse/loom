"""The journal's assertions as projection rows, and back: the one mapping the
publisher, replay and reads share."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.memory.domain import temporal_model as tm

Ident = tuple[str, str, str, str]


@dataclass
class Served:
    """One served assertion with its projection row columns."""

    a: tm.Assertion
    memory_id: str
    born_seq: int
    born_at: datetime


def ident_of(r: Mapping[str, Any]) -> Ident:
    return (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"]))


def served_from_row(r: Mapping[str, Any]) -> Served:
    a = assertion_from_projection(r)
    return Served(a, str(r["memory_id"]), int(r["born_seq"]), tm.utc(r["born_at"]) or r["born_at"])


def _text(v: Any) -> str:
    """A FixedString column comes back as bytes."""
    return v.decode() if isinstance(v, bytes) else str(v or "")


def assertion_from_projection(r: Mapping[str, Any]) -> tm.Assertion:
    parent = r.get("parent_assertion_id")
    return tm.Assertion(
        assertion_id=str(r["assertion_id"]),
        origin_assertion_id=str(r["origin_assertion_id"]),
        parent_assertion_id=str(parent) if parent else None,
        value=str(r["value"]),
        memory_type=str(r["memory_type"]),
        temporal_kind=str(r["temporal_kind"]),
        validity_basis=str(r["validity_basis"]),
        valid_from=tm.utc(r.get("valid_from")),
        valid_to=tm.utc(r.get("valid_to")),
        valid_text=str(r.get("valid_text") or ""),
        valid_precision=str(r.get("valid_precision") or ""),
        valid_tz=str(r.get("valid_tz") or ""),
        modality=str(r.get("modality") or "asserted"),
        status=str(r.get("status") or "active"),
        confidence=float(r.get("confidence") or 0.0),
        expires_at=tm.utc(r.get("expires_at")),
        pinned=bool(r.get("pinned")),
        lock_mode=str(r.get("lock_mode") or ""),
        lock_until=tm.utc(r.get("lock_until")),
        verified_by=str(r.get("verified_by") or ""),
        verified_at=tm.utc(r.get("verified_at")),
        verified_sha256=str(r.get("verified_sha256") or ""),
        grounding=str(r.get("grounding") or ""),
        source_class=str(r.get("source_class") or ""),
        actor=str(r.get("actor") or ""),
        sensitivity=str(r.get("sensitivity") or ""),
        source_version=int(r.get("source_version") or 0),
        embed_input_sha256=_text(r.get("embed_input_sha256")),
        qualifiers=dict(r.get("qualifiers") or {}),
        tags=tuple(r.get("tags") or ()),
        entities=tuple(r.get("entities") or ()),
        evidence_ids=tuple(r.get("evidence_ids") or ()),
        derived_from=tuple(r.get("derived_from") or ()),
    )


def projection_row(
    org: str,
    ns: str,
    scope: str,
    ident: Ident,
    s: Served,
    projected_seq: int,
    *,
    retired: tuple[int, datetime] | None = None,
) -> dict[str, Any]:
    a = s.a
    agent, uid, sid, key = ident
    return {
        "org": org,
        "namespace": ns,
        "scope": scope,
        "agent": agent,
        "user_id": uid,
        "session_id": sid,
        "memory_key": key,
        "memory_id": s.memory_id,
        "assertion_id": a.assertion_id,
        "parent_assertion_id": a.parent_assertion_id,
        "origin_assertion_id": a.origin_assertion_id,
        "born_seq": s.born_seq,
        "born_at": s.born_at,
        "retired_seq": retired[0] if retired else None,
        "retired_at": retired[1] if retired else None,
        "projected_seq": projected_seq,
        "source_version": a.source_version,
        "memory_type": a.memory_type,
        "value": a.value,
        "value_sha256": a.value_sha256,
        "embed_input_sha256": a.embed_input_sha256 or "0" * 64,
        "temporal_kind": a.temporal_kind,
        "validity_basis": a.validity_basis,
        "valid_from": a.valid_from,
        "valid_to": a.valid_to,
        "valid_text": a.valid_text,
        "valid_precision": a.valid_precision,
        "valid_tz": a.valid_tz,
        "modality": a.modality,
        "status": a.status,
        "confidence": a.confidence,
        "expires_at": a.expires_at,
        "pinned": int(a.pinned),
        "lock_mode": a.lock_mode,
        "lock_until": a.lock_until,
        "verified_by": a.verified_by,
        "verified_at": a.verified_at,
        "verified_sha256": a.verified_sha256,
        "grounding": a.grounding,
        "source_class": a.source_class,
        "actor": a.actor,
        "sensitivity": a.sensitivity,
        "qualifiers": dict(a.qualifiers),
        "tags": list(a.tags),
        "entities": list(a.entities),
        "evidence_ids": list(a.evidence_ids),
        "derived_from": list(a.derived_from),
    }
