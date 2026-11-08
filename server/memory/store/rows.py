"""Row shapes of the loom.memory table family: the leaf every other module
of the package imports.

The typed read shape (`Identity`, `MemoryRow`, `_hydrate`), the column
registries and the version-row builders — `build_memory_version_row`,
`build_embedding_row`, `build_session_summary_row`,
`build_entity_summary_row` — are the single source of truth for the columns
a ReplacingMergeTree(version) write must carry forward. No ClickHouse I/O
happens here.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from server.core.datetimes import naive_utc_now
from server.core.hashing import hash_text
from server.core.rmt_carry import register
from server.memory.domain.delete_marks import carried_trace
from server.memory.domain.expire_reasons import carried_reason

# A memory's key: (org, namespace, agent, user_id, session_id, memory_key).
IdentityKey = tuple[str, str, str, str, str, str]
# (agent, user_id, session_id, memory_key): an IdentityKey within one namespace.
KeyInNamespace = tuple[str, str, str, str]


IDENTITY_COLUMNS = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")


def row_field(row: Any, name: str) -> Any:
    """A column of a MemoryRow, or of anything indexed by column name (a dict, a driver
    record)."""
    return getattr(row, name) if isinstance(row, BaseModel) else row[name]


def identity_key(row: Any) -> IdentityKey:
    """The (org, namespace, agent, user_id, session_id, memory_key) of a MemoryRow, a dict
    or a driver record."""
    org, ns, agent, uid, sid, key = (str(row_field(row, c)) for c in IDENTITY_COLUMNS)
    return org, ns, agent, uid, sid, key


def key_in_namespace(row: Any) -> KeyInNamespace:
    """The (agent, user_id, session_id, memory_key) of a row, as `identity_key` reads it."""
    agent, uid, sid, key = (str(row_field(row, c)) for c in IDENTITY_COLUMNS[2:])
    return agent, uid, sid, key


class Identity(BaseModel):
    """The five-field identity tuple every loom.memory row carries.

    `user_id`, `session_id` default to '-' so callers can omit them
    for the common per-agent case.
    """

    model_config = ConfigDict(frozen=True)

    org: str
    namespace: str
    agent: str
    user_id: str = "-"
    session_id: str = "-"


class MemoryRow(BaseModel):
    """One row of loom.memory in its read shape.

    `value` is the deserialized JSON (already parsed from the stored
    string), not the raw bytes. Callers get usable Python objects, not
    strings to json.loads themselves.
    """

    model_config = ConfigDict(frozen=True)

    memory_id: str
    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str
    memory_type: str
    value: Any
    confidence: float
    version: int
    pinned: bool
    created_at: datetime
    # Last write to this memory_key; preserved-across-updates created_at is
    # distinct. Falls back to created_at for a row without the column
    # (memory_embeddings, or a projection that does not select it).
    updated_at: datetime | None = None
    expires_at: datetime | None
    # When the row was soft-deleted. None for live rows.
    tombstoned_at: datetime | None = None
    # Real-world time the fact became true (vs created_at = when recorded).
    # Drives the as-of read path. NULL when neither caller nor extractor set it.
    valid_at: datetime | None = None
    # Real-world time the fact STOPPED being true; NULL = open interval.
    valid_to: datetime | None = None
    # Record-axis retirement stamp: when a supersession retired this row.
    # Distinct from valid_to (the fact's own end) and tombstoned_at (deletion).
    invalid_at: datetime | None = None
    # Server-enforced character cap on `value` (NULL = no cap).
    char_limit: int | None = None
    run_id: str = "-"
    source_trace: str = ""
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    status: str = "active"
    # The stored `value` column VERBATIM — the bytes the writer serialised —
    # so a version row (tombstone, restore, touch, lock,
    # rollback) carries them unchanged. `value` above is the parsed form,
    # which cannot be written back: a JSON-string value parses to a `str`
    # that a raw write drops one encoding level from, and an object re-dumps
    # with other whitespace / key order. "" when the read did not carry the
    # text (a preview page, a hand-built row); the builder then serialises
    # `value` the way memory.set does.
    value_text: str = ""
    # SHA-256 of the canonical JSON value; preserved across RMT version rows.
    content_sha256: str = ""
    # Set by a preview read (list_by_namespace(preview=True)) when the stored
    # value is longer than the 2 KB preview `value` then carries.
    value_truncated: bool = False
    recall_count: int = 0
    last_recalled_at: datetime | None = None
    value_json: str = "{}"
    redacted_at: datetime | None = None
    redaction_reason: str = ""
    conflict_with: list[str] = Field(default_factory=list)
    duplicate_of: str = ""
    evidence_count: int = 1
    cross_promoted_from_memory_id: str = ""
    cross_promoted_from_namespace: str = ""
    superseded_by: str = ""
    rejection_reason: str = ""
    temporal_anchor: datetime | None = None
    # How precisely the source pinned temporal_anchor (0..1); 0 = unscored.
    temporal_confidence: float = 0.0
    reinforcement_count: int = 1
    last_reinforced_at: datetime | None = None
    mentioned_at: datetime | None = None
    source_resource_id: str = ""
    # Attestation, bound to content: `<kind>:<subject>` actor, when, and the
    # canonical hash attested to. State (human/process/stale/none) is DERIVED
    # against the current hash — see server/memory/domain/trust.py.
    verified_by: str = ""
    verified_at: datetime | None = None
    # Sensitivity tier: '' | low | medium | high.
    sensitivity: str = ""
    verified_sha256: str = ""
    # Grounding grade + verbatim quote; "" = ungraded.
    grounding: str = ""
    evidence: str = ""
    # Who wrote this version (`human:<subject>` / `process:<subject>`, kind from
    # the token) and how the content entered memory (asserted | extracted |
    # observed | derived | imported | external).
    actor: str = ""
    source_class: str = ""
    # Write-time lock. '' = unlocked; a past lock_until means
    # the lock lapsed. Read through
    # server/memory/domain/lock.effective_mode, never the raw column.
    lock_mode: str = ""
    lock_reason: str = ""
    lock_until: datetime | None = None
    locked_by: str = ""
    locked_at: datetime | None = None
    # What happens when expires_at passes:
    # '' / 'tombstone' | 'demote' | 'anonymize'. Applied by the expiry sweep.
    on_expire: str = ""
    expire_reason: str = ""
    # Code-native facets: file paths the fact references + code concepts
    # (functions / symbols / libraries / error tokens).
    files: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)

    @classmethod
    def from_snapshot(cls, source: Any, **overrides: Any) -> MemoryRow:
        """A MemoryRow from any object or mapping that carries loom.memory
        columns by name (a snapshot payload row, a restore/import record).
        Columns the source lacks take the model default — `created_at`
        falls back to now — so `build_memory_version_row` can then carry
        EVERY persisted column into the insert, `created_at` included. `value` is
        kept as given: a serialized JSON string becomes `value_text` and is
        written verbatim by the builder, a parsed object is dumped the way
        memory.set dumps it."""

        def get(name: str, default: Any = None) -> Any:
            if isinstance(source, Mapping):
                return source.get(name, default)
            return getattr(source, name, default)

        data: dict[str, Any] = {}
        for name in cls.model_fields:
            if name in overrides:
                data[name] = overrides[name]
                continue
            val = get(name, None)
            if val is not None:
                data[name] = val
        for name in ("org", "namespace", "agent", "memory_key", "memory_type"):
            data.setdefault(name, "")
        for name in ("user_id", "session_id"):
            data.setdefault(name, "-")
        data.setdefault("memory_id", str(uuid.uuid4()))
        data.setdefault("value", "")
        # A snapshot / history payload carries `value` as the stored text:
        # keep it as the bytes the builder writes.
        if isinstance(data.get("value"), str) and not data.get("value_text"):
            data["value_text"] = data["value"]
        data.setdefault("confidence", 1.0)
        data.setdefault("version", 0)
        data.setdefault("pinned", False)
        data.setdefault("created_at", naive_utc_now())
        data.setdefault("expires_at", None)
        if "cross_promoted_from_memory_id" in data:
            lineage = uuid_or_none(data["cross_promoted_from_memory_id"])
            data["cross_promoted_from_memory_id"] = lineage or ""
        return cls(**data)


# Canonical column list for loom.memory — single source of truth for
# build_memory_version_row. Omitting a column on a follow-up version row
# silently zeros it under FINAL.
MEMORY_PERSISTED_COLS: tuple[str, ...] = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "memory_type",
    "value",
    "value_json",
    "confidence",
    "source_trace",
    "version",
    "tombstoned_at",
    "created_at",
    "expires_at",
    "pinned",
    "run_id",
    "entities",
    "memory_id",
    "redacted_at",
    "redaction_reason",
    "status",
    "conflict_with",
    "duplicate_of",
    "evidence_count",
    "recall_count",
    "last_recalled_at",
    "cross_promoted_from_memory_id",
    "cross_promoted_from_namespace",
    "tags",
    "superseded_by",
    "rejection_reason",
    "temporal_anchor",
    "temporal_confidence",
    "updated_at",
    "valid_at",
    "valid_to",
    "invalid_at",
    "char_limit",
    "reinforcement_count",
    "last_reinforced_at",
    "mentioned_at",
    "source_resource_id",
    "content_sha256",
    "files",
    "concepts",
    "verified_by",
    "verified_at",
    "verified_sha256",
    "grounding",
    "evidence",
    # Sensitivity tier: '' | low | medium | high.
    "sensitivity",
    "actor",
    "source_class",
    "lock_mode",
    "lock_reason",
    "lock_until",
    "locked_by",
    "locked_at",
    "on_expire",
    "expire_reason",
)


# String columns that are non-Nullable in loom.memory and may be absent from
# an older MemoryRow: default to "" rather than None on re-emit.
_NON_NULLABLE_STRING_COLS = frozenset(
    {
        "grounding",
        "evidence",
        "verified_by",
        "verified_sha256",
        "source_trace",
        "sensitivity",
        "redaction_reason",
        "rejection_reason",
        "source_resource_id",
        "cross_promoted_from_namespace",
        "superseded_by",
        "actor",
        "source_class",
        "lock_mode",
        "lock_reason",
        "locked_by",
        "on_expire",
        "expire_reason",
    }
)


# `cross_promoted_from_memory_id` is Nullable(UUID). An empty string is NOT
# a null to ClickHouse: it parses to the zero UUID, which then reads back as
# a real (dangling) lineage pointer. Every write path funnels the column
# through uuid_or_none so the row lands NULL.
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def uuid_or_none(value: Any) -> str | None:
    """Normalise a UUID-ish value for a Nullable(UUID) column: None for
    missing / empty / zero, else its string form."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == ZERO_UUID:
        return None
    return text


