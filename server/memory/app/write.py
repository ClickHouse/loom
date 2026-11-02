"""memory.set and memory.update / update_by_id: the Expand and Revise write
path. Its helpers are imported and patched here, where they live."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from server.code.app.member_writes import assert_own_row
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import served_content_sha256
from server.core.hashing import sha256_value as _content_sha256
from server.core.vectors import codes_state
from server.embedding import get_embedder
from server.identity.app.request_scope import request_scope
from server.llm import customer
from server.memory.api.models_crud import (
    MemorySetInput,
    MemorySetOutput,
)
from server.memory.app import provenance as app_provenance
from server.memory.app.set_commit import (
    _set_after_write,
    _set_conflict_verdict,
    _set_dry_run_answer,
    _set_dual_write,
    _set_embed,
    _set_merge,
    _set_plain_insert,
    _SetWrite,
    count_attribute_tags,
    count_write,
)
from server.memory.domain import lock as _lock
from server.memory.domain import provenance as _prov
from server.memory.domain.expire_reasons import carried_reason
from server.memory.domain.files import files_for_set
from server.memory.domain.graph_quality import canonical_entities
from server.memory.domain.procedural_contract import retype_for as _procedural_retype
from server.memory.domain.value_shape import value_shape_advisories
from server.memory.store import key_write_lock
from server.memory.store.rows import next_version
from server.settings import settings

log = logging.getLogger(__name__)


# Leaf module (no internal imports) shared with memory.app.retrieval to
# avoid a circular import.
from server.memory.domain.memory_types import is_embedded, is_searchable_type  # noqa: E402

# Terminal failure shapes of `memory.set`, keyed by the `code` in the HTTPException detail.
# Mapped once at the handler boundary rather than counted at each raise site, so no refusal
# branch goes uncounted (a namespace whose writes are all refused must not look identical to
# one receiving no traffic). A new refusal branch is picked up here for free; only an
# unmapped code needs a line added.
_WRITE_OUTCOME_BY_ERROR_CODE: dict[str, str] = {
    "PINNED_CONFLICT": "rejected",
    "BLACKLISTED": "blacklisted",
    "CHAR_LIMIT_EXCEEDED": "too_large",
    "PRECONDITION_FAILED": "precondition_failed",
}


# Anything not in that map is not a refusal — see handle_memory_set.
_WRITE_OUTCOME_UNMAPPED = "error"


def stored_sha256(row: Any) -> str:
    """The hash `if_match_sha256` is checked against: of the row's stored value."""
    return "" if row is None else served_content_sha256(row.value)


def _guard_existing_row(
    existing: Any, input: MemorySetInput, retyped_from: str
) -> tuple[bool, MemorySetOutput | None]:
    """What the live row under this key permits a memory.set to do.

    Returns ``(restated, refusal)``. A byte-identical value is a restatement: it passes every
    guard, and a restatement of a pinned row keeps the pin. A changed value meets the lock
    first, then the pin: a pinned row is never evicted by a write that does not itself pin —
    409 PINNED_CONFLICT, the same refusal a close write against a pinned peer gets, applied
    also to the same key the peer search excludes. memory.update (keeps the pin) and
    memory.pin (re-pins) are the explicit ways to change a pinned value. A dry run answers
    the refusal as a verdict instead of raising.
    """
    assert_own_row(existing, op="memory.set")  # a code-namespace member's own rows
    if existing is None:
        return False, None
    existing_sha = getattr(existing, "content_sha256", "") or stored_sha256(existing)
    restated = _content_sha256(input.value) == existing_sha
    if not restated:
        _lock.assert_unlocked(existing, "value", op="memory.set")
    if input.clear_expiry or input.ttl_seconds is not None:
        _lock.assert_unlocked(existing, "expire", op="memory.set")
    if not getattr(existing, "pinned", False) or input.pinned:
        return restated, None
    if restated:
        input.pinned = True
        return restated, None
    blocker = str(getattr(existing, "memory_id", "") or "")
    if input.dry_run:
        return restated, _pinned_dry_run_output(input, blocker, retyped_from)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "PINNED_CONFLICT",
            "message": (
                f"memory.set: {input.memory_key!r} is pinned; a write that does not "
                "pin never replaces it. Change it with memory.update (keeps the pin) "
                "or memory.pin (re-pins), or store the new fact under its own key."
            ),
            "pinned_blocker_id": blocker,
            "peer_ids": [blocker],
            "memory_key": input.memory_key,
        },
    )


