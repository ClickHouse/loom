"""Handlers of the bi-temporal ops and of the bi-temporal form of memory.get / list."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from server.core.errors import (
    LoomServerError,
    PreconditionFailedError,
    TemporalUnavailableError,
    ValidationError,
)
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import (
    MemoryGetByIdInput,
    MemoryGetByIdOutput,
    MemoryGetInput,
    MemoryGetOutput,
    MemoryListInput,
    MemoryListOutput,
    _MemoryListEntry,
)
from server.memory.api.models_temporal import (
    MemoryAmendInput,
    MemoryAmendOutput,
    MemoryIntervalsInput,
    MemoryIntervalsOutput,
    MemoryTemporalDiffInput,
    MemoryTemporalDiffOutput,
    MemoryTemporalStatusInput,
    MemoryTemporalStatusOutput,
    _TemporalChange,
)
from server.memory.app import temporal_publisher as publisher
from server.memory.app import temporal_reads as reads
from server.memory.domain import temporal_model as tm
from server.memory.domain.provenance import source_class_for
from server.memory.domain.temporal_capture import WriteIntent
from server.memory.domain.temporal_reads import Axes, answer, resolve_axes
from server.memory.domain.trust import verification_state
from server.memory.store import temporal as store
from server.memory.store.rows import MemoryRow
from server.settings import settings


async def get(input: MemoryGetInput, *, memory_id: str = "") -> MemoryGetOutput:
    """memory.get at (V, K): the assertions of the key that apply at V, as the
    journal recorded them at K. The exact identity is tried first, then the same
    user's rows under any agent, as the current-state read does; with `memory_id`
    (a get_by_id), only that memory's rows under its own identity."""
    unsupported = [f for f in _GET_UNSUPPORTED if getattr(input, f)]
    if unsupported:
        raise ValidationError(f"a historical memory.get does not take {', '.join(unsupported)}")
    axes = reads.axes_of(input)
    cut = await reads.cutoff(
        input.org, input.namespace, axes, read_token=input.read_token, policy=input.validity_policy
    )
    rows = await reads.served(
        input.org,
        cut,
        scope=request_scope(),
        memory_keys=[input.memory_key],
        user_id=input.user_id,
        session_id=input.session_id,
        include_high=True,
    )
    if memory_id:
        mine = [h for h in rows if h.agent == input.agent and h.memory_id == memory_id]
    else:
        mine = [h for h in rows if h.agent == input.agent] or rows
    held = reads.held_newest(mine, cut)
    if held is None:
        return MemoryGetOutput(found=False, temporal=cut.wire())
    hs, disputed = held
    top = max(hs, key=lambda h: (h.born_seq, h.a.valid_from or tm.MIN))
    await _audit_high(input, hs)
    a = top.a
    return MemoryGetOutput(
        found=True,
        memory_id=top.memory_id,
        value=reads.decode_value(a.value),
        memory_type=a.memory_type,
        confidence=a.confidence,
        version=a.source_version or None,
        expires_at=a.expires_at,
        valid_at=a.valid_from,
        valid_to=a.valid_to,
        pinned=a.pinned,
        actor=a.actor,
        source_class=a.source_class,
        agent=top.agent,
        status=a.status,
        tags=list(a.tags),
        entities=list(a.entities),
        content_sha256=a.content_sha256,
        verified_by=a.verified_by,
        verified_at=a.verified_at,
        verification=verification_state(a.verified_by, a.verified_sha256, a.content_sha256),
        lock_mode=a.lock_mode,
        sensitivity=a.sensitivity,
        temporal=cut.wire(),
        assertions=[h.wire() for h in hs],
        disputed=disputed,
    )