def serialize_value(value: Any) -> str:
    """The `value` column bytes memory.set writes for a Python value: the
    same `json.dumps(value)` call (default separators, insertion order, no
    `default=str`), so a row built from a genuinely new value matches what a
    set of that value would have written."""
    return json.dumps(value)


def stored_value_text(row: Any) -> str:
    """The `value` column text a version row of `row` must carry: the stored
    bytes verbatim when the read carried them (`value_text`), else the
    memory.set serialisation of the parsed value.

    A version row built from the parsed value would write a JSON-string
    value back RAW (one encoding level lost, JSONType String → Object, the
    content hash changed) and re-dump object values with other whitespace
    and key order, so a restored row would differ from what the agent wrote."""
    text = getattr(row, "value_text", "") or ""
    if text:
        return str(text)
    return serialize_value(getattr(row, "value", None))


def written_now() -> datetime:
    """`updated_at` for a version a caller meant to write:
    update, lock/unlock, tombstone, restore, apply label/demote. The builder
    below carries `updated_at` forward by default, which is right for
    maintenance (backfills, repair, recall touches) and wrong for an edit,
    which would answer "last changed" with its first write's time."""
    return naive_utc_now()


def build_memory_version_row(
    existing: MemoryRow,
    **overrides: Any,
) -> dict[str, Any]:
    """Construct an INSERT dict for loom.memory that carries every column
    from `existing` forward, applying `overrides`. Use for every write that
    mutates an existing memory key: tombstone, restore, touch, update_by_id,
    promote, demote. Single source of truth: MEMORY_PERSISTED_COLS.
    """
    row: dict[str, Any] = {}
    for col in MEMORY_PERSISTED_COLS:
        if col in overrides:
            row[col] = overrides[col]
            continue
        val = getattr(existing, col, None)
        if col == "expire_reason":
            # A blacklist sweep's marker stays on the sweep's tombstone (the sweep and
            # namespace.rollback pass it as an override); every other version drops it
            # (write_fence).
            val = carried_reason(val)
        elif col == "source_trace":
            # memory.delete_version's marker likewise stays on its own tombstone.
            val = carried_trace(val)
        elif col == "value":
            # The stored bytes verbatim, never the parsed value written back
            # (see `stored_value_text`).
            val = stored_value_text(existing)
        elif col == "pinned":
            val = 1 if val else 0
        elif col in ("tags", "entities", "conflict_with", "files", "concepts"):
            val = list(val or [])
        elif col == "cross_promoted_from_memory_id":
            val = uuid_or_none(val)
        elif col in _NON_NULLABLE_STRING_COLS and val is None:
            # A column added after this row was read (or a MemoryRow built
            # from an older SELECT) must land the schema default, not None:
            # loom.memory's String columns are non-Nullable and the insert
            # would fail the whole tombstone / restore / touch.
            val = ""
        row[col] = val
    for k in overrides:
        if k not in MEMORY_PERSISTED_COLS:
            row[k] = overrides[k]
    return row