def _pinned_dry_run_output(
    input: MemorySetInput, blocker: str, retyped_from: str
) -> MemorySetOutput:
    return MemorySetOutput(
        retyped_from=retyped_from,
        version=0,
        pinned=False,
        memory_id=blocker,
        content_sha256=_content_sha256(input.value),
        namespace=input.namespace,
        memory_key=input.memory_key,
        conflict_verdict="rejected",
        next=(
            f"verdict=rejected: {input.memory_key!r} is pinned; change it with "
            "memory.update or memory.pin."
        ),
        noop=True,
        dry_run=True,
    )


async def handle_memory_set(
    input: MemorySetInput, *, op: str = "memory.set", retry_what: str = "the write"
) -> MemorySetOutput:
    """Count the write outcome for every terminal shape, then delegate.

    Successful shapes are counted inside `_handle_memory_set` where the conflict
    verdict is known. Failures are counted here so a branch added later cannot
    silently stop being counted: mapped codes get their specific outcome, an
    unmapped 4xx is a `rejected` refusal, and anything else — including the
    non-HTTPException failures a ClickHouse outage actually raises — is `error`.
    """
    try:
        out = await _handle_memory_set(input, op=op, retry_what=retry_what)
    except HTTPException as e:
        code = e.detail.get("code", "") if isinstance(e.detail, dict) else ""
        outcome = _WRITE_OUTCOME_BY_ERROR_CODE.get(str(code))
        if outcome is None:
            # Classify an UNMAPPED shape on the HTTP status, not the code string. A 4xx is
            # still a caller/policy refusal even when its code is not one of the four we map;
            # only 5xx is infrastructure, so a policy problem never points the operator at
            # infrastructure.
            outcome = "rejected" if 400 <= e.status_code < 500 else _WRITE_OUTCOME_UNMAPPED
        count_write(input.namespace, outcome)
        raise
    except Exception:
        # Infrastructure failures do NOT arrive as HTTPException: server/core/ch.py raises driver
        # exceptions or a bare RuntimeError, and the repository and namespace_types modules
        # raise no HTTPException at all. Counted here so a ClickHouse outage shows as
        # `error`, then re-raised untouched — this is an accounting hook, not error handling.
        count_write(input.namespace, _WRITE_OUTCOME_UNMAPPED)
        raise
    # A base64, double-encoded or over-cap value is stored as given (a value is a value) and
    # the response says so once per shape, so the agent can fix its side.
    advisories = value_shape_advisories(input.value, input.memory_type)
    if advisories:
        out.advisories = [*out.advisories, *advisories]
    return out


