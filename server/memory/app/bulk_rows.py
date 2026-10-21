"""The rows one memory.bulk_set writes: each entry's memory version row (the
columns it carries forward from the live row, the skips and locks that drop
it) and its embedding row, contextualised and embedded in one batch."""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from fastapi import HTTPException

from server.core.datetimes import parse_iso_dt, to_naive_utc
from server.core.hashing import sha256_value as _content_sha256
from server.memory.api.models_bulk import (
    BLACKLISTED,
    LOCKED,
    PINNED,
    MemoryBulkSetInput,
    _BulkSetResult,
)
from server.memory.app.bulk_verdicts import NOT_LANDING, BulkVerdictPlan, verdict_target
from server.memory.app.projections import (
    resolved_entity_list,
)
from server.memory.app.provenance import writer_actor
from server.memory.app.set_commit import set_next, set_peers
from server.memory.domain import lock as _lock
from server.memory.domain import provenance as _prov
from server.memory.domain.embed_text import _build_embed_text, key_axis_text
from server.memory.domain.excerpt import build_excerpt
from server.memory.domain.expire_reasons import carried_reason
from server.memory.domain.memory_types import is_embedded
from server.memory.domain.procedural_contract import retype_for as _procedural_retype
from server.memory.domain.sensitive_retention import sensitive_retention
from server.memory.domain.sensitivity import detector_text, resolve_sensitivity
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
    # Ids minted for keys with no row yet: a key written twice in one batch is one row.
    new_ids: dict[str, str] = field(default_factory=dict)
    # Live key -> denormalized content_sha256: lets the
    # per-entry loop skip rewrite+re-embed+re-project of a byte-identical
    # re-ingest. The store's view only: the batch's own rows are in `batch_rows`.
    existing_sha: dict[str, str] = field(default_factory=dict)
    # Pin + creation-time of each live key, read in the SAME batched lookup.
    # `pinned` gates the per-entry skip; `created_at` is carried forward so an
    # overwrite (RMT FINAL winner) doesn't reset creation to insert time.
    existing_pinned: dict[str, bool] = field(default_factory=dict)
    # Key -> the batch's latest row for it. Only a candidate until the verdicts say
    # whether it lands (`settle_batch_rows`).
    batch_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
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
    # Ids of the rows this batch has written a new version of: their stored bytes may be gone.
    rewritten_ids: set[str] = field(default_factory=set)
    # A byte-identical restatement of the SAME key is the frequency signal: it reinforces the
    # live row instead of vanishing. `restated_ids` are the rows to reinforce;
    # `restatements` are (result index, batch row) for an entry repeating a value an
    # earlier entry wrote, settled after the verdicts (which may drop that row).
    restated_ids: list[str] = field(default_factory=list)
    restatements: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    # Indexes into `memory_rows` of the rows whose entry named no sensitivity and took the
    # label of the value it repeats: their level is not a change (`_same_labels`).
    inherited_level_rows: set[int] = field(default_factory=set)
    # Key -> the entry that built `batch_rows[key]`. Only an entry equal to it is a
    # restatement: one that changes a label (sensitivity, pin, tags, dates) builds its own row.
    batch_entries: dict[str, Any] = field(default_factory=dict)
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
    carried = {
        c: (live.get(c) if live.get(c) is not None else d)
        for c, d in _BULK_CARRIED_DEFAULTS.items()
    }
    carried["expire_reason"] = carried_reason(carried["expire_reason"])
    return carried


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


def _bulk_locked(state: _BulkWriteState, memory_key: str, action: str) -> bool:
    """The live row's lock in force refuses `action` ('value' or 'expire': read_only)."""
    live = state.existing_carried.get(memory_key)
    if not live:
        return False
    mode = _lock.effective_mode(_LockView(live))
    return action in _lock.REFUSES.get(mode, frozenset())


def _refuse_locked(state: _BulkWriteState, memory_key: str) -> None:
    """The entry writes nothing: its key's lock refuses it, as memory.set's 423 does."""
    state.skipped_locked.append(memory_key)
    state.results.append(_BulkSetResult(memory_key=memory_key, version=0, conflict_verdict=LOCKED))