# A revision is a NEW value written over the live row of the same identity and
# key (memory.set, memory.bulk_set). Every column the new value does not set is
# carried from the live row — usage (recall_count, evidence_count,
# last_recalled_at), lineage (cross_promoted_from_*), identity, lock, expiry —
# except the columns below, which describe the OLD value or its standing and
# would mis-mark the new one:
REVISION_RESETS: dict[str, Any] = {
    # The verdicts on the old value: the write's own conflict check sets the
    # new value's (a revived superseded / rejected / duplicate row is live again).
    "status": "active",
    "conflict_with": [],
    "duplicate_of": "",
    "superseded_by": "",
    "rejection_reason": "",
    "invalid_at": None,
    # A redaction replaced the old value's bytes; the new value is not redacted.
    "redacted_at": None,
    "redaction_reason": "",
    # Read out of the old value's text: its quote, and the date it was about.
    "grounding": "",
    "evidence": "",
    "temporal_anchor": None,
    "temporal_confidence": 0.0,
    # The resource the old value was extracted from.
    "source_resource_id": "",
}
# A first write carries no attestation. A revision keeps the live row's
# attestation even when it changes the content: verified_sha256 names the
# bytes that were attested, so the new value reads "stale" (who attested and
# when stay visible, namespace.validate reports it, memory.verify re-binds
# it). Readers bind trust to the content hash through verification_state,
# never to verified_by alone.
_ATTESTATION_RESETS: dict[str, Any] = {
    "verified_by": "",
    "verified_at": None,
    "verified_sha256": "",
}