async def _set_resolve_type(input: MemorySetInput) -> tuple[MemorySetInput, str, str]:
    """Phase 1 of memory.set: settle the memory type the write is stored under.

    Returns the (possibly rebound) input, the type resolved after the classify
    hook but before the procedural retype — the retention default and the
    non-searchable warning key off it — and the type a retype moved away from
    ("" when none).
    """
    from server.observability.otel import traced

    # Optional classify hook overrides the caller-supplied type. Fail-open:
    # any failure falls back to input.memory_type. Rebind via model_copy so
    # every downstream reference sees the resolved type.
    if input.classify:
        try:
            from server.memory.app.classify import classify as _classify
            from server.memory.app.classify import get_namespace_classify_config
            from server.memory.app.namespace_types import list_type_configs

            # Spanned so the classify hook's share of the write path's cost is attributable.
            with traced("classify.config"):
                endpoint, prompt_template = await get_namespace_classify_config(
                    input.org,
                    input.namespace,
                )
            if endpoint:
                with traced("classify.type_configs"):
                    registered = await list_type_configs(input.org, input.namespace)
                valid_types = [r["memory_type"] for r in registered]
                # An outbound HTTP call to an operator-supplied endpoint, on the write path.
                with traced("classify.call", endpoint_configured=True):
                    classified = await _classify(
                        endpoint=endpoint,
                        prompt_template=prompt_template,
                        value=json.dumps(input.value),
                        valid_types=valid_types,
                    )
                if classified:
                    input = input.model_copy(update={"memory_type": classified})
        except customer.HOLDS:
            raise  # a refusal or an unread switch, not a classifier fault
        except Exception as e:  # noqa: BLE001 — classify failure keeps the caller-supplied type
            log.warning("classify failed (non-fatal); using caller-supplied type: %s", e)
    resolved_memory_type = input.memory_type

    # The procedural type keeps its contract at every door: a procedural declaration on a
    # value that is not a procedure body is stored as semantic, tagged with where it came
    # from, and the response says so. The value is untouched.
    retyped_from = ""
    _retype = _procedural_retype(input.memory_type, input.value)
    if _retype is not None:
        retyped_from = input.memory_type
        input = input.model_copy(
            update={
                "memory_type": _retype.memory_type,
                "tags": [
                    *(input.tags or []),
                    *[t for t in _retype.tags if t not in (input.tags or [])],
                ],
            }
        )
        log.info(
            "memory.set retyped procedural→%s key=%r ns=%s: %s",
            _retype.memory_type,
            input.memory_key,
            input.namespace,
            _retype.reason,
        )

    # Server-side non-searchable-type guard. episodic/profile carry NO embedding and never
    # surface in memory.search — durable, recallable content written there (e.g. PR
    # summaries stored as episodic) is invisible to recall. MCP strips these from the type
    # enum, but the SDK/HTTP path can still send them, so the guard belongs HERE,
    # server-side. Warn loudly (greppable + a telemetry counter) rather than reject, since a
    # caller may deliberately store a non-searchable record.
    if not is_searchable_type(resolved_memory_type):
        log.warning(
            "memory.set wrote a NON-SEARCHABLE memory_type=%r (org=%s ns=%s "
            "key=%s): it gets no embedding and will NOT appear in "
            "memory.search. Use 'semantic' for recallable memories.",
            resolved_memory_type,
            input.org,
            input.namespace,
            input.memory_key,
        )
    return input, resolved_memory_type, retyped_from


async def _set_fetch_existing(input: MemorySetInput) -> tuple[Any, Any]:
    """Phase 2 of memory.set: the live row under this key and the write-config prefetch.

    Returns (the scheduled write-config task, the existing row or None).
    """
    # Fetch existing up front to preserve fields that survive an update
    # (created_at, expires_at). Single read, reused below. The namespace's
    # write config rides the same round trip.
    import server.memory.store as _mr
    from server.memory.app import conflicts as _cf
    from server.observability.otel import traced

    _cfg_task = _cf.start_write_config(
        input.org, input.namespace, input.memory_type, input.memory_key
    )
    with traced("ch.query.existing_memory"):
        existing = await _mr.get_by_key(
            _mr.Identity(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id,
                session_id=input.session_id,
            ),
            memory_key=input.memory_key,
            fresh=True,
            strong=True,  # the guard sees the latest version
            scope=request_scope(),
        )
    return _cfg_task, existing


async def _set_expiry(
    input: MemorySetInput, existing: Any, _cfg_task: Any, resolved_memory_type: str
) -> datetime | None:
    """Phase 3 of memory.set: the expiry the new version row carries."""
    from server.memory.app import conflicts as _cf
    from server.observability.otel import traced

    expires_at = None
    if input.clear_expiry:
        # Explicit "never expire" — skip defaults so a tag retention can't
        # immediately re-stamp an expiry.
        expires_at = None
    elif input.ttl_seconds is not None:
        expires_at = datetime.now(UTC) + timedelta(seconds=input.ttl_seconds)
    elif (
        existing is not None
        and existing.expires_at is not None
        and existing.expires_at > naive_utc_now()
    ):
        # Preserve a LIVE expiry — edits to other fields must not silently
        # change the user's expiry choice. A past expiry is not preserved:
        # re-setting an expired key is a fresh write, not a revival of a
        # row the reads already hide.
        expires_at = existing.expires_at
    else:
        # New row (or had no expiry) — apply per-type retention default
        # if declared. NULL → table-level TTL.
        with traced("ch.query.type_config"):
            type_cfg = await _cf.type_config_for(_cfg_task, input, resolved_memory_type)
        if type_cfg.retention_days > 0:
            expires_at = datetime.now(UTC) + timedelta(days=type_cfg.retention_days)
    return expires_at


