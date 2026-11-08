"""Precedent answers for memory.ask: the pure part.

A synthesized answer is a function of the question, the request shape and the evidence
the synthesizer read. Retrieval is deterministic, so when the same question with the
same shape retrieves the same window and every memory in it still carries the
content_sha256 it had when the answer was written, the synthesizer would read
byte-identical evidence and the earlier answer stands. That is the only condition under
which a precedent is served; every other case is a miss, and the fresh synthesized
answer overwrites the row.

This module holds the key, the evidence identity and the validity checks. The
orchestration is `server.memory.app.precedents`; ClickHouse access lives in
`server.memory.store.ask_precedents`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_WS_RE = re.compile(r"\s+")
# from the run's first character only, so a long run is one pass
_TRAIL_RE = re.compile(r"(?<![\s?.!])[\s?.!]+$")
# A memory row whose content_sha256 holds the default '0' (or nothing) cannot
# have its content verified, so it never anchors a precedent.
_UNHASHED = frozenset({"", "0"})


def normalise_question(question: str) -> str:
    """Case, whitespace and trailing punctuation are not part of the question."""
    q = _WS_RE.sub(" ", (question or "").strip()).lower()
    return _TRAIL_RE.sub("", q)


def precedent_key(question: str, shape_sha256: str) -> str:
    """sha256 over the normalised question and the request-shape digest."""
    payload = f"{normalise_question(question)}\n{shape_sha256 or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Evidence:
    """(namespace, memory_key) → content_sha256 for one synthesis window."""

    hashes: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def verifiable(self) -> bool:
        return bool(self.hashes) and all(h not in _UNHASHED for h in self.hashes.values())

    def columns(self) -> tuple[list[str], list[str], list[str]]:
        items = sorted(self.hashes.items())
        return (
            [ns for (ns, _k), _h in items],
            [k for (_ns, k), _h in items],
            [h for (_ns, _k), h in items],
        )

    @classmethod
    def from_columns(cls, namespaces: list[str], keys: list[str], hashes: list[str]) -> Evidence:
        if not (len(namespaces) == len(keys) == len(hashes)):
            return cls({})
        return cls({(ns, k): h for ns, k, h in zip(namespaces, keys, hashes, strict=True)})


# Planes whose rows live in loom.memory and carry a content_sha256. Every
# other plane in a synthesis window (code_index, summary, record, chunk,
# pending turns) is hashed from the excerpt the reader actually saw.
_MEMORY_PLANES = frozenset({"", "fact", "memory"})


def _is_memory_row(h: Any) -> bool:
    return str(getattr(h, "source", "") or "") in _MEMORY_PLANES


def window_identity(window: list[Any], default_namespace: str) -> list[tuple[str, str]]:
    """The (namespace, memory_key) pairs of a synthesis window that are
    loom.memory rows, de-duplicated, in window order. Cross-namespace hits
    carry their own namespace; hits from the request's namespace carry ''.
    Rows from other planes are identified by `local_evidence` instead — a
    `code_index` row in the window is one no memory read can hash."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for h in window:
        if not _is_memory_row(h):
            continue
        key = str(getattr(h, "memory_key", "") or "")
        if not key:
            continue
        ident = (str(getattr(h, "namespace", "") or "") or default_namespace, key)
        if ident not in seen:
            seen.add(ident)
            out.append(ident)
    return out


def local_evidence(window: list[Any]) -> Evidence:
    """Evidence for the window's non-memory rows: identity `("#<source>",
    memory_key)`, hash = sha256 of the excerpt the reader saw. Computed the
    same way at store and reuse time, so a changed excerpt is a changed
    window without a lookup."""
    out: dict[tuple[str, str], str] = {}
    for h in window:
        if _is_memory_row(h):
            continue
        key = str(getattr(h, "memory_key", "") or "")
        if not key:
            continue
        ident = ("#" + str(getattr(h, "source", "") or "plane"), key)
        text = str(getattr(h, "content_excerpt", "") or "")
        out[ident] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Evidence(out)


def merge(*parts: Evidence) -> Evidence:
    out: dict[tuple[str, str], str] = {}
    for p in parts:
        out.update(p.hashes)
    return Evidence(out)


@dataclass(frozen=True)
class Precedent:
    precedent_key: str
    question: str
    answer: str
    cited_keys: list[str]
    evidence: Evidence
    synth_model: str = ""
    reuse_count: int = 0
    created_at: datetime | None = None
    # When the window this answer was written from was retrieved (the fast
    # path reads it). None / epoch when the row carries none: those never take
    # the fast path.
    retrieved_at: datetime | None = None