class _LockView:
    """Attribute view of a lookup row for `memory_lock.effective_mode`."""

    __slots__ = ("lock_mode", "lock_until")

    def __init__(self, cols: dict[str, Any]) -> None:
        self.lock_mode = cols.get("lock_mode")
        self.lock_until = cols.get("lock_until")


def _note_restatement(state: _BulkWriteState, memory_id: str) -> None:
    """Reinforce `memory_id` once for a byte-identical restatement of its value."""
    if memory_id and memory_id not in state.restated_ids:
        state.restated_ids.append(memory_id)


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
    # Repeating an earlier entry exactly is unchanged only if that entry lands:
    # `settle_batch_rows` gives it the earlier entry's outcome, and counts it, after the
    # verdicts. An entry that repeats the value with other labels builds its own row.
    batch_row = state.batch_rows.get(entry.memory_key)
    if (
        batch_row is not None
        and batch_row["content_sha256"] == entry_sha
        and state.batch_entries.get(entry.memory_key) == entry
    ):
        state.restatements.append((len(state.results), batch_row))
        state.results.append(_BulkSetResult(memory_key=entry.memory_key, version=0))
        return True
    restated = state.existing_sha.get(entry.memory_key) == entry_sha
    # A ttl_seconds asks to change the expiry, which a read_only lock refuses even when
    # nothing else changes (memory.set: 423, refused_action 'expire').
    if (
        restated
        and getattr(entry, "ttl_seconds", None) is not None
        and _bulk_locked(state, entry.memory_key, "expire")
    ):
        _refuse_locked(state, entry.memory_key)
        return True
    if batch_row is None and restated and _same_labels_as_stored(state, entry):
        state.skipped_unchanged += 1
        _note_restatement(state, state.existing_ids.get(entry.memory_key, ""))
        state.results.append(_BulkSetResult(memory_key=entry.memory_key, version=0))
        return True
    # The same bytes already live under this identity, in another session or under another
    # key. Reinforce the keeper (an `evidence` event the usage fold turns into
    # reinforcement_count), write nothing, and tell the caller which key holds the fact.
    _dup = identity_dupes.get(entry_sha)
    # Dedup only stops a new key from repeating a stored fact: a key that already has a row
    # here is being revised. A keeper the batch has rewritten may no longer hold the bytes.
    if (
        _dup is not None
        and entry.memory_key not in state.existing_sha
        and entry.memory_key not in state.batch_rows
        and _dup[1] not in state.rewritten_ids
    ):
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
    # Only the stored pin skips here. A pin an earlier entry asked for counts once the
    # verdicts say that entry landed (`settle_batch_rows`).
    # The same bytes are not a value change: as on memory.set, no pin refuses them, and a
    # lock refuses them only if they change the expiry (`_changes_locked_expiry`).
    if restated:
        return False
    if _bulk_locked(state, entry.memory_key, "value"):
        _refuse_locked(state, entry.memory_key)
        return True
    # A pinning write may replace a pinned value, as on memory.set.
    if state.existing_pinned.get(entry.memory_key) and not getattr(entry, "pinned", False):
        state.skipped_pinned.append(entry.memory_key)
        state.results.append(
            _BulkSetResult(
                memory_key=entry.memory_key,
                version=0,
                conflict_verdict=PINNED,
                pinned_blocker_id=state.existing_ids.get(entry.memory_key, ""),
            )
        )
        return True
    return False


def _same_labels_as_stored(state: _BulkWriteState, entry: Any) -> bool:
    """Whether an entry repeating the stored value also keeps its sensitivity and adds no
    pin. memory.set writes a new version when either changes, so a bulk must too. Only a
    level the entry names counts: an extracted fact names none, and keeps the stored one."""
    live = state.existing_rows.get(entry.memory_key)
    stored_level = str(getattr(live, "sensitivity", "") or "")
    named_level = str(getattr(entry, "sensitivity", "") or "")
    adds_pin = bool(getattr(entry, "pinned", False)) and not state.existing_pinned.get(
        entry.memory_key
    )
    return named_level in ("", stored_level) and not adds_pin