async def _set_prepare_value(w: _SetWrite) -> None:
    """Phase 4 of memory.set: serialize the value and gate it before any write.

    Sets serialized_value, new_sha, the reinforcement fields, the sensitivity
    tier, the sensitive retention (expires_at, on_expire) and the label tags
    on `w.input`. Raises 422 CHAR_LIMIT_EXCEEDED / BLACKLISTED.
    """
    from server.observability.otel import traced

    input, existing, expires_at, restated = w.input, w.existing, w.expires_at, w.restated

    serialized_value = json.dumps(input.value)
    new_sha = _content_sha256(input.value)

    # The set path honors the caller's key (no cross-key consolidation), so a new or changed
    # value starts at 1. A byte-identical restatement of the live value is the frequency
    # signal: the version row carries base + 1, and the usage fold propagates the base to the
    # embeddings row's search-side boost.
    reinforcement_count_for_insert = (
        int(getattr(existing, "reinforcement_count", 1) or 1) + 1 if restated else 1
    )
    last_reinforced_at_for_insert = naive_utc_now()
    # When the fact was last said, for the maintenance age rule: a restatement is a fresh
    # mention; a new value keeps the caller's/extractor's date (NULL here — the bulk path
    # sets it).
    mentioned_at_for_insert = last_reinforced_at_for_insert if restated else None

    # Server-enforced char cap, measured against the canonical JSON form
    # (what we store). NULL = no cap.
    if input.char_limit is not None and len(serialized_value) > input.char_limit:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CHAR_LIMIT_EXCEEDED",
                "message": (
                    f"value length {len(serialized_value)} exceeds char_limit {input.char_limit}"
                ),
                "value_length": len(serialized_value),
                "char_limit": input.char_limit,
            },
        )

    # Blacklist enforcement ("Refuse") — raises 422 BLACKLISTED before any
    # write if `value` matches a global/org/namespace pattern.
    from server.memory.app.blacklist_guard import enforce_on_insert

    with traced("blacklist.enforce"):
        await enforce_on_insert(
            org=input.org,
            namespace=input.namespace,
            value=serialized_value,
        )

    # Sensitivity tier: the caller's level, else detected from the value. 'high' rows take
    # the non-embedded path below.
    from server.memory.domain.sensitivity import detector_text, resolve_sensitivity

    # Detected on the decoded value: in the stored JSON a token after a newline loses its
    # word boundary.
    decoded_value = detector_text(serialized_value)
    effective_sensitivity = resolve_sensitivity(getattr(input, "sensitivity", ""), decoded_value)
    # A sensitive row carries its retention window: the caller's or the type's expiry stays
    # when shorter; on_expire defaults to the sensitive post-action when the caller set none.
    from server.memory.domain.sensitive_retention import sensitive_retention

    expires_at, _sens_on_expire, _sens_windowed = sensitive_retention(
        sensitivity=effective_sensitivity,
        expires_at=expires_at,
        on_expire=input.on_expire
        or ((getattr(existing, "on_expire", "") or "") if existing is not None else ""),
        now=datetime.now(UTC),
        settings=settings,
    )
    # A new expiry from the retention window is an expiry change: a read_only lock refuses
    # it as it refuses a ttl_seconds (423, refused_action 'expire'), as memory.bulk_set does.
    if expires_at != w.expires_at:
        _lock.assert_unlocked(existing, "expire", op="memory.set")
    # Protected-attribute label: a tag, never a refusal.
    from server.memory.domain.sensitivity import attribute_tags

    _attr_tags = attribute_tags(decoded_value, input.tags or [])
    if _attr_tags:
        input = input.model_copy(update={"tags": [*(input.tags or []), *_attr_tags]})
    # The sensitivity tier rides the tags too: the embeddings table has no sensitivity
    # column, and the ranking demotes labelled rows on questions that did not ask for a
    # contact detail.
    from server.memory.domain.sensitivity import sensitivity_tags

    _sens_tags = sensitivity_tags(effective_sensitivity, input.tags or [])
    if _sens_tags:
        input = input.model_copy(update={"tags": [*(input.tags or []), *_sens_tags]})
        count_attribute_tags(input.namespace, _attr_tags)
    w.input = input
    w.serialized_value = serialized_value
    w.new_sha = new_sha
    w.reinforcement_count = reinforcement_count_for_insert
    w.last_reinforced_at = last_reinforced_at_for_insert
    w.mentioned_at = mentioned_at_for_insert
    w.effective_sensitivity = effective_sensitivity
    w.expires_at = expires_at
    w.on_expire = _sens_on_expire