def build_revision_row(existing: MemoryRow, new: Mapping[str, Any]) -> dict[str, Any]:
    """The version row of a revision: `new` (the columns the write sets) over
    the live row `existing`, through `build_memory_version_row`, with
    REVISION_RESETS applied to every column `new` does not set. The
    attestation is carried (a changed value reads "stale"). See
    REVISION_RESETS for the rule, column by column."""
    # Fresh lists per row: a caller appends to conflict_with after the build.
    overrides: dict[str, Any] = {
        c: (list(v) if isinstance(v, list) else v)
        for c, v in REVISION_RESETS.items()
        if c not in new
    }
    overrides.update(new)
    return build_memory_version_row(existing, **overrides)


# The DDL default of every column a first write may leave unset: what
# ClickHouse would fill in, written out so every first version carries the full
# column set the insert funnel checks. `updated_at` / `last_reinforced_at` /
# `created_at` (DEFAULT now()) are the caller's `now`; `memory_id` (DEFAULT
# generateUUIDv4()) is a fresh UUID. The identity, the value, `version`,
# `content_sha256`, `actor` and `source_class` have no default here: a first
# write that omits one is refused by `check_memory_rows`.
FIRST_VERSION_DEFAULTS: dict[str, Any] = {
    **REVISION_RESETS,
    **_ATTESTATION_RESETS,
    "cross_promoted_from_memory_id": None,
    "cross_promoted_from_namespace": "",
    "evidence_count": 1,
    "recall_count": 0,
    "last_recalled_at": None,
    "reinforcement_count": 1,
    "mentioned_at": None,
    "source_trace": "",
    "tombstoned_at": None,
    "expires_at": None,
    "pinned": 0,
    "run_id": "-",
    "entities": [],
    "tags": [],
    "files": [],
    "concepts": [],
    "valid_at": None,
    "valid_to": None,
    "char_limit": None,
    "sensitivity": "",
    "lock_mode": "",
    "lock_reason": "",
    "lock_until": None,
    "locked_by": "",
    "locked_at": None,
    "on_expire": "",
    "expire_reason": "",
}