async def get_by_id(input: MemoryGetByIdInput) -> MemoryGetByIdOutput:
    """memory.get_by_id at (V, K): the id resolves through the journal, so a
    memory deleted since is still found at an earlier K. A namespace outside the
    caller's grant reads as not found."""
    from server.auth.authz import row_in_scope

    reads.axes_of(input)  # a bad axis is refused before the id is looked up
    where = await store.identity_of_memory_id(input.org, input.memory_id)
    if where is None or not await row_in_scope(input.org, str(where["namespace"])):
        return MemoryGetByIdOutput(found=False)
    got = await get(
        MemoryGetInput(
            org=input.org,
            namespace=str(where["namespace"]),
            agent=str(where["agent"]),
            user_id=str(where["user_id"]),
            session_id=str(where["session_id"]),
            memory_key=str(where["memory_key"]),
            valid_as_of=input.valid_as_of,
            known_as_of=input.known_as_of,
            read_token=input.read_token,
            validity_policy=input.validity_policy,
        ),
        memory_id=str(uuid.UUID(input.memory_id)),
    )
    if not got.found:
        return MemoryGetByIdOutput(found=False, temporal=got.temporal)
    return MemoryGetByIdOutput(
        found=True,
        memory_id=got.memory_id,
        namespace=str(where["namespace"]),
        agent=got.agent,
        user_id=str(where["user_id"]),
        session_id=str(where["session_id"]),
        memory_key=str(where["memory_key"]),
        memory_type=got.memory_type,
        value=got.value,
        confidence=got.confidence,
        version=got.version,
        expires_at=got.expires_at,
        valid_at=got.valid_at,
        valid_to=got.valid_to,
        pinned=got.pinned,
        content_sha256=got.content_sha256,
        verified_by=got.verified_by,
        actor=got.actor,
        source_class=got.source_class,
        verified_at=got.verified_at,
        verification=got.verification,
        sensitivity=got.sensitivity,
        temporal=got.temporal,
        assertions=got.assertions,
        disputed=got.disputed,
    )


# memory.list options a historical read cannot answer: their columns (scores,
# tombstones, source trace, the namespace version) are not in the journal.
_LIST_UNSUPPORTED = (
    "count_only",
    "quotas",
    "include_tombstoned",
    "include_superseded",
    "source_trace",
    "known_version",
)
# memory.get options with no historical answer: a redirect, the live version poll
# and the recall counters are today's state.
_GET_UNSUPPORTED = ("follow", "if_version", "include_recall_breakdown")


async def list_at(input: MemoryListInput) -> MemoryListOutput:
    """memory.list at (V, K): one entry per key with an assertion that applies at
    V, as recorded at K, with the live list's filters applied after the served set
    is fixed."""
    from server.memory.app.query import _preview_for_list
    from server.operations import read_projection

    unsupported = [f for f in _LIST_UNSUPPORTED if getattr(input, f)]
    if input.sort_by == "score":
        unsupported.append("sort_by=score")
    if unsupported:
        raise ValidationError(f"a historical memory.list does not take {', '.join(unsupported)}")
    keep = read_projection.resolve(
        _MemoryListEntry, list(input.fields), always=read_projection.LIST_ENTRY_ALWAYS
    )
    axes = reads.axes_of(input)
    cut = await reads.cutoff(
        input.org, input.namespace, axes, read_token=input.read_token, policy=input.validity_policy
    )
    rows = await reads.served(
        input.org,
        cut,
        scope=request_scope(),
        agent=None if input.agent == "*" else input.agent,
        user_id=None if input.user_id == "*" else input.user_id,
        memory_type=input.memory_type or None,
        tags=input.tags,
    )
    include = reads.statuses(
        include_proposed=input.include_proposed, include_duplicates=input.include_duplicates
    )
    needle = (input.contains or "").casefold()
    entries = []
    for (_, _, _, key), (hs, _) in sorted(reads.held_at(rows, cut, include=include).items()):
        h = max(hs, key=lambda x: x.born_seq)
        a = h.a
        if input.prefix and not key.startswith(input.prefix):
            continue
        if input.memory_type and a.memory_type != input.memory_type:
            continue
        if input.tags and not set(input.tags) & set(a.tags):
            continue
        if needle and needle not in key.casefold() and needle not in a.value.casefold():
            continue
        value, cut_off = _preview_for_list(a, input.preview_chars)
        entry = _MemoryListEntry(
            memory_key=key,
            memory_type=a.memory_type,
            confidence=a.confidence,
            version=a.source_version,
            created_at=h.born_at or axes.valid,
            valid_at=a.valid_from,
            expires_at=a.expires_at,
            status=a.status,
            pinned=a.pinned,
            verified_by=a.verified_by,
            verification=verification_state(a.verified_by, a.verified_sha256, a.content_sha256),
            tags=list(a.tags),
            agent=h.agent,
            user_id=h.user_id,
            session_id=h.session_id,
            value=value,
            value_truncated=cut_off,
        )
        if keep is not None:
            entry._projection = keep
        entries.append((entry, a))
    # `recency`, the only order a historical list takes: newest first, then key.
    entries.sort(key=lambda e: (-(e[0].created_at or axes.valid).timestamp(), e[0].memory_key))
    page = entries[input.offset : input.offset + input.limit]
    out = MemoryListOutput(
        entries=[e for e, _ in page],
        truncated=len(entries) > input.offset + input.limit,
        total=len(entries),
        temporal=cut.wire(),
    )
    _record_exposure(
        "memory.list", input, [a for _, a in page], [e.memory_key for e, _ in page], out.temporal
    )
    return out


