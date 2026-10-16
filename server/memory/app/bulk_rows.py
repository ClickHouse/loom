"""The rows one memory.bulk_set writes: each entry's memory version row (the
columns it carries forward from the live row, the skips and locks that drop
it) and its embedding row, contextualised and embedded in one batch."""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from server.core.datetimes import parse_iso_dt
from server.core.hashing import sha256_value as _content_sha256
from server.memory.api.models_bulk import (
    BLACKLISTED,
    MemoryBulkSetInput,
    _BulkSetResult,
)
from server.memory.app.projections import (
    resolved_entity_list,
)
from server.memory.app.provenance import writer_actor
from server.memory.domain import lock as _lock
from server.memory.domain import provenance as _prov
from server.memory.domain.embed_text import _build_embed_text, key_axis_text
from server.memory.domain.excerpt import build_excerpt
from server.memory.domain.memory_types import is_embedded
from server.memory.domain.procedural_contract import retype_for as _procedural_retype
from server.memory.domain.sensitive_retention import sensitive_retention
from server.memory.domain.sensitivity import resolve_sensitivity
from server.memory.store.rows import next_version

if TYPE_CHECKING:
    from server.memory.app.search.clock import StageClock

log = logging.getLogger(__name__)


def _with_sensitivity_tag(tags: list[str], level: str) -> list[str]:
    """Append `sensitivity:<level>` to a batch row's tags so the label reaches the embeddings
    table the ranking reads. Never raises."""
    try:
        from server.memory.domain.sensitivity import sensitivity_tags

        return [*tags, *sensitivity_tags(level, tags)]
    except Exception:  # noqa: BLE001 — a label must not fail a write
        return list(tags)


def _with_attribute_tags(tags: Any, value: str, namespace: str) -> list[str]:
    """Append `attribute:<class>` labels to a batch row's tags; counts each label. Never
    raises: a label must not fail a write."""
    base = [str(t) for t in (tags or [])]
    try:
        from server.memory.domain.sensitivity import attribute_tags

        extra = attribute_tags(value, base)
        if extra:
            from server.observability.metrics import record_attribute_tagged

            for t in extra:
                record_attribute_tagged(namespace=namespace, attribute=t.split(":", 1)[1])
        return [*base, *extra]
    except Exception:  # noqa: BLE001 — tagging is optional; base tags
        return base