async def _set_check_if_match(w: _SetWrite) -> None:
    """Phase 5 of memory.set: the if_match_sha256 precondition (409 PRECONDITION_FAILED).

    When the caller sent a hash, `w.existing` is re-read strongly and rebound.
    """
    input, existing = w.input, w.existing

    # Optimistic concurrency: verify the live row's hash matches before
    # accepting. Mismatch → 409 so the caller can re-read and retry.
    if input.if_match_sha256:
        import server.memory.store as memory_repo

        existing = await memory_repo.get_by_key(
            memory_repo.Identity(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id,
                session_id=input.session_id,
            ),
            input.memory_key,
            fresh=True,
            # The precondition compares against the LATEST version; a lagging
            # replica returning the previous version would pass a stale hash
            # and let a read-modify-write land twice.
            strong=True,
            scope=request_scope(),
        )
        current_sha = stored_sha256(existing)
        if current_sha != input.if_match_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PRECONDITION_FAILED",
                    "message": (
                        "if_match_sha256 does not match current row "
                        f"(current_sha256={current_sha!r})"
                    ),
                    "current_sha256": current_sha,
                },
            )
    w.existing = existing


def _set_build_memory_row(w: _SetWrite) -> None:
    """Phase 6 of memory.set: the loom.memory version row, every column filled.

    Sets memory_id, had_existing_row, the server-stamped writer_actor and
    source_class, and memory_row itself. Nothing is written here.
    """
    import uuid as _uuid

    input, existing, version = w.input, w.existing, w.version
    serialized_value, new_sha, expires_at = w.serialized_value, w.new_sha, w.expires_at
    reinforcement_count_for_insert = w.reinforcement_count
    last_reinforced_at_for_insert = w.last_reinforced_at
    mentioned_at_for_insert = w.mentioned_at
    effective_sensitivity, _sens_on_expire = w.effective_sensitivity, w.on_expire

    # Carry forward the original created_at across updates (else the RMT
    # winner takes the schema-default now() and loses the creation moment).
    # updated_at always stamps to now.
    now_naive = naive_utc_now()
    preserved_created_at = (
        existing.created_at if existing is not None and existing.created_at else now_naive
    )
    memory_id = (
        existing.memory_id
        if existing is not None and bool(getattr(existing, "memory_id", ""))
        else str(_uuid.uuid4())
    )
    # Captured here because `existing` is rebound inside the merge branch below. It
    # distinguishes a first write to this key from a new version of one that already
    # existed — both insert a row and reuse the same memory_id, but "learning something new"
    # vs "restating what we had" is the question the writes panel asks.
    had_existing_row = existing is not None

    # Server-decided provenance: the writer from the token, the class from the op +
    # source_trace convention; the body may only downgrade to external / imported.
    writer_actor = app_provenance.writer_actor()
    source_class = _prov.source_class_for(
        default="asserted",
        requested=getattr(input, "source_class", None),
        source_trace=input.source_trace,
    )

    memory_row = {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "user_id": input.user_id,
        "session_id": input.session_id,
        "run_id": input.run_id,
        "memory_key": input.memory_key,
        "memory_type": input.memory_type,
        "value": serialized_value,
        # CH JSON type rejects primitives; populate value_json only for
        # dict-shaped values, primitives stay in `value`.
        "value_json": (serialized_value if isinstance(input.value, dict) else "{}"),
        "confidence": input.confidence,
        "source_trace": input.source_trace,
        "actor": writer_actor,
        "source_class": source_class,
        # The embedding row below carries it too; the row every mirror copies from must.
        "source_resource_id": input.source_resource_id,
        "version": version,
        "tombstoned_at": None,
        "expires_at": expires_at,
        "valid_at": input.valid_at,
        "valid_to": input.valid_to,
        "char_limit": input.char_limit,
        "pinned": 1 if input.pinned else 0,
        "memory_id": memory_id,
        "tags": list(input.tags or []),
        "created_at": preserved_created_at,
        "updated_at": now_naive,
        "content_sha256": new_sha,
        "reinforcement_count": reinforcement_count_for_insert,
        "last_reinforced_at": last_reinforced_at_for_insert,
        "mentioned_at": mentioned_at_for_insert,
        # A lock rides every version row: a permitted write (a no_delete value change, a
        # read_only restatement) keeps it.
        "lock_mode": getattr(existing, "lock_mode", "") or "" if existing is not None else "",
        "lock_reason": getattr(existing, "lock_reason", "") or "" if existing is not None else "",
        "lock_until": getattr(existing, "lock_until", None) if existing is not None else None,
        "locked_by": getattr(existing, "locked_by", "") or "" if existing is not None else "",
        "locked_at": getattr(existing, "locked_at", None) if existing is not None else None,
        # Expiry post-action: the caller's, else the row's.
        "on_expire": _sens_on_expire,
        "expire_reason": (input.expire_reason or "")[:512]
        or (carried_reason(getattr(existing, "expire_reason", "")) if existing is not None else ""),
        # Canonical, as search reads them; the embedding row copies them. This key must be
        # present: `ch.insert` derives its column list from `rows[0].keys()`, so an omission
        # silently writes the schema default. `find_unentitied_memories` selects on
        # `empty(loom.memory.entities)`, so a row without the caller's entities looks
        # unentitied to the entity_extractor worker, which re-extracts, finds nothing for a
        # short value, and writes its `["_none_"]` sentinel to BOTH tables — destroying the
        # caller's entities.
        "entities": canonical_entities(input.entities or []),
        # The caller's files, else those the value names, else the row's.
        "files": files_for_set(input.files, input.value, serialized_value, existing),
        "concepts": [],
        "sensitivity": effective_sensitivity,
    }
    if existing is not None:
        # A revision: every column not set above (usage counters, lineage) comes
        # from the live row; the builder resets the ones about the old value.
        import server.memory.store as memory_repo

        memory_row = memory_repo.build_revision_row(existing, memory_row)
    else:
        import server.memory.store as memory_repo

        memory_row = memory_repo.build_first_version_row(memory_row, now=now_naive)
    w.memory_id = memory_id
    w.had_existing_row = had_existing_row
    w.writer_actor = writer_actor
    w.source_class = source_class
    w.memory_row = memory_row