def _record_exposure(
    operation: str,
    input: Any,
    shown: list[tm.Assertion],
    keys: list[str],
    temporal: Any,
) -> None:
    """The exposure row of a historical read: the exact revisions shown and the cutoff."""
    from server.memory.app.exposure_ledger import Receipt
    from server.memory.app.query import _record_read_exposure

    receipt = Receipt(
        ids=tuple(a.assertion_id for a in shown),
        sha256=tuple(a.value_sha256 for a in shown),
        cutoff_seq=temporal.cutoff_seq,
        valid_as_of=temporal.valid_as_of,
        known_as_of=temporal.known_as_of,
    )
    _record_read_exposure(operation, input, keys, receipt)


async def handle_memory_intervals(input: MemoryIntervalsInput) -> MemoryIntervalsOutput:
    scope = request_scope()
    axes = reads.axes_of(input)
    cut = await reads.cutoff(
        input.org, input.namespace, axes, read_token=input.read_token, policy=input.validity_policy
    )
    who: dict[str, Any] = {
        "agent": input.agent,
        "user_id": input.user_id,
        "session_id": input.session_id,
    }
    rows = await reads.served(
        input.org,
        cut,
        scope=scope,
        memory_keys=[input.memory_key],
        include_high=True,
        limit=input.limit,
        **who,
    )
    rows.sort(key=lambda h: (h.a.valid_from or tm.MIN, h.born_seq))
    held = reads.held_newest(rows, cut)
    retired: list[reads.Held] = []
    if input.include_retired:
        served_ids = {h.a.assertion_id for h in rows}
        retired = [
            h
            for h in await reads.history(
                input.org, cut, input.memory_key, scope=scope, limit=input.limit, **who
            )
            if h.a.assertion_id not in served_ids
        ]
    await _audit_high(input, [*rows, *retired])
    return MemoryIntervalsOutput(
        memory_key=input.memory_key,
        assertions=[h.wire() for h in rows],
        retired=[h.wire() for h in retired],
        at_valid=[h.a.assertion_id for h in held[0]] if held else [],
        disputed=bool(held and held[1]),
        temporal=cut.wire(),
    )


async def _audit_high(input: Any, held: list[reads.Held]) -> None:
    """A key read that returns a 'high' value leaves an audit row, as memory.get does."""
    high = next((h for h in held if h.a.sensitivity == "high"), None)
    if high is None:
        return
    from server.observability import audit as _audit

    await _audit.emit(
        "memory.sensitive_read",
        org=input.org,
        namespace=input.namespace,
        actor=input.agent,
        subject={"memory_key": high.memory_key, "memory_id": high.memory_id},
    )


def _page(changes: list[_TemporalChange], limit: int, cut: reads.Cut) -> list[_TemporalChange]:
    """The first `limit` changes; more is a gap on the cutoff the answer carries."""
    if len(changes) > limit:
        cut.gaps.append(f"truncated:{limit}")
    return changes[:limit]