@dataclass(slots=True)
class _BulkWriteState:
    """Request-scoped state that flows between the phases of
    `handle_memory_bulk_set`; created at the top of the call, never shared."""

    existing_ids: dict[str, str] = field(default_factory=dict)
    # Live key -> denormalized content_sha256: lets the
    # per-entry loop skip rewrite+re-embed+re-project of a byte-identical
    # re-ingest.
    existing_sha: dict[str, str] = field(default_factory=dict)
    # Pin + creation-time of each live key, read in the SAME batched lookup.
    # `pinned` gates the per-entry skip; `created_at` is carried forward so an
    # overwrite (RMT FINAL winner) doesn't reset creation to insert time.
    existing_pinned: dict[str, bool] = field(default_factory=dict)
    existing_created: dict[str, Any] = field(default_factory=dict)
    # The live row's `_BULK_CARRIED_COLS`, read in the same lookup: its lock gates the
    # per-entry skip, and every written version row carries them forward.
    existing_carried: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The live rows themselves, from the same read: a revision carries them forward.
    existing_rows: dict[str, Any] = field(default_factory=dict)
    # Whether the batched lookup below actually succeeded. It is fail-open
    # (log-and-continue), so on a ClickHouse hiccup `existing_ids` is empty and every entry
    # would look new, emitting N `created` ticks for a re-ingest of an unchanged corpus: the
    # exact false signal the counter exists to remove, during an incident when the panel is
    # trusted. MUST be initialised here, before the lookup: initialising it after the lookup
    # would clobber the success assignment and classify every batch as `unknown`.
    existing_lookup_ok: bool = False
    # Identity dedup: entry key -> the keeper key that holds its bytes, and the keepers' ids
    # to reinforce.
    identity_duplicates: dict[str, str] = field(default_factory=dict)
    identity_dup_ids: list[str] = field(default_factory=list)
    # A byte-identical restatement of the SAME key is the frequency signal: it reinforces the
    # live row instead of vanishing.
    restated_ids: list[str] = field(default_factory=list)
    # (retired id, the row it retired behind) per same-key retirement the judge decided
    # (`key_supersession.judge_same_key_rows`), for the triple plane's follow step.
    judged_follow_pairs: list[tuple[str, str]] = field(default_factory=list)
    # Keys whose row FINAL keeps landed superseded by the judge: no version of them links.
    judged_retired_keys: set[str] = field(default_factory=set)
    memory_rows: list[dict[str, Any]] = field(default_factory=list)
    embedding_rows: list[dict[str, Any]] = field(default_factory=list)
    # Each landing row's id -> its key, built once the verdicts fix which rows land.
    key_of: dict[str, str] = field(default_factory=dict)
    # One per entry, in order; version 0 for an entry that wrote nothing.
    results: list[_BulkSetResult] = field(default_factory=list)
    # Row i's result is results[row_results[i]], as built (before the verdicts drop rows).
    row_results: list[int] = field(default_factory=list)
    # (row_idx, embed_text, excerpt, key_text) per searchable kept entry.
    pending: list[tuple[int, str, str, str]] = field(default_factory=list)
    # (entry, serialized_value, tags, entities, expires_at) per kept entry.
    entry_metas: list[tuple[Any, str, list[str], list[str], datetime | None]] = field(
        default_factory=list
    )
    skipped_unchanged: int = 0
    # Keys of the entries the blacklist refused.
    skipped_blacklisted: list[str] = field(default_factory=list)
    retyped: list[str] = field(default_factory=list)
    skipped_pinned: list[str] = field(default_factory=list)
    skipped_locked: list[str] = field(default_factory=list)
    # Write outcomes for `loom.memory.writes`: created / updated / unknown.
    n_created: int = 0
    n_updated: int = 0
    n_unknown: int = 0


# Columns a bulk version row sets from the live row under its key before the rest of
# the row is carried (`memory_repo.build_revision_row`): the lock (a no_delete value
# change keeps it; it also gates the per-entry skip) and the expiry action. Every other
# column of a revision follows the builder's rule (usage and lineage carried, the old
# value's verdicts and attestation reset), as memory.set's does.
_BULK_CARRIED_COLS: tuple[str, ...] = (
    "lock_mode",
    "lock_reason",
    "lock_until",
    "locked_by",
    "locked_at",
    "on_expire",
    "expire_reason",
)
_BULK_CARRIED_DEFAULTS: dict[str, Any] = {
    "lock_mode": "",
    "lock_reason": "",
    "lock_until": None,
    "locked_by": "",
    "locked_at": None,
    "on_expire": "",
    "expire_reason": "",
}


def _bulk_carried(state: _BulkWriteState, memory_key: str) -> dict[str, Any]:
    """The `_BULK_CARRIED_COLS` a version row under `memory_key` writes: the live row's,
    else the schema defaults. Every row carries all of them — ch.insert derives its column
    list from rows[0].keys()."""
    live = state.existing_carried.get(memory_key) or {}
    return {
        c: (live.get(c) if live.get(c) is not None else d)
        for c, d in _BULK_CARRIED_DEFAULTS.items()
    }