def build_first_version_row(new: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """`new` completed to every persisted column with the DDL defaults of the
    ones it does not set (FIRST_VERSION_DEFAULTS; `updated_at` and
    `last_reinforced_at` = `now`)."""
    row: dict[str, Any] = {
        c: (list(v) if isinstance(v, list) else v)
        for c, v in FIRST_VERSION_DEFAULTS.items()
        if c not in new
    }
    row.setdefault("updated_at", now)
    row.setdefault("last_reinforced_at", now)
    row.setdefault("created_at", now)
    row.update(new)
    if not row.get("memory_id"):
        row["memory_id"] = str(uuid.uuid4())
    return row


# Canonical column list for loom.memory_embeddings — the mirror table. Every
# searchable write funnels through build_embedding_row so no path silently omits
# a column (omitting one lands the schema DEFAULT, which FINAL then surfaces over
# the prior real value). MATERIALIZED columns (content_excerpt_lc / _stem) are
# CH-derived and intentionally absent. content_excerpt_lemma is populated
# app-side (lemmagen3) — see build_embedding_row — so the lemma lexical column
# needs no server-side <lemmatizers> dictionary (unavailable on ClickHouse
# Cloud). Defaults mirror the DDL.
EMBEDDING_PERSISTED_COLS: dict[str, Any] = {
    "content_excerpt_lemma": "",
    "org": "",
    "namespace": "",
    "agent": "",
    "user_id": "-",
    "session_id": "-",
    "memory_key": "",
    "memory_type": "semantic",
    "scope": "private",
    "content_excerpt": "",
    "embedding_model": "",
    "embedding_dim": 0,
    "embedding": [],
    "tombstoned_at": None,
    "created_at": None,
    "version": 0,
    "embed_attempts": 0,
    "embed_last_error": "",
    "embed_failed_at": None,
    "embed_retry_at": None,
    "run_id": "-",
    "entities": [],
    "conflict_checked_at": None,
    # The content_sha256 of the write this row mirrors; '' on a row stored without one.
    "content_sha256": "",
    "tags": [],
    "status": "active",
    "confidence": 1.0,
    "temporal_anchor": None,
    # Grade of temporal_anchor; 0 = unscored.
    "temporal_confidence": 0.0,
    "valid_at": None,
    # Closing bound of the fact's stated validity; NULL = open interval.
    "valid_to": None,
    # When a contradicting newer fact superseded this one (distinct from
    # valid_to, the fact's OWN stated end); NULL = not invalidated.
    "invalid_at": None,
    "memory_key_embedding": [],
    "memory_key_embedding_dim": 0,
    "reinforcement_count": 1,
    "last_reinforced_at": None,
    "mentioned_at": None,
    "source_resource_id": "",
    "expires_at": None,
    # How the content entered memory; mirrored for ranking.
    "source_class": "",
    # 1 while the row's attestation is live; ranking term.
    "verified": 0,
    "files": [],
    "concepts": [],
}


# Non-nullable DateTime columns on loom.memory_embeddings — must never be NULL.
_EMBEDDING_NONNULL_DATETIMES: tuple[str, ...] = ("created_at", "last_reinforced_at")


def build_embedding_row(**overrides: Any) -> dict[str, Any]:
    """Construct a complete loom.memory_embeddings INSERT row.

    Single source of truth: EMBEDDING_PERSISTED_COLS. An unknown override key
    raises (a typo'd column name is a bug, not a silently dropped value).
    """
    unknown = [k for k in overrides if k not in EMBEDDING_PERSISTED_COLS]
    if unknown:
        raise ValueError(f"build_embedding_row: unknown columns {unknown}")
    row: dict[str, Any] = dict(EMBEDDING_PERSISTED_COLS)
    row.update(overrides)
    # Populate the lemma lexical column app-side (lemmagen3) rather than via a
    # CH `lemmatize('en', ...)` MATERIALIZED expression, so the column needs no
    # server-side <lemmatizers> dictionary (unavailable on ClickHouse Cloud).
    # Same 'en' model the query side uses, so stored and query lemmas agree.
    if not row.get("content_excerpt_lemma"):
        from server.memory.domain.lemmatize import lemmagen_lemma_text

        row["content_excerpt_lemma"] = lemmagen_lemma_text(row.get("content_excerpt") or "")
    now = naive_utc_now()
    for col in _EMBEDDING_NONNULL_DATETIMES:
        if row.get(col) is None:
            row[col] = now
    # RMT(version) merge key, distinct from created_at so a re-embed wins the
    # merge without overwriting the original creation time. Callers may pin it.
    if not row.get("version"):
        row["version"] = next_version()
    return row


def search_document_row(
    memory_row: Mapping[str, Any],
    *,
    embedding_model: str,
    embedding: list[float],
    key_embedding: list[float],
    key_text: str,
    excerpt: str,
    conflict_checked_at: datetime | None = None,
) -> dict[str, Any]:
    """The loom.memory_embeddings row of one loom.memory version row: its search
    document. Every persisted column is filled, so a later version cannot resurrect a
    default over a real value, and its version is the memory row's, so a newer write's
    retire or fence outranks it even when it lands late."""
    from server.memory.domain.trust import attests

    dim = len(embedding)
    return build_embedding_row(
        org=memory_row["org"],
        namespace=memory_row["namespace"],
        agent=memory_row["agent"],
        user_id=memory_row["user_id"],
        session_id=memory_row["session_id"],
        run_id=memory_row["run_id"],
        memory_key=memory_row["memory_key"],
        memory_type=memory_row["memory_type"],
        scope="private",
        content_excerpt=excerpt,
        source_class=memory_row["source_class"],
        embedding_model=embedding_model,
        embedding_dim=dim,
        embedding=embedding,
        memory_key_embedding=key_embedding,
        memory_key_embedding_dim=dim if key_text else 0,
        confidence=float(memory_row["confidence"]),
        tombstoned_at=None,
        created_at=naive_utc_now(),
        tags=list(memory_row.get("tags") or []),
        files=memory_row["files"],
        entities=memory_row["entities"],
        source_resource_id=memory_row["source_resource_id"],
        valid_at=memory_row.get("valid_at"),
        valid_to=memory_row.get("valid_to"),
        invalid_at=memory_row.get("invalid_at"),
        expires_at=memory_row.get("expires_at"),
        reinforcement_count=memory_row["reinforcement_count"],
        last_reinforced_at=memory_row["last_reinforced_at"],
        # The lifecycle status from the first version: a `duplicate` verdict the
        # memory row holds must not be served as `active` by the search pool.
        status=str(memory_row.get("status") or "active"),
        conflict_checked_at=conflict_checked_at,
        content_sha256=str(memory_row.get("content_sha256") or ""),
        version=int(memory_row["version"]),
        # A revision of unchanged content keeps its attestation; the mirror says so.
        verified=int(
            attests(
                memory_row.get("verified_by"),
                memory_row.get("verified_sha256"),
                memory_row.get("content_sha256"),
            )
        ),
    )


# Canonical column list for loom.session_summaries. Same RMT(version) mirror
# discipline as loom.memory_embeddings; defaults mirror the DDL.
SESSION_SUMMARY_PERSISTED_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "agent": "-",
    "user_id": "-",
    "session_id": "",
    "run_id": "-",
    "summary_text": "",
    "summary_embedding": [],
    "embedding_model": "",
    "embedding_dim": 0,
    "key_facts": [],
    "derived_from": [],
    "valid_at": None,
    "created_at": None,
    "tombstoned_at": None,
    "version": 0,
}