async def handle_memory_temporal_diff(input: MemoryTemporalDiffInput) -> MemoryTemporalDiffOutput:
    scope = request_scope()
    who: dict[str, Any] = {
        "agent": input.agent,
        "user_id": input.user_id,
        "session_id": input.session_id,
    }
    now = datetime.now(UTC)
    if input.mode == "world":
        axes = resolve_axes(None, input.at)
        cut = await reads.cutoff(input.org, input.namespace, axes)
        rows = await reads.served(
            input.org, cut, scope=scope, memory_keys=[input.memory_key], include_high=True, **who
        )
        await _audit_high(input, rows)
        lo, hi = tm.utc(input.start), tm.utc(input.end)
        changes: list[_TemporalChange] = []
        for h in rows:
            a = h.a
            edges = [
                (a.valid_from, "occurred" if a.temporal_kind in ("event", "snapshot") else "began")
            ]
            if a.temporal_kind not in ("event", "snapshot"):
                edges.append((a.valid_to, "ended"))
            for at, kind in edges:
                if at is not None and (lo is None or at >= lo) and (hi is None or at < hi):
                    changes.append(
                        _TemporalChange(at=at, kind=kind, commit_seq=h.born_seq, assertion=h.wire())
                    )
        changes.sort(key=lambda c: c.at or now)
        return MemoryTemporalDiffOutput(
            memory_key=input.memory_key,
            mode="world",
            changes=_page(changes, input.limit, cut),
            temporal=cut.wire(),
        )
    v = tm.utc(input.at) or now
    axes = Axes(valid=v, known=tm.utc(input.end))
    cut = await reads.cutoff(input.org, input.namespace, axes)
    history = await reads.history(input.org, cut, input.memory_key, scope=scope, limit=5000, **who)
    await _audit_high(input, history)
    lo_seq = 0
    if input.start is not None:
        lo_cut = await reads.cutoff(
            input.org, input.namespace, Axes(valid=v, known=tm.utc(input.start))
        )
        lo_seq = lo_cut.seq
    commits = await store.commits_between(
        input.org, input.namespace, lo_seq, cut.seq, memory_keys=[input.memory_key]
    )
    kind_at = {
        int(c["commit_seq"]): (str(c["change_kind"]), tm.utc(c["recorded_at"]))
        for c in commits
        if all(w in (None, "*") or str(c[col]) == w for col, w in who.items())
    }

    def held_at(c: int) -> list[reads.Held]:
        live = [
            h for h in history if h.born_seq <= c and (h.retired_seq is None or h.retired_seq > c)
        ]
        index = {h.a.assertion_id: h for h in live}
        return [index[a.assertion_id] for a in answer([h.a for h in live], v)[0]]

    def state(held: list[reads.Held]) -> list[str]:
        """What a reader at V is told: each assertion's whole recorded state but its
        ids (value, validity, classification, tags, assessments, expiry)."""
        return sorted(
            repr(sorted((k, v) for k, v in h.a.to_json().items() if k not in _ASSERTION_IDS))
            for h in held
        )

    changes = []
    before = held_at(lo_seq)
    for c in sorted(kind_at):
        after = held_at(c)
        if state(after) != state(before):
            kind, at = kind_at[c]
            changes.append(
                _TemporalChange(
                    at=at,
                    kind=kind,
                    commit_seq=c,
                    before=[reads.decode_value(h.a.value) for h in before],
                    after=[reads.decode_value(h.a.value) for h in after],
                    before_assertions=[h.wire() for h in before],
                    after_assertions=[h.wire() for h in after],
                )
            )
            before = after
    return MemoryTemporalDiffOutput(
        memory_key=input.memory_key,
        mode="belief",
        changes=_page(changes, input.limit, cut),
        temporal=cut.wire(),
    )


# An assertion's identifiers, not its state: a revision changes them and nothing else.
_ASSERTION_IDS = tm.IDENTITY_FIELDS | {"embed_input_sha256"}