def _full_version_row(
    state: _BulkWriteState, memory_row: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Every row carries every column (one insert, one column list): a revision
    carries the live row forward (`build_revision_row`), a first write takes the
    DDL defaults (`build_first_version_row`)."""
    import server.memory.store as memory_repo

    live = state.existing_rows.get(str(memory_row["memory_key"]))
    if live is not None:
        return memory_repo.build_revision_row(live, memory_row)
    return memory_repo.build_first_version_row(memory_row, now=now)


def _bulk_value_locked(state: _BulkWriteState, memory_key: str) -> bool:
    """The live row's lock in force refuses a value change (read_only)."""
    live = state.existing_carried.get(memory_key)
    if not live:
        return False
    mode = _lock.effective_mode(_LockView(live))
    return "value" in _lock.REFUSES.get(mode, frozenset())


class _LockView:
    """Attribute view of a lookup row for `memory_lock.effective_mode`."""

    __slots__ = ("lock_mode", "lock_until")

    def __init__(self, cols: dict[str, Any]) -> None:
        self.lock_mode = cols.get("lock_mode")
        self.lock_until = cols.get("lock_until")


def _bulk_skip_gate(
    entry: Any,
    entry_sha: str,
    state: _BulkWriteState,
    identity_dupes: dict[str, tuple[str, str]],
) -> bool:
    """Phase 4a, per entry: True when the entry writes nothing — an unchanged
    restatement, an identity duplicate, a value-locked or a pinned target. Each still gets a
    version-0 result row so the response shape is unchanged."""
    # Re-ingest skip-gate: a byte-identical live value skips the
    # rewrite+re-embed+re-project. Caller still gets a result row
    # (version 0) so the response shape is unchanged.
    if state.existing_sha.get(entry.memory_key) == entry_sha:
        state.skipped_unchanged += 1
        _rid = state.existing_ids.get(entry.memory_key)
        if _rid and _rid not in state.restated_ids:
            state.restated_ids.append(_rid)
        state.results.append(_BulkSetResult(memory_key=entry.memory_key, version=0))
        return True
    # The same bytes already live under this identity, in another session or under another
    # key. Reinforce the keeper (an `evidence` event the usage fold turns into
    # reinforcement_count), write nothing, and tell the caller which key holds the fact.
    _dup = identity_dupes.get(entry_sha)
    if _dup is not None:
        state.identity_duplicates[entry.memory_key] = _dup[0]
        if _dup[1] not in state.identity_dup_ids:
            state.identity_dup_ids.append(_dup[1])
        state.results.append(_BulkSetResult(memory_key=entry.memory_key, version=0))
        return True
    # Pin is a write-time invariant. The point path 409s on a conflicting
    # write against a pinned peer; here (machine-extracted batch) a pinned
    # target skips just that entry — surfaced in `skipped_pinned` — rather
    # than failing the whole batch or silently overwriting (which unpins,
    # since entries default pinned=False).
    # A lock is the same kind of invariant: memory.set 423s a value change to a read_only
    # key; a batch skips just that entry, surfaced in `skipped_locked`.
    if _bulk_value_locked(state, entry.memory_key):
        state.skipped_locked.append(entry.memory_key)
        state.results.append(_BulkSetResult(memory_key=entry.memory_key, version=0))
        return True
    if state.existing_pinned.get(entry.memory_key):
        state.skipped_pinned.append(entry.memory_key)
        state.results.append(_BulkSetResult(memory_key=entry.memory_key, version=0))
        return True
    return False


def _bulk_append_pending(state: _BulkWriteState, entry: Any, serialized_value: str) -> None:
    """Phase 4b, per searchable kept entry (the one just appended to
    `state.memory_rows`): queue its embed text, clean excerpt and key-axis
    text for the one batched embed call."""
    # Embed the fact-augmented text; store the CLEAN excerpt as
    # content_excerpt. Humanized key text ("user_dog_name" -> "user dog
    # name") rides the same batch as a second vector axis, matching the
    # point path so a key embeds identically across write paths.
    # question_topic marks a genuine assistant answer: it topic-keys the
    # embed text and raises the excerpt ceiling so a long/enumerated
    # answer survives WHOLE (the reader surfaces content_excerpt).
    entry_topic = getattr(entry, "question_topic", "") or ""
    state.pending.append(
        (
            len(state.memory_rows) - 1,
            _build_embed_text(
                serialized_value,
                memory_key=entry.memory_key,
                question_topic=entry_topic,
            ),
            build_excerpt(
                serialized_value,
                memory_key=entry.memory_key,
                is_answer=bool(entry_topic.strip()),
            ),
            key_axis_text(entry.memory_key),
        )
    )


async def _bulk_build_rows(
    input: MemoryBulkSetInput,
    state: _BulkWriteState,
    identity_dupes: dict[str, tuple[str, str]],
    entity_resolution: dict[str, str],
    now_utc: datetime,
) -> None:
    """Phase 4: walk the entries in order — skip gate, TTL and retention,
    char_limit (raises 422), blacklist, procedural retype — and append one
    full-column memory row, a result and (when searchable) a pending embed
    item per kept entry to `state`."""
    from server.memory.app.blacklist_guard import find_match, note_hit

    _seen_keys: set[str] = set()
    for entry in input.entries:
        entry_sha = _content_sha256(entry.value)
        if _bulk_skip_gate(entry, entry_sha, state, identity_dupes):
            continue
        version = next_version()
        expires_at = (
            now_utc + timedelta(seconds=entry.ttl_seconds)
            if entry.ttl_seconds is not None
            else None
        )
        serialized_value = json.dumps(entry.value)
        # Sensitivity tier: caller's level or detected from
        # the value; rides the row, its `sensitivity:` tag and the embed gate.
        entry_sensitivity = resolve_sensitivity(getattr(entry, "sensitivity", ""), serialized_value)
        # Sensitive retention window, same rule as memory.set.
        from server.settings import settings as _sr_settings

        expires_at, entry_on_expire, _ = sensitive_retention(
            sensitivity=entry_sensitivity,
            expires_at=expires_at,
            # The entry's, else the live row's — as memory.set.
            on_expire=str(getattr(entry, "on_expire", "") or "")
            or str(_bulk_carried(state, entry.memory_key)["on_expire"]),
            now=now_utc,
            settings=_sr_settings,
        )
        # Per-entry char_limit enforcement — same shape as handle_memory_set.
        if entry.char_limit is not None and len(serialized_value) > entry.char_limit:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CHAR_LIMIT_EXCEEDED",
                    "message": (
                        f"entry {entry.memory_key!r}: value length "
                        f"{len(serialized_value)} exceeds char_limit "
                        f"{entry.char_limit}"
                    ),
                    "memory_key": entry.memory_key,
                    "value_length": len(serialized_value),
                    "char_limit": entry.char_limit,
                },
            )
        # Refuse (blacklist): the point path 422s; here a match DROPS just
        # the offending entry + logs, rather than failing the whole batch.
        bl_hit = await find_match(org=input.org, namespace=input.namespace, value=serialized_value)
        if bl_hit is not None:
            await note_hit(bl_hit.pattern_id)
            state.skipped_blacklisted.append(entry.memory_key)
            state.results.append(
                _BulkSetResult(memory_key=entry.memory_key, version=0, conflict_verdict=BLACKLISTED)
            )
            log.warning(
                "bulk_set dropped blacklisted entry key=%r pattern=%s scope=%s",
                entry.memory_key,
                bl_hit.pattern_id,
                bl_hit.scope,
            )
            continue
        # The procedural type keeps its contract at every door.
        _retype = _procedural_retype(entry.memory_type, entry.value)
        if _retype is not None:
            state.retyped.append(entry.memory_key)
            entry = entry.model_copy(
                update={
                    "memory_type": _retype.memory_type,
                    "tags": [
                        *(entry.tags or []),
                        *[t for t in _retype.tags if t not in (entry.tags or [])],
                    ],
                }
            )
        entry_tags = list(entry.tags or [])
        entry_anchor_dt = parse_iso_dt(entry.temporal_anchor)
        entry_entities = resolved_entity_list(entry.entities or [], entity_resolution)
        state.entry_metas.append((entry, serialized_value, entry_tags, entry_entities, expires_at))
        memory_row = {
            "org": input.org,
            "namespace": input.namespace,
            "agent": input.agent,
            "user_id": input.user_id,
            "session_id": input.session_id,
            "run_id": input.run_id,
            "memory_key": entry.memory_key,
            "memory_type": entry.memory_type,
            "value": serialized_value,
            "value_json": (serialized_value if isinstance(entry.value, dict) else "{}"),
            "confidence": entry.confidence,
            "source_trace": entry.source_trace,
            # Provenance: writer from the token, class from the source_trace convention
            # (pipeline:… → extracted) else asserted; the entry may only declare a downgrade.
            "actor": writer_actor(),
            "source_class": _prov.source_class_for(
                default="asserted",
                requested=getattr(entry, "source_class", None),
                source_trace=entry.source_trace,
            ),
            # Denormalized content hash — omitting it lands the
            # FixedString DEFAULT '0' and silently kills the re-ingest skip-gate.
            "content_sha256": entry_sha,
            "version": version,
            "tombstoned_at": None,
            "expires_at": expires_at,
            "valid_at": entry.valid_at,
            # Closing bound of the fact's stated validity. The extractor emits it on
            # closed-window state changes ("until 2024") and the pipeline parses it into the
            # entry; omitting it here would leave the read side's validity window NULL on
            # every row. NULL = still valid.
            "valid_to": entry.valid_to,
            "char_limit": entry.char_limit,
            "pinned": 1 if entry.pinned else 0,
            # Protected-attribute label rides the row's tags.
            "tags": _with_sensitivity_tag(
                _with_attribute_tags(entry_tags, serialized_value, input.namespace),
                entry_sensitivity,
            ),
            "entities": entry_entities,
            # Code-native facets. Carried on every row: ch.insert
            # derives columns from rows[0].keys(), and under RMT FINAL an omitted
            # array would zero a prior facet on overwrite.
            "files": list(getattr(entry, "files", None) or []),
            "concepts": list(getattr(entry, "concepts", None) or []),
            # Grounding: the grade and quote ride on every row
            # for the same rows[0].keys() reason as the facets above.
            "grounding": str(getattr(entry, "grounding", "") or ""),
            "evidence": str(getattr(entry, "evidence", "") or "")[:400],
            # Sensitivity tier: caller's level or detected
            # from the value; 'high' skips the embedding row below.
            "sensitivity": entry_sensitivity,
            "temporal_anchor": entry_anchor_dt,
            # Grade of that anchor; meaningless without one.
            "temporal_confidence": (
                float(getattr(entry, "temporal_confidence", 0.0) or 0.0)
                if entry_anchor_dt is not None
                else 0.0
            ),
            # memory_id / created_at MUST be set on EVERY row: ch.insert derives
            # the column list from rows[0].keys(), so a batch mixing existing-key
            # rows (carry-forward) with new-key rows that omit these would either
            # KeyError or drop the carried values — rotating memory_id / resetting
            # creation on overwrite. Mirror the point path: existing when live,
            # fresh uuid4 / insert-time otherwise.
            "memory_id": (state.existing_ids.get(entry.memory_key) or str(_uuid.uuid4())),
            # status / duplicate_of MUST be on every row too: the bulk verdict step stamps
            # them on the duplicate rows only, and a batch whose rows disagree on their keys
            # fails ch.insert with KeyError('status'). Schema defaults, overwritten by the
            # verdict.
            "status": "active",
            "duplicate_of": "",
            "created_at": (
                state.existing_created.get(entry.memory_key) or now_utc.replace(tzinfo=None)
            ),
            # Lock and expire_reason of the live row (defaults for a new key); on_expire
            # is the resolved one above.
            **_bulk_carried(state, entry.memory_key),
            "on_expire": entry_on_expire,
            "updated_at": now_utc.replace(tzinfo=None),
        }
        memory_row = _full_version_row(state, memory_row, now_utc.replace(tzinfo=None))
        state.memory_rows.append(memory_row)
        # created vs updated, decided per row rather than labelling the whole batch
        # "created". Only byte-identical restatements are diverted by the skip-gate above; an
        # entry whose key already exists with DIFFERENT content is inserted as a new version,
        # and calling that "created" would make the panel show healthy creation volume for a
        # corpus re-extracting the same keys: the same false signal the counter exists to
        # remove, with the opposite sign. `existing_ids` is the same lookup the pin and
        # skip-gate checks use.
        if not state.existing_lookup_ok:
            # Unknown, not "new". Counting these as `created` would fabricate
            # learning volume during an incident.
            state.n_unknown += 1
        elif entry.memory_key in state.existing_ids or entry.memory_key in _seen_keys:
            # `_seen_keys` catches two entries sharing one memory_key inside a
            # single batch — the second is a new version, not a creation.
            state.n_updated += 1
        else:
            state.n_created += 1
        _seen_keys.add(entry.memory_key)
        state.row_results.append(len(state.results))
        state.results.append(
            _BulkSetResult(
                memory_key=entry.memory_key, version=version, memory_id=str(memory_row["memory_id"])
            )
        )

        if is_embedded(entry.memory_type, entry_sensitivity):
            _bulk_append_pending(state, entry, serialized_value)