# Non-nullable DateTime columns on loom.session_summaries — must never be NULL.
_SESSION_SUMMARY_NONNULL_DATETIMES: tuple[str, ...] = ("created_at",)


def build_session_summary_row(**overrides: Any) -> dict[str, Any]:
    """Construct a complete loom.session_summaries INSERT row.

    Single source of truth: SESSION_SUMMARY_PERSISTED_COLS. An unknown override
    key raises (a typo'd column name is a bug, not a silently dropped value).
    """
    unknown = [k for k in overrides if k not in SESSION_SUMMARY_PERSISTED_COLS]
    if unknown:
        raise ValueError(f"build_session_summary_row: unknown columns {unknown}")
    row: dict[str, Any] = dict(SESSION_SUMMARY_PERSISTED_COLS)
    row.update(overrides)
    now = naive_utc_now()
    for col in _SESSION_SUMMARY_NONNULL_DATETIMES:
        if row.get(col) is None:
            row[col] = now
    # RMT(version) merge key so a re-ingest of the same session wins the merge.
    if not row.get("version"):
        row["version"] = next_version()
    return row


# Canonical column list for loom.entity_summaries (per-entity offline summaries).
# Same RMT(version) mirror discipline as loom.memory_embeddings — omitting a
# column lands the schema DEFAULT, which FINAL surfaces over the prior real
# value. Defaults mirror the DDL.
ENTITY_SUMMARY_PERSISTED_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "agent": "-",
    "user_id": "-",
    "entity": "",
    "summary_text": "",
    "summary_embedding": [],
    "embedding_model": "",
    "embedding_dim": 0,
    "key_facts": [],
    "fact_count": 0,
    "valid_from": None,
    "updated_at": None,
    "created_at": None,
    "tombstoned_at": None,
    "version": 0,
}