def _repeated_level(state: _BulkWriteState, entry: Any, entry_sha: str) -> str | None:
    """The label of the value an entry repeats when it names no level of its own: the
    batch's latest row for its key, else the stored row. None when it names a level or
    writes a new value."""
    if getattr(entry, "sensitivity", ""):
        return None
    batch_row = state.batch_rows.get(entry.memory_key)
    if batch_row is not None:
        if batch_row["content_sha256"] != entry_sha:
            return None
        return str(batch_row.get("sensitivity") or "")
    if state.existing_sha.get(entry.memory_key) != entry_sha:
        return None
    live = state.existing_rows.get(entry.memory_key)
    return str(getattr(live, "sensitivity", "") or "")


def _entry_expiry(
    state: _BulkWriteState, entry: Any, entry_sha: str, now_utc: datetime
) -> datetime | None:
    """The expiry an entry's row starts from, before the sensitive retention: its
    `ttl_seconds`, else the expiry of the value it repeats (the batch's latest row for its
    key, else the stored row) when that expiry has not passed (memory.set keeps it the same
    way), else none."""
    if entry.ttl_seconds is not None:
        return now_utc + timedelta(seconds=entry.ttl_seconds)
    batch_row = state.batch_rows.get(entry.memory_key)
    if batch_row is not None:
        if batch_row["content_sha256"] != entry_sha:
            return None
        return _not_lapsed(batch_row.get("expires_at"), now_utc)
    if state.existing_sha.get(entry.memory_key) != entry_sha:
        return None
    return _live_stored_expiry(state, entry.memory_key, now_utc)


def _live_stored_expiry(
    state: _BulkWriteState, memory_key: str, now_utc: datetime
) -> datetime | None:
    """The stored row's expiry under `memory_key`, if it has not passed yet."""
    return _not_lapsed(getattr(state.existing_rows.get(memory_key), "expires_at", None), now_utc)


def _not_lapsed(expires_at: datetime | None, now_utc: datetime) -> datetime | None:
    """`expires_at` as naive UTC, or None when it is unset or has passed."""
    expiry = to_naive_utc(expires_at)
    if expiry is not None and expiry > to_naive_utc(now_utc):
        return expiry
    return None


def _changes_locked_expiry(
    state: _BulkWriteState,
    entry: Any,
    entry_sha: str,
    expires_at: datetime | None,
    now_utc: datetime,
) -> bool:
    """Whether an entry repeating a read_only row's bytes would change its expiry, by its
    `ttl_seconds` or by the sensitive retention: the lock refuses it (memory.set: 423,
    refused_action 'expire'). A new value on such a row the skip gate refuses already."""
    key = entry.memory_key
    if state.existing_sha.get(key) != entry_sha or not _bulk_locked(state, key, "expire"):
        return False
    if entry.ttl_seconds is not None:
        return True
    return to_naive_utc(expires_at) != _live_stored_expiry(state, key, now_utc)


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


def _keep_row(state: _BulkWriteState, row: dict[str, Any], entry: Any) -> None:
    """Add a row built from `entry` as its key's latest candidate. It is not the key's
    value yet: the verdicts may still drop it. (A batch never sets a lock: the row carries
    the stored one.)"""
    state.memory_rows.append(row)
    state.batch_rows[str(row["memory_key"])] = row
    state.batch_entries[str(row["memory_key"])] = entry
    state.rewritten_ids.add(str(row["memory_id"]))


class _Copy(NamedTuple):
    """A restatement (result `res_i`) that copies the verdict of an earlier entry's `row`
    (result `src_i`)."""

    res_i: int
    src_i: int
    row: dict[str, Any]


class _Landing(NamedTuple):
    """A restatement whose earlier entry's `row` merged into `target`, a row the batch
    rewrote in between: that row lands for it."""

    res_i: int
    src_i: int
    row: dict[str, Any]
    target: str