async def _bulk_contextualize(
    input: MemoryBulkSetInput,
    pending: list[tuple[int, str, str, str]],
    clock: StageClock,
) -> list[tuple[int, str, str, str]]:
    """Phase 5 (opt-in): the pending embed items with a model-written context
    line in front of each text and excerpt; the list unchanged when off, over
    the cap, or on any failure."""
    if pending and bool(input.contextualize):
        # This call's entries are one document; each embedded text and excerpt gets a
        # model-written one-line context in front. Fail-open: a missing context leaves that
        # entry as it was.
        try:
            from server.settings import settings as _cx_settings

            if bool(_cx_settings.memory_bulk_contextualize_allowed) and len(pending) <= int(
                _cx_settings.memory_bulk_contextualize_max_entries
            ):
                from server.llm.client import get_llm_client
                from server.memory.app.chunk_context import contextualize_chunks, prefixed

                document = "\n\n".join(excerpt for _, _, excerpt, _ in pending)
                contexts = await contextualize_chunks(
                    get_llm_client(),
                    document,
                    [excerpt for _, _, excerpt, _ in pending],
                    concurrency=int(_cx_settings.memory_bulk_contextualize_concurrency),
                )
                contextualized = [
                    (idx, prefixed(cx, embed_text), prefixed(cx, excerpt), key_text)
                    for (idx, embed_text, excerpt, key_text), cx in zip(
                        pending, contexts, strict=False
                    )
                ]
                clock.mark("contextualize")
                return contextualized
        except Exception as e:  # noqa: BLE001 - the write proceeds without contexts
            log.warning("bulk_set contextualize skipped (non-fatal): %s", e)
    return pending