# Non-nullable DateTime columns on loom.entity_summaries — must never be NULL.
_ENTITY_SUMMARY_NONNULL_DATETIMES: tuple[str, ...] = ("updated_at", "created_at")


def build_entity_summary_row(**overrides: Any) -> dict[str, Any]:
    """Construct a complete loom.entity_summaries INSERT row.

    Single source of truth: ENTITY_SUMMARY_PERSISTED_COLS. An unknown override
    key raises (a typo'd column name is a bug, not a silently dropped value).
    """
    unknown = [k for k in overrides if k not in ENTITY_SUMMARY_PERSISTED_COLS]
    if unknown:
        raise ValueError(f"build_entity_summary_row: unknown columns {unknown}")
    row: dict[str, Any] = dict(ENTITY_SUMMARY_PERSISTED_COLS)
    row.update(overrides)
    now = naive_utc_now()
    for col in _ENTITY_SUMMARY_NONNULL_DATETIMES:
        if row.get(col) is None:
            row[col] = now
    # ReplacingMergeTree(version): stamp the merge-ordering key so a re-run for the
    # same entity (same ORDER BY tuple) wins the merge as the fresh row.
    if not row.get("version"):
        row["version"] = next_version()
    return row


def parse_memory_value(raw: Any) -> Any:
    """Parse a stored memory ``value``.

    The column holds JSON for non-string values but the RAW string for string
    values (see ``build_memory_version_row``) — so a bare assertion like
    ``Alice likes coffee`` is not valid JSON. Fall back to the raw string
    instead of raising: a non-JSON value must never 500 a list/get/conflict
    read. ``None``/empty → ``None``.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return raw


def _hydrate(row: dict[str, Any]) -> MemoryRow:
    """Convert a CH row dict into a typed MemoryRow."""
    value = parse_memory_value(row.get("value"))
    # The stored text — `value_text` when the projection
    # aliases it (the FINAL projection renders `value` from value_json), the
    # raw `value` column otherwise (`SELECT *`, the versions mirror). A
    # preview page carries a truncated value and no text at all.
    raw_text = row["value_text"] if "value_text" in row else row.get("value")
    if row.get("value_truncated") or raw_text is None:
        value_text = ""
    elif isinstance(raw_text, bytes):
        value_text = raw_text.decode("utf-8", errors="replace")
    else:
        value_text = str(raw_text)
    return MemoryRow(
        memory_id=str(row["memory_id"]),
        org=row["org"],
        namespace=row["namespace"],
        agent=row["agent"],
        user_id=row["user_id"],
        session_id=row["session_id"],
        memory_key=row["memory_key"],
        memory_type=row["memory_type"],
        value=value,
        value_text=value_text,
        value_truncated=bool(row.get("value_truncated") or False),
        confidence=float(row["confidence"]),
        version=int(row["version"]),
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        # memory_embeddings rows and narrow projections carry no updated_at.
        updated_at=row.get("updated_at") or row["created_at"],
        expires_at=row.get("expires_at"),
        tombstoned_at=row.get("tombstoned_at"),
        valid_at=row.get("valid_at"),
        valid_to=row.get("valid_to"),
        invalid_at=row.get("invalid_at"),
        char_limit=(int(row["char_limit"]) if row.get("char_limit") is not None else None),
        run_id=row.get("run_id", "-") or "-",
        source_trace=row.get("source_trace", "") or "",
        status=row.get("status", "active"),
        content_sha256=hash_text(row.get("content_sha256")),
        tags=list(row.get("tags") or []),
        entities=list(row.get("entities") or []),
        recall_count=int(row.get("recall_count") or 0),
        last_recalled_at=row.get("last_recalled_at"),
        value_json=str(row.get("value_json") or "{}"),
        redacted_at=row.get("redacted_at"),
        redaction_reason=row.get("redaction_reason", "") or "",
        conflict_with=[str(x) for x in (row.get("conflict_with") or [])],
        duplicate_of=row.get("duplicate_of", "") or "",
        evidence_count=int(row.get("evidence_count") or 1),
        cross_promoted_from_memory_id=uuid_or_none(row.get("cross_promoted_from_memory_id")) or "",
        cross_promoted_from_namespace=row.get("cross_promoted_from_namespace", "") or "",
        superseded_by=row.get("superseded_by", "") or "",
        rejection_reason=row.get("rejection_reason", "") or "",
        temporal_anchor=row.get("temporal_anchor"),
        temporal_confidence=float(row.get("temporal_confidence") or 0.0),
        reinforcement_count=int(row.get("reinforcement_count") or 1),
        last_reinforced_at=row.get("last_reinforced_at"),
        mentioned_at=row.get("mentioned_at"),
        source_resource_id=row.get("source_resource_id", "") or "",
        files=[str(x) for x in (row.get("files") or [])],
        concepts=[str(x) for x in (row.get("concepts") or [])],
        verified_by=row.get("verified_by", "") or "",
        verified_at=row.get("verified_at"),
        verified_sha256=row.get("verified_sha256", "") or "",
        grounding=row.get("grounding", "") or "",
        actor=row.get("actor", "") or "",
        source_class=row.get("source_class", "") or "",
        evidence=row.get("evidence", "") or "",
        sensitivity=str(row.get("sensitivity", "") or ""),
        lock_mode=row.get("lock_mode", "") or "",
        lock_reason=row.get("lock_reason", "") or "",
        lock_until=row.get("lock_until"),
        locked_by=row.get("locked_by", "") or "",
        locked_at=row.get("locked_at"),
        on_expire=row.get("on_expire", "") or "",
        expire_reason=row.get("expire_reason", "") or "",
    )


def next_version() -> int:
    """Version-stamp for ReplacingMergeTree(version).

    Wall-clock epoch ns, NOT monotonic_ns: monotonic has a per-process epoch
    and isn't comparable across replicas, so the replica with the larger epoch
    would always win a same-key race. Wall-clock trades that for bounded
    cross-host clock skew (sub-ms under NTP).
    """
    return time.time_ns()


class Versioned(Protocol):
    version: int


def version_over(head: Versioned, drawn: int | None = None) -> int:
    """The version of a copy of `head`: `drawn` (by default the next one), or two above
    the head's when the head's writer's clock runs ahead, so the copy becomes the head
    and its vector retire reaches a vector-only copy of the head (`reemit_version`'s
    read + 1). With a version drawn before the read, the copy outranks every head the
    read found, and a write drawing a version above the copy's outranks it."""
    return max(next_version() if drawn is None else drawn, head.version + 2)