@dataclass
class _Replay:
    """What `settle_batch_rows` has replayed so far, by row `id()`. Once it is done,
    `drop`, `copies`, `stale`, `landed` and `landings` are what the next steps read."""

    sha: dict[str, str]  # key -> the value it holds now
    pinned: dict[str, bool]  # key -> its pin now
    sensitivity: dict[str, str]  # key -> its sensitivity now
    rewritten: set[str] = field(default_factory=set)  # ids an earlier landed row rewrote
    stale: set[int] = field(default_factory=set)
    landed: set[int] = field(default_factory=set)
    outcome: dict[int, str] = field(default_factory=dict)  # landed | unchanged | pinned | verdict
    result_of: dict[int, int] = field(default_factory=dict)  # its result index
    index_of: dict[int, int] = field(default_factory=dict)  # its memory-row index
    copies: list[_Copy] = field(default_factory=list)
    drop: set[int] = field(default_factory=set)  # memory-row indexes to keep out
    landings: list[_Landing] = field(default_factory=list)  # `land_restated_merges`

    def land(self, row: dict[str, Any]) -> None:
        """`row` lands: it becomes its key's value and pin."""
        key = str(row["memory_key"])
        self.outcome[id(row)] = "landed"
        self.landed.add(id(row))
        self.sha[key] = str(row["content_sha256"])
        self.pinned[key] = bool(row["pinned"])
        self.sensitivity[key] = str(row.get("sensitivity") or "")
        self.rewritten.add(str(row["memory_id"]))


def settle_batch_rows(state: _BulkWriteState, verdicts: dict[int, Any]) -> _Replay:
    """Replay the batch in entry order as serial memory.set calls of the same key would
    see it, once the `verdicts` (by memory-row index) are known: each landed row becomes
    its key's value and pin, so a later row of that key is refused, unchanged or stale
    as it would be serially. (A byte-identical value under another key is not replayed,
    a gap the serial path has too.)"""
    replay = _Replay(
        sha=dict(state.existing_sha),
        pinned=dict(state.existing_pinned),
        sensitivity={
            k: str(getattr(r, "sensitivity", "") or "") for k, r in state.existing_rows.items()
        },
    )
    steps: list[tuple[int, int, dict[str, Any]]] = [
        (state.row_results[i], i, row) for i, row in enumerate(state.memory_rows)
    ]
    steps += [(res_i, -1, row) for res_i, row in state.restatements]
    for res_i, idx, row in sorted(steps, key=lambda s: s[0]):
        if idx < 0:
            _replay_restatement(state, replay, verdicts, res_i, row)
            continue
        key = str(row["memory_key"])
        replay.result_of[id(row)] = res_i
        replay.index_of[id(row)] = idx
        v = verdicts.get(idx)
        if verdict_target(v) and verdict_target(v) in replay.rewritten:
            replay.stale.add(id(row))
        # The skip gate's order: unchanged, then pinned, then the verdict.
        same_bytes = replay.sha.get(key) == row["content_sha256"]
        if same_bytes and replay.pinned.get(key):
            row["pinned"] = 1  # the same bytes keep the pin, as on memory.set
        unchanged = same_bytes and _same_labels(replay, row, idx in state.inherited_level_rows)
        refused = replay.pinned.get(key) and not same_bytes and not row["pinned"]
        if unchanged or refused:
            replay.outcome[id(row)] = "unchanged" if unchanged else PINNED
            _drop_built_row(state, state.results[res_i], row, replay.outcome[id(row)])
            replay.drop.add(idx)
        elif getattr(v, "verdict", "") in NOT_LANDING and id(row) not in replay.stale:
            replay.outcome[id(row)] = "verdict"
        else:
            # The same bytes with a new label: still a restatement, as on memory.set.
            if same_bytes:
                _note_restatement(state, str(row["memory_id"]))
            replay.land(row)
    return replay


def _same_labels(replay: _Replay, row: dict[str, Any], inherited_level: bool) -> bool:
    """Whether `row` keeps the sensitivity and pin its key holds now. A level the row's
    entry did not name (`inherited_level`) is never a change."""
    key = str(row["memory_key"])
    level = str(row.get("sensitivity") or "")
    same_level = inherited_level or level == replay.sensitivity.get(key, "")
    return same_level and bool(row["pinned"]) == bool(replay.pinned.get(key))