async def _handle_memory_set(
    input: MemorySetInput, *, op: str = "memory.set", retry_what: str = "the write"
) -> MemorySetOutput:
    """memory.set; a conditional write and a pin hold the key's write lock from their read
    to their insert, as a resolver write does, so a retirement never lands between them."""
    if not (input.if_match_sha256 or input.pinned):
        return await _memory_set_phases(input)
    # A pin rewrites the row from its read: a retirement landing between the two would be
    # written over by the pinned copy, active again, so the pin holds the lock across both.
    if not input.if_match_sha256:  # a pin: busy, the answer says what to retry
        async with key_write_lock.key_lock_for(
            input.org,
            input.namespace,
            input.agent,
            input.user_id,
            input.session_id,
            input.memory_key,
            what=retry_what,
            operation=op,
        ):
            return await _memory_set_phases(input)
    async with key_write_lock.key_write_locks.hold(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_key,
    ):
        return await _memory_set_phases(input)


async def _memory_set_phases(input: MemorySetInput) -> MemorySetOutput:
    """memory.set, as its phases in order (each `_set_*` above says what it does).

    The shared request state is `w` (_SetWrite). Early returns: a lock/pin
    refusal, the non-embedded path, a dry-run 'rejected', a merge, a dry run.
    """
    ch = get_client()
    version = next_version()

    input, resolved_memory_type, retyped_from = await _set_resolve_type(input)

    _cfg_task, existing = await _set_fetch_existing(input)

    # Same key, same bytes → a reinforcement. A lock or a pin refuses a changed value before
    # anything is embedded.
    _restated, _pin_refusal = _guard_existing_row(existing, input, retyped_from)
    if _pin_refusal is not None:
        return _pin_refusal

    expires_at = await _set_expiry(input, existing, _cfg_task, resolved_memory_type)

    w = _SetWrite(
        input=input,
        version=version,
        retyped_from=retyped_from,
        existing=existing,
        expires_at=expires_at,
        restated=_restated,
    )
    await _set_prepare_value(w)
    if input.proposed:
        await _refuse_proposal_over_fact(input)
    await _set_check_if_match(w)
    if input.if_match_sha256:
        # The precondition was read under the key lock just now: the version is drawn
        # after it, so only a delete that lands after the check is above it (write_fence).
        w.version = next_version()
    _set_build_memory_row(w)

    # Non-searchable type, or a 'high' sensitivity row: single insert is the
    # only work — no embedding row means no search leg can return it.
    if not is_embedded(w.input.memory_type, w.effective_sensitivity):
        return await _set_plain_insert(w, ch)

    # Searchable: build + insert the embedding row too. The two inserts
    # hit different tables and don't depend on each other; parallelize to
    # pay 1×RTT instead of 2 (RTT dominates on CH Cloud). A vector the coded
    # table cannot take fails the write before the calls it pays for.
    await codes_state.refuse_width("memory_embeddings", get_embedder().dim)
    await _set_embed(w)

    _cfg, conflict_result, peers_out, _rejected_dry_run = await _set_conflict_verdict(w, _cfg_task)
    if _rejected_dry_run is not None:
        return _rejected_dry_run

    if conflict_result.verdict == "merge":
        return await _set_merge(w, ch, _cfg, conflict_result, peers_out)

    # duplicate tier: very-high similarity, canonical-form mismatch.
    # Insert proceeds (unlike merge) with status='duplicate' +
    # duplicate_of pointing at the canonical, surfaced to the caller.
    duplicate_of_id = _stamp_lifecycle(w.memory_row, conflict_result, w.input)

    if w.input.dry_run:
        # The verdict is the answer. No loom.memory row, no embedding row, no conflict marks,
        # no supersession, no entity projection, no audit hash (that row records writes; none
        # happened), no write tick. The version is 0 so nothing can mistake it for one.
        return _set_dry_run_answer(w, conflict_result, peers_out, duplicate_of_id)

    conflict_result = await _set_dual_write(w, ch, _cfg, conflict_result)
    if conflict_result.verdict == "clear":
        peers_out = []  # a conflict the mark found no live peer for: none to resolve

    return await _set_after_write(w, ch, _cfg, conflict_result, duplicate_of_id, peers_out)


def _stamp_lifecycle(
    memory_row: dict[str, Any], conflict_result: Any, input: MemorySetInput
) -> str:
    """The row's lifecycle status before insert; returns `duplicate_of`.
    A duplicate verdict (the canonical already holds the fact) wins over
    `proposed` (a fact on trial)."""
    if conflict_result.verdict == "duplicate":
        dup = conflict_result.duplicate_of_id or ""
        memory_row["status"], memory_row["duplicate_of"] = "duplicate", dup
        return dup
    if input.proposed:
        memory_row["status"] = "proposed"
    return ""


async def _refuse_proposal_over_fact(input: MemorySetInput) -> None:
    """A proposal may not land on a key that holds an established fact under any identity of
    the namespace — same-key supersession would retire that fact in favour of an untested
    one."""
    import server.memory.store as memory_repo

    held = await memory_repo.key_holds_established_fact(
        input.org, input.namespace, input.memory_key
    )
    if held:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "KEY_HOLDS_FACT",
                "message": (
                    f"{input.memory_key!r} already holds an accepted fact in "
                    f"{input.namespace}; a proposal would retire it. Propose "
                    "under a new key, or revise the fact without `proposed`."
                ),
            },
        )