# Planes whose rows a namespace write mark covers: the memory
# rows themselves plus the local-evidence planes fed by materialized views.
# A stored window with a row from any other plane (code_index, observation,
# entity_summary, …) cannot be proven unchanged without retrieving.
FAST_COVERED_PLANES: frozenset[str] = frozenset({"#summary", "#chunk", "#record"})

_EPOCH_GUARD = datetime(1971, 1, 1, tzinfo=UTC)


def fast_eligibility(
    prec: Precedent, *, now: datetime | None = None, max_age_hours: int = 1
) -> str:
    """'' when `prec` may be served without a fresh retrieval, given that no
    plane it reads has moved; otherwise the reason it may not. The mark
    check itself is `marks_block`; this is what can be decided from the row.

    The age bound is not staleness (the marks answer that); it bounds rank
    drift. A retrieval taken hours later can order the same rows differently
    (recency, usage), and retrieval determinism was measured across minutes.
    Repeats recur at p50 38 s and p90 27 min, so one hour covers the
    traffic and keeps the window inside the measured-deterministic span."""
    if not (prec.answer or "").strip():
        return "empty_answer"
    if not prec.evidence.verifiable:
        return "stored_evidence_unverifiable"
    if prec.retrieved_at is None or prec.retrieved_at < _EPOCH_GUARD:
        return "no_retrieved_at"
    if max_age_hours > 0:
        now = now or datetime.now(UTC)
        if (now - prec.retrieved_at).total_seconds() > max_age_hours * 3600:
            return "rank_drift_bound"
    for ns, _key in prec.evidence.hashes:
        if ns.startswith("#") and ns not in FAST_COVERED_PLANES:
            return f"uncovered_plane:{ns[1:]}"
    return ""


def fast_namespaces(prec: Precedent, namespace: str) -> list[str]:
    """The namespaces whose marks must predate the stored retrieval: the
    request's own (local-evidence rows live there) and every namespace a
    memory row in the window came from."""
    out = {namespace}
    for ns, _key in prec.evidence.hashes:
        if ns and not ns.startswith("#"):
            out.add(ns)
    return sorted(out)


def marks_block(
    prec: Precedent,
    marks: dict[str, datetime],
    *,
    namespaces: list[str],
    blacklist_at: datetime | None = None,
    org_wide: str = "*",
) -> str:
    """'' when every plane the window reads last moved BEFORE the stored
    retrieval; otherwise which mark blocks. A namespace with no mark has
    had no recorded write. The org-wide sentinel covers writes whose
    namespace the writer did not know (a user erasure); the blacklist
    watermark covers the one filter that changes a window without a row."""
    at = prec.retrieved_at
    if at is None:
        return "no_retrieved_at"
    for ns in [*namespaces, org_wide]:
        mark = marks.get(ns)
        if mark is not None and mark >= at:
            return f"moved:{ns}"
    if blacklist_at is not None and blacklist_at >= at:
        return "moved:blacklist"
    return ""


def validity(
    prec: Precedent,
    fresh: Evidence,
    *,
    window: list[tuple[str, str]] | None = None,
    now: datetime | None = None,
    max_age_hours: int = 0,
) -> str:
    """'' when the precedent may be served; otherwise the reason it may not.

    Served only when the fresh window is the SAME key set as the stored one
    and every key carries the stored hash — the synthesizer would read the
    same bytes. A key that vanished (tombstoned, superseded, pushed out of
    the window), a key that arrived, or an edited key is a miss."""
    if not (prec.answer or "").strip():
        return "empty_answer"
    if not prec.evidence.verifiable:
        return "stored_evidence_unverifiable"
    if max_age_hours > 0 and prec.created_at is not None:
        now = now or datetime.now(UTC)
        created = prec.created_at if prec.created_at.tzinfo else prec.created_at.replace(tzinfo=UTC)
        if (now - created).total_seconds() > max_age_hours * 3600:
            return "expired"
    if not fresh.hashes:
        return "empty_window"
    stored, live = prec.evidence.hashes, fresh.hashes
    # `window` is what retrieval returned; `live` is what still has a live
    # row. A window key with no live row (tombstoned, orphaned vector) is a
    # changed window, not a matched one.
    if set(stored) != set(window if window is not None else live):
        return "window_changed"
    for ident, h in stored.items():
        if ident not in live:
            return "row_gone"
        if live[ident] != h:
            return "content_changed"
    return ""