async def _bulk_embed_rows(
    input: MemoryBulkSetInput,
    state: _BulkWriteState,
    embedder: Any,
    entity_resolution: dict[str, str],
    clock: StageClock,
    now_utc: datetime,
) -> tuple[list[int], list[list[float]], dict[str, list[float]]]:
    """Phase 6: ONE embed call for the value, key and entity texts; one
    full-column embedding row per pending item appended to
    `state.embedding_rows`. Returns (memory-row index of each embedding row,
    the value vectors, entity -> vector)."""
    from server.memory.store import build_embedding_row

    emb_idx_of: list[int] = []
    value_vectors: list[list[float]] = []
    # The entity texts ride the same embedding batch as the values and keys, so the
    # entity-vector projection makes no provider call of its own (a separate round trip
    # would be the longest tail leg).
    entity_texts: list[str] = sorted(
        {
            ent
            for entry in input.entries
            for ent in resolved_entity_list(list(entry.entities or []), entity_resolution)
            if ent
        }
    )
    entity_embeddings: dict[str, list[float]] = {}
    state.pending = await _bulk_contextualize(input, state.pending, clock)
    if state.pending:
        # ONE batched embed call: value texts first, key texts appended.
        # Search ranks on min(value_distance, key_distance), and embeddings is
        # RMT(version), so a row missing its key vector both loses that axis and
        # (on overwrite) replaces a prior real key vector with the default [].
        value_texts = [embed_text for _, embed_text, _, _ in state.pending]
        key_texts = [key_text for _, _, _, key_text in state.pending if key_text]
        clock.mark("prepare")
        batch_vectors = await embedder.embed_many(value_texts + key_texts + entity_texts)
        clock.mark("embed")
        value_vectors = [list(v) for v in batch_vectors[: len(value_texts)]]
        _n_vk = len(value_texts) + len(key_texts)
        key_vector_iter = iter(batch_vectors[len(value_texts) : _n_vk])
        entity_embeddings = {
            ent: list(vec) for ent, vec in zip(entity_texts, batch_vectors[_n_vk:], strict=False)
        }
        for (idx, _embed_text, excerpt, key_text), embedding in zip(
            state.pending, batch_vectors[: len(value_texts)], strict=True
        ):
            entry, _serialized, e_tags, e_ents, e_expires = state.entry_metas[idx]
            key_embedding = next(key_vector_iter) if key_text else []
            emb_idx_of.append(idx)
            state.embedding_rows.append(
                build_embedding_row(
                    org=input.org,
                    namespace=input.namespace,
                    agent=input.agent,
                    user_id=input.user_id,
                    session_id=input.session_id,
                    run_id=input.run_id,
                    memory_key=entry.memory_key,
                    memory_type=entry.memory_type,
                    scope="private",
                    content_excerpt=excerpt,
                    source_class=_prov.source_class_for(
                        default="asserted",
                        requested=getattr(entry, "source_class", None),
                        source_trace=entry.source_trace,
                    ),
                    embedding_model=embedder.model_name,
                    embedding_dim=embedder.dim,
                    embedding=embedding,
                    memory_key_embedding=key_embedding,
                    memory_key_embedding_dim=embedder.dim if key_text else 0,
                    confidence=float(entry.confidence),
                    tombstoned_at=None,
                    created_at=now_utc.replace(tzinfo=None),
                    tags=e_tags,
                    entities=e_ents,
                    files=list(getattr(entry, "files", None) or []),
                    concepts=list(getattr(entry, "concepts", None) or []),
                    temporal_anchor=parse_iso_dt(entry.temporal_anchor),
                    temporal_confidence=(
                        float(getattr(entry, "temporal_confidence", 0.0) or 0.0)
                        if parse_iso_dt(entry.temporal_anchor) is not None
                        else 0.0
                    ),
                    valid_at=entry.valid_at,
                    valid_to=entry.valid_to,
                    expires_at=e_expires,
                    verified=_row_attested(state.memory_rows[idx]),
                    content_sha256=str(state.memory_rows[idx].get("content_sha256") or ""),
                    # The entry's memory version: a newer write's retire or fence
                    # outranks this vector even when the batch lands late.
                    version=int(state.memory_rows[idx]["version"]),
                )
            )
    return emb_idx_of, value_vectors, entity_embeddings


def _row_attested(row: dict[str, Any]) -> int:
    """The embedding mirror's `verified`: the version row's attestation binds
    its content (a revision of unchanged content keeps it)."""
    from server.memory.domain.trust import attests

    return int(
        attests(row.get("verified_by"), row.get("verified_sha256"), row.get("content_sha256"))
    )