async def handle_memory_amend(input: MemoryAmendInput) -> MemoryAmendOutput:
    """Publish one bounded change, then bring the current-state row in line with
    what now applies. The publication is the change; the row is a projection."""
    head = await _amend_guards(input)
    build = _amendment_builder(input, _new_assertion(input, head))
    # One id for the journal and the current-state row, minted here for a new key.
    memory_id = str(getattr(head, "memory_id", "") or uuid.uuid4())
    receipt = await publisher.publish_amendment(
        input.org,
        input.namespace,
        (input.agent, input.user_id, input.session_id, input.memory_key),
        memory_id,
        build,
        operation="memory.amend",
    )
    out = MemoryAmendOutput(memory_key=input.memory_key)
    if receipt is None:
        return out
    out.commit_id, out.commit_seq, out.recorded_at = (
        receipt.commit_id,
        receipt.commit_seq,
        receipt.recorded_at,
    )
    out.change_kind = receipt.change_kind
    out.read_token = publisher.read_token([receipt])
    amendment = receipt.amendment
    assert amendment is not None  # publish plans every change it receipts
    out.retired = list(amendment.retired)
    out.added = [
        reads.Held(
            a=a,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            memory_key=input.memory_key,
            memory_id=memory_id,
            born_seq=receipt.commit_seq,
            born_at=receipt.recorded_at,
        ).wire()
        for a in amendment.added
    ]
    # The commit is the change; the current-state row follows it. A row write that
    # fails (an erasure that began meanwhile) is reported, and the projection
    # worker's reconciler brings the row in line.
    try:
        out.current_value = await _project_current(input, out.read_token)
    except LoomServerError as e:
        out.current_row_error = f"{e.code}: {e.message}"
    return out


async def _amend_guards(input: MemoryAmendInput) -> MemoryRow | None:
    """Refuse a malformed, locked or blacklisted amendment before anything is
    published; return the key's current-state row (None for a new key)."""
    import server.memory.store as memory_repo
    from server.memory.app.blacklist_guard import enforce_on_insert
    from server.memory.domain import lock as _lock

    if input.change_kind == "transition" and input.effective_at is None:
        raise ValidationError("a transition needs effective_at")
    if input.change_kind != "retract" and input.value is None:
        raise ValidationError(f"a {input.change_kind} needs a value")
    lo, hi = tm.utc(input.valid_from), tm.utc(input.valid_to)
    if lo is not None and hi is not None and hi <= lo:
        raise ValidationError("valid_to must be after valid_from")
    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    head = await memory_repo.get_by_key(
        identity, input.memory_key, scope=WHOLE_NAMESPACE, fresh=True, strong=True
    )
    if head is not None:
        _lock.assert_unlocked(
            head, "delete" if input.change_kind == "retract" else "value", op="memory.amend"
        )
    if input.value is not None:
        await enforce_on_insert(
            org=input.org, namespace=input.namespace, value=json.dumps(input.value)
        )
    return head


def _new_assertion(input: MemoryAmendInput, head: MemoryRow | None) -> tm.Assertion | None:
    """The assertion a correct or transition adds; None for a retract."""
    if input.value is None:
        return None
    from server.memory.app.provenance import writer_actor
    from server.memory.domain.sensitivity import resolve_sensitivity
    from server.memory.domain.temporal_capture import embed_input_sha256

    serialized = json.dumps(input.value)
    memory_type = str(getattr(head, "memory_type", "") or "semantic")
    return tm.first(
        serialized,
        memory_type=memory_type,
        temporal_kind=input.temporal_kind or "state",
        validity_basis=input.validity_basis,
        valid_text=input.valid_text,
        valid_precision=input.valid_precision,
        valid_tz=input.valid_tz,
        modality=input.modality or "asserted",
        confidence=input.confidence,
        actor=writer_actor(),
        source_class=source_class_for(default="asserted"),
        # An amendment changes the claim; the key's serving metadata stays as the
        # current row holds it (and as the current-state rewrite keeps it). A value
        # that reads as high is high, as memory.set would store it.
        sensitivity=resolve_sensitivity(getattr(head, "sensitivity", "") or None, serialized),
        expires_at=tm.utc(head.expires_at) if head is not None else None,
        pinned=bool(head.pinned) if head is not None else False,
        lock_mode=str(getattr(head, "lock_mode", "") or ""),
        tags=tuple(head.tags) if head is not None else (),
        entities=tuple(head.entities) if head is not None else (),
        embed_input_sha256=embed_input_sha256(memory_type, serialized, input.memory_key),
        qualifiers=dict(input.qualifiers),
        evidence_ids=tuple(input.evidence_ids),
    )