def version_time(version: int) -> datetime:
    """When `version` (`next_version`'s epoch ns) was taken, naive UTC, to the
    microsecond: integer arithmetic, where a float of ns rounds."""
    return datetime(1970, 1, 1) + timedelta(microseconds=int(version) // 1000)


def reemit_version(row: dict[str, Any]) -> int:
    """The version for a re-emit of a memory_embeddings row read earlier: one above it.

    The rule for every vector write that copies a read row:
    - a metadata copy (touch, verify, tags, entities) writes read + 1. It replaces
      exactly the row it read; a vector written after the read is newer and keeps
      winning.
    - a retire (sets tombstoned_at, or a status search hides, on a memory_embeddings
      or entity_vectors row) writes the version of the loom.memory (or loom.shared)
      row that caused it and skips vector rows at or above it. Two copies of one read
      row would tie at read + 1 and the later insert would win, so a retire must sit
      above any earlier read + 1. A copy lifted over a head (`version_over`) goes two
      above it, so a writer of vector-only copies writes at most one above a head.
    - a retire with no causing row (a reconcile or a sweep) uses one next_version()
      drawn before its scan.
    - the expiry sweep's demote is a metadata copy pinned like a retire, at the version
      of the memory row it wrote, so it skips a later write's vectors."""
    return int(row.get("version") or 0) + 1


# A version row carries any column of the live table these builders do not
# write (server/core/rmt_carry.py). Keys are the tables' ORDER BY.
_KEY_PREFIX = ("org", "namespace", "agent", "user_id")
register("loom.memory", key=IDENTITY_COLUMNS, columns=MEMORY_PERSISTED_COLS)
register(
    "loom.memory_embeddings",
    key=(*_KEY_PREFIX, "session_id", "scope", "embedding_model", "memory_key"),
    columns=EMBEDDING_PERSISTED_COLS,
)
register(
    "loom.session_summaries",
    key=(*_KEY_PREFIX, "session_id", "embedding_model"),
    columns=SESSION_SUMMARY_PERSISTED_COLS,
)
register(
    "loom.entity_summaries",
    key=(*_KEY_PREFIX, "entity", "embedding_model"),
    columns=ENTITY_SUMMARY_PERSISTED_COLS,
)