def _replay_restatement(
    state: _BulkWriteState,
    replay: _Replay,
    verdicts: dict[int, Any],
    res_i: int,
    row: dict[str, Any],
) -> None:
    """An entry repeating an earlier `row` of the batch takes that row's outcome. A merge
    whose target a landed row rewrote in between lands instead, as the repeat would
    serially (`replay.landings`)."""
    key = str(row["memory_key"])
    res = state.results[res_i]
    done = replay.outcome[id(row)]
    target = verdict_target(verdicts.get(replay.index_of[id(row)]))
    if done == "verdict" and target and target in replay.rewritten:
        replay.stale.add(id(row))
        replay.land(row)
        replay.landings.append(_Landing(res_i, replay.result_of[id(row)], row, target))
    elif done == "verdict":
        replay.copies.append(_Copy(res_i, replay.result_of[id(row)], row))
    elif done == PINNED:
        res.conflict_verdict = PINNED
        res.pinned_blocker_id = str(row["memory_id"])
        state.skipped_pinned.append(key)
    else:
        state.skipped_unchanged += 1
        _note_restatement(state, str(row["memory_id"]))


def land_restated_merges(
    state: _BulkWriteState, plan: BulkVerdictPlan, replay: _Replay, verdicts: dict[int, Any]
) -> None:
    """Each `replay.landings` restatement answers its row landing (clear), and the earlier
    entry its merge, as serially. Runs before `copy_restated_verdicts`."""
    verdict_of = {id(state.memory_rows[i]): v for i, v in verdicts.items()}
    for res_i, src_i, row, target in replay.landings:
        src, res = state.results[src_i], state.results[res_i]
        # The row landed as the restatement's write: it met the new value, so no peer.
        res.version, res.memory_id = src.version, src.memory_id
        res.conflict_verdict, res.peers, res.next = "clear", [], set_next("clear", [])
        # The earlier entry answers its merge with the peers it was judged against (the
        # merge target among them): stamped as a stale clear, it named none.
        src.version, src.memory_id = 0, ""
        src.peers = set_peers(verdict_of.get(id(row)))
        src.conflict_verdict, src.next = "merge", set_next("merge", src.peers)
        plan.merged.append(src.memory_key)
        plan.reinforce_ids.append(target)
        # A copy of this merge reinforces the same target.
        plan.merged_into[id(row)] = target


def _drop_built_row(
    state: _BulkWriteState, res: _BulkSetResult, row: dict[str, Any], why: str
) -> None:
    """A built row `settle_batch_rows` keeps out: its result and counters say what
    the skip gate would have said had the earlier rows' fate been known."""
    res.version, res.memory_id = 0, ""
    if why == PINNED:
        res.conflict_verdict = PINNED
        res.pinned_blocker_id = str(row["memory_id"])
        state.skipped_pinned.append(str(row["memory_key"]))
    else:
        state.skipped_unchanged += 1
        _note_restatement(state, str(row["memory_id"]))
    # It was counted as a write of a key the batch had already seen.
    if state.existing_lookup_ok:
        state.n_updated -= 1
    else:
        state.n_unknown -= 1


def drop_built_rows(
    state: _BulkWriteState, emb_idx_of: list[int], verdicts: dict[int, Any], drop: set[int]
) -> tuple[list[int], dict[int, Any]]:
    """Remove the rows `drop` names (memory-row indexes) and their embedding rows, and
    renumber what points at rows. Returns the new `emb_idx_of` and verdicts."""
    if not drop:
        return emb_idx_of, verdicts
    new_idx: dict[int, int] = {}
    for i in range(len(state.memory_rows)):
        if i not in drop:
            new_idx[i] = len(new_idx)
    kept = [pos for pos, i in enumerate(emb_idx_of) if i not in drop]
    state.memory_rows = [r for i, r in enumerate(state.memory_rows) if i in new_idx]
    state.row_results = [r for i, r in enumerate(state.row_results) if i in new_idx]
    state.entry_metas = [m for i, m in enumerate(state.entry_metas) if i in new_idx]
    state.embedding_rows = [state.embedding_rows[pos] for pos in kept]
    state.pending = [(new_idx[i], t, x, k) for i, t, x, k in state.pending if i in new_idx]
    emb_idx_of = [new_idx[emb_idx_of[pos]] for pos in kept]
    verdicts = {new_idx[i]: v for i, v in verdicts.items() if i in new_idx}
    return emb_idx_of, verdicts