def _amendment_builder(input: MemoryAmendInput, new: tm.Assertion | None) -> publisher.Build:
    """`build(served) -> Amendment` for this request, run against what the key
    serves when the publisher plans the commit."""
    from server.memory.domain.temporal_capture import inherit_shape

    lo, hi = tm.utc(input.valid_from), tm.utc(input.valid_to)
    # A declared shape wins; otherwise the key keeps the one it has.
    shape = WriteIntent(
        temporal_kind=input.temporal_kind or "",
        modality=input.modality or "",
        qualifiers=dict(input.qualifiers),
    )

    def build(served: list[tm.Assertion]) -> tm.Amendment:
        if input.expected_revision and input.expected_revision not in {
            a.assertion_id for a in served
        }:
            raise PreconditionFailedError(
                f"{input.memory_key!r} no longer serves revision {input.expected_revision}; re-read"
            )
        n = new and inherit_shape(new, served, shape)
        if input.change_kind == "transition":
            assert n is not None and input.effective_at is not None
            return tm.transition(served, tm.utc(input.effective_at) or input.effective_at, n)
        if input.change_kind == "correct":
            return tm.correct(served, lo, hi, n)
        # A retract names its claim by qualifiers; another qualifier's claim stays.
        if input.qualifiers:
            served = [a for a in served if a.qualifiers == dict(input.qualifiers)]
        return tm.correct(served, lo, hi, None) if (lo or hi) else tm.retract(served)

    return build


async def _project_current(input: MemoryAmendInput, read_token: str) -> Any:
    """Rewrite the current-state row to what applies now, every column from the
    journal (the reconciler's own write); tombstone it when the key serves nothing.
    The reconciler repairs a row this fails to write."""
    from server.memory.app.temporal_projection import _rewrite_current

    axes = Axes(valid=datetime.now(UTC), known=None)
    # The cut must cover this amendment's own commit (an allocation in doubt below
    # it holds the cut back): otherwise the row is left to the reconciler.
    cut = await reads.cutoff(input.org, input.namespace, axes, read_token=read_token)
    if any(g.startswith("read_token") for g in cut.gaps):
        raise TemporalUnavailableError("the amendment's current row is pending; it follows")
    rows = await reads.served(
        input.org,
        cut,
        scope=WHOLE_NAMESPACE,
        memory_keys=[input.memory_key],
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        include_high=True,
    )
    want = reads.applies_now(rows, cut, input.memory_key)
    ident = (input.agent, input.user_id, input.session_id, input.memory_key)
    await _rewrite_current(input.org, input.namespace, ident, want, cut_seq=cut.seq, plans=rows)
    return reads.decode_value(want[0].a.value) if want else None


async def handle_memory_temporal_status(
    input: MemoryTemporalStatusInput,
) -> MemoryTemporalStatusOutput:
    row, wm = await asyncio.gather(
        store.cutoff(input.org, input.namespace, None),
        store.watermark(input.org, input.namespace),
    )
    kind = await _publisher_kind()
    blockers = []
    if kind == "none":
        blockers.append(
            "no Keeper: set LOOM_MEMORY_TEMPORAL_PUBLISHER=process when exactly one "
            "process writes memory"
        )
    if int(row.get("broken_at") or 0):
        blockers.append(f"integrity break at sequence {row['broken_at']}")
    any_commit = int(row.get("n") or 0) > 0
    return MemoryTemporalStatusOutput(
        namespace=input.namespace,
        publisher=kind,
        head_seq=int(row.get("head") or 0) if any_commit else 0,
        projection_seq=wm,
        first_recorded_at=row.get("first_at") if any_commit else None,
        integrity_break_at=int(row.get("broken_at") or 0),
        write_blockers=blockers,
    )


async def _publisher_kind() -> str:
    if settings.memory_temporal_publisher != "auto":
        return settings.memory_temporal_publisher
    return "keeper" if await publisher._locks.has_keeper() else "none"