def copy_restated_verdicts(
    state: _BulkWriteState, plan: BulkVerdictPlan, copies: list[_Copy]
) -> None:
    """Each restatement of a merged or rejected row answers that row's verdict, as the
    same memory.set again would, and is listed under it in the `plan`; a merge's
    restatement reinforces the merge target."""
    for res_i, src_i, row in copies:
        src, res = state.results[src_i], state.results[res_i]
        res.conflict_verdict, res.peers, res.next = src.conflict_verdict, list(src.peers), src.next
        res.pinned_blocker_id = src.pinned_blocker_id
        if src.conflict_verdict == "merge":
            plan.merged.append(res.memory_key)
            _note_restatement(state, plan.merged_into.get(id(row), ""))
        elif src.conflict_verdict == "rejected":
            plan.rejected_pinned.append(res.memory_key)


def _retyped(state: _BulkWriteState, entry: Any) -> Any:
    """The entry with the memory type and tags its procedural contract asks for: the
    procedural type keeps its contract at every door."""
    retype = _procedural_retype(entry.memory_type, entry.value)
    if retype is None:
        return entry
    state.retyped.append(entry.memory_key)
    tags = list(entry.tags or [])
    return entry.model_copy(
        update={
            "memory_type": retype.memory_type,
            "tags": [*tags, *[t for t in retype.tags if t not in tags]],
        }
    )


def _row_id(state: _BulkWriteState, memory_key: str) -> str:
    """The key's live row id, else one minted once per batch for it."""
    return state.existing_ids.get(memory_key) or state.new_ids.setdefault(
        memory_key, str(_uuid.uuid4())
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
        expires_at = _entry_expiry(state, entry, entry_sha, now_utc)
        serialized_value = json.dumps(entry.value)
        decoded_value = detector_text(serialized_value)
        # Sensitivity tier: caller's level, the repeated value's, or detected from
        # the value; rides the row, its `sensitivity:` tag and the embed gate.
        repeated_level = _repeated_level(state, entry, entry_sha)
        entry_sensitivity = (
            repeated_level
            if repeated_level is not None
            else resolve_sensitivity(getattr(entry, "sensitivity", ""), decoded_value)
        )
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
        if _changes_locked_expiry(state, entry, entry_sha, expires_at, now_utc):
            _refuse_locked(state, entry.memory_key)
            continue
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
        original_entry = entry  # what a later restatement is compared with
        entry = _retyped(state, entry)
        entry_tags = list(entry.tags or [])
        entry_anchor_dt = parse_iso_dt(entry.temporal_anchor)
        entry_entities = resolved_entity_list(entry.entities or [], entity_resolution)
        # The protected-attribute and sensitivity labels ride the row's tags, and the
        # embedding row's: the ranking reads them there.
        row_tags = _with_sensitivity_tag(
            _with_attribute_tags(entry_tags, decoded_value, input.namespace),
            entry_sensitivity,
        )
        state.entry_metas.append((entry, serialized_value, row_tags, entry_entities, expires_at))
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
            "tags": row_tags,
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
            "memory_id": _row_id(state, entry.memory_key),
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
        _keep_row(state, memory_row, original_entry)
        if repeated_level is not None:
            state.inherited_level_rows.add(len(state.memory_rows) - 1)
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
                from server.memory.app import extraction_policy
                from server.memory.app.chunk_context import contextualize_chunks, prefixed

                document = "\n\n".join(excerpt for _, _, excerpt, _ in pending)
                # An unreadable policy lands in the fail-open below: no context.
                policy = await extraction_policy.effective(input.org, input.namespace)
                contexts = await contextualize_chunks(
                    get_llm_client(),
                    document,
                    [excerpt for _, _, excerpt, _ in pending],
                    concurrency=int(_cx_settings.memory_bulk_contextualize_concurrency),
                    operator_policy=policy,
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
