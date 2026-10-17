"""Bi-temporal reads: which assertions applied at valid time V, according to what
the journal had recorded at knowledge time K.

A read first fixes its namespace cutoff C in one journal query (the highest
sequence recorded at or before K, or the head; never past an integrity break),
then takes the assertions served at C — from the projection where its
watermark covers C, reducing the journal tail itself where it does not — and
only then filters by V, content and tags.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.ch import sequential_reads
from server.identity.domain.read_scope import ReadFilters
from server.memory.api.models_common import TemporalAssertion, TemporalCutoff
from server.memory.app.temporal_rows import assertion_from_projection
from server.memory.domain import temporal_model as tm
from server.memory.domain.temporal_reads import Axes, answer, decode_token, resolve_axes
from server.memory.store import temporal as store
from server.settings import settings


@dataclass
class Cut:
    """One namespace's cutoff and what the read could not cover."""

    namespace: str
    axes: Axes
    seq: int = 0
    head: int = 0
    watermark: int = 0
    first_at: datetime | None = None
    policy: str = "compat"
    gaps: list[str] = field(default_factory=list)
    erased: store.Erased = store.NOT_ERASED

    def wire(self) -> TemporalCutoff:
        return TemporalCutoff(
            namespace=self.namespace,
            valid_as_of=self.axes.valid,
            known_as_of=self.axes.known,
            cutoff_seq=self.seq,
            head_seq=self.head,
            complete=not self.gaps,
            history_from=self.first_at,
            validity_policy=self.policy,
            gaps=list(self.gaps),
        )


@dataclass
class Held:
    """An assertion served at the cutoff, with where it lives."""

    a: tm.Assertion
    agent: str
    user_id: str
    session_id: str
    memory_key: str
    memory_id: str = ""
    born_seq: int = 0
    born_at: datetime | None = None
    retired_seq: int | None = None
    retired_at: datetime | None = None

    def wire(self) -> TemporalAssertion:
        a = self.a
        return TemporalAssertion(
            assertion_id=a.assertion_id,
            origin_assertion_id=a.origin_assertion_id,
            parent_assertion_id=a.parent_assertion_id,
            memory_key=self.memory_key,
            agent=self.agent,
            user_id=self.user_id,
            session_id=self.session_id,
            value=decode_value(a.value),
            value_sha256=a.value_sha256,
            memory_type=a.memory_type,
            temporal_kind=a.temporal_kind,
            validity_basis=a.validity_basis,
            valid_from=a.valid_from,
            valid_to=a.valid_to,
            valid_text=a.valid_text,
            valid_precision=a.valid_precision,
            valid_tz=a.valid_tz,
            modality=a.modality,
            status=a.status,
            confidence=a.confidence,
            verified_by=a.verified_by,
            verified_at=a.verified_at,
            source_class=a.source_class,
            actor=a.actor,
            qualifiers=dict(a.qualifiers),
            tags=list(a.tags),
            evidence_ids=list(a.evidence_ids),
            derived_from=list(a.derived_from),
            born_seq=self.born_seq,
            born_at=self.born_at,
            retired_seq=self.retired_seq,
            retired_at=self.retired_at,
        )


def columns_of(a: tm.Assertion) -> dict[str, Any]:
    """The loom.memory columns an assertion fixes."""
    decoded = decode_value(a.value)
    return {
        "value": a.value,
        "value_json": a.value if isinstance(decoded, dict) else "{}",
        "content_sha256": a.content_sha256,
        "memory_type": a.memory_type,
        "confidence": a.confidence,
        "valid_at": tm.utc(tm.row_start(a)),
        "valid_to": tm.utc(a.valid_to),
        "expires_at": tm.utc(a.expires_at),
        "status": a.status,
        "pinned": int(a.pinned),
        "lock_mode": a.lock_mode,
        "lock_until": tm.utc(a.lock_until),
        "lock_reason": a.lock_reason,
        "locked_by": a.locked_by,
        "locked_at": tm.utc(a.locked_at),
        "verified_by": a.verified_by,
        "verified_at": tm.utc(a.verified_at),
        "verified_sha256": a.verified_sha256,
        "grounding": a.grounding,
        "sensitivity": a.sensitivity,
        "source_class": a.source_class,
        "actor": a.actor,
        "tags": list(a.tags),
        "entities": list(a.entities),
        "tombstoned_at": None,
        "redacted_at": None,
        # The validity is the assertion's alone (an extracted anchor became it).
        # No start shown (none known, or only an inferred one): no anchor either.
        **({"temporal_anchor": None} if tm.row_start(a) is None else {}),
    }


def decode_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


async def erased(org: str, namespace: str) -> store.Erased:
    """The erasures a read of this namespace honours: what was recorded before them
    is never served, whether or not every table's cleanup has finished."""
    from server.erasure.app import fence

    ns_us, users = await fence.scope_erasure_times(org, namespace)
    return store.Erased(namespace_us=ns_us, users=tuple(users))


def axes_of(args: Any) -> Axes:
    return resolve_axes(args.valid_as_of, args.known_as_of)


async def cutoff(
    org: str, namespace: str, axes: Axes, *, read_token: str = "", policy: str = "compat"
) -> Cut:
    """Fix the namespace cutoff: never above the stable prefix, so an allocation
    still in doubt can't change an answer once it lands. A `read_token` for this
    namespace waits (within `memory_bitemporal_read_wait_ms`) until the cut covers it;
    one the cut can't cover is a gap."""
    cut = Cut(namespace=namespace, axes=axes, policy=policy)
    want = decode_token(read_token).get(namespace, 0) if read_token else 0
    deadline = time.monotonic() + settings.memory_bitemporal_read_wait_ms / 1000
    cut.erased = await erased(org, namespace)
    stable = 0
    erased_head = await store.watermark(org, namespace, "erased")
    while True:
        with sequential_reads():
            row = await store.cutoff(org, namespace, axes.known)
        cut.head = int(row.get("head") or 0)
        cut.seq = 0
        if int(row.get("n") or 0):
            cut.first_at = tm.utc(row.get("first_at"))
            at_k = int(row.get("seq") or 0) if int(row.get("known") or 0) else 0
            stable = store.stable_prefix(row, erased_head=erased_head)
            cut.seq = min(stable, at_k if axes.known is not None else 2**63)
        # A committed token write the cut at K still misses was recorded after K.
        after_k = axes.known is not None and want <= int(row.get("committed") or 0)
        if want <= cut.seq or after_k or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    committed = int(row.get("committed") or 0)
    if int(row.get("n") or 0) and stable < committed and (axes.known is None or cut.seq == stable):
        # Commits above the cut wait on an insert still in doubt below them.
        cut.gaps.append(f"allocation_in_doubt:{stable + 1}")
    broken = int(row.get("broken_at") or 0)
    if broken and cut.seq >= broken:
        cut.seq = broken - 1
        cut.gaps.append(f"integrity_break_at:{broken}")
    if want > cut.seq:
        cut.gaps.append("read_token_after_known_as_of" if after_k else "read_token_not_ready")
    if await store.rows_before_journal(org, namespace, cut.first_at):
        cut.gaps.append("history_before_journal")
    cut.watermark = await store.watermark(org, namespace)
    if cut.seq > cut.watermark and cut.seq - cut.watermark > settings.memory_bitemporal_max_tail:
        cut.gaps.append("projection_not_ready")
    return cut


async def served(
    org: str,
    cut: Cut,
    *,
    scope: ReadFilters,
    memory_keys: Sequence[str] | None = None,
    agent: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    include_high: bool = False,
    memory_type: str | None = None,
    tags: Sequence[str] = (),
    limit: int = 10_000,
) -> list[Held]:
    """Every assertion served at the cutoff, whatever its valid time, that today's
    rules still let the caller see. `memory_type` / `tags` narrow to the keys with
    a matching assertion, before the limit (a truncated set says so in a gap)."""
    if not cut.seq:
        return []
    with sequential_reads():
        out = await _served_at(
            org,
            cut,
            scope=scope,
            memory_keys=memory_keys,
            agent=agent,
            user_id=user_id,
            session_id=session_id,
            include_high=include_high,
            memory_type=memory_type,
            tags=tags,
            limit=limit,
        )
    return await visible(org, cut.namespace, list(out.values()))


async def history(
    org: str,
    cut: Cut,
    memory_key: str,
    *,
    scope: ReadFilters,
    agent: str | None,
    user_id: str | None,
    session_id: str | None,
    limit: int,
) -> list[Held]:
    """Every assertion the key had by the cut, retired or not, that today's rules
    let the caller see, oldest first. A projection behind the cut, or a full page
    (the oldest are left out), is a gap on `cut`."""
    namespace = cut.namespace
    gone = await erased(org, namespace)
    with sequential_reads():
        rows = await store.key_history(
            org,
            namespace,
            memory_key,
            scope=scope,
            agent=agent,
            user_id=user_id,
            session_id=session_id,
            erased=gone,
            limit=limit,
            upto_seq=cut.seq,
        )
    if cut.seq > cut.watermark and "projection_not_ready" not in cut.gaps:
        cut.gaps.append("projection_not_ready")
    if len(rows) >= limit:
        cut.gaps.append(f"truncated:{limit}")
    held = [_held(r) for r in reversed(rows)]
    return await visible(org, namespace, held)


async def visible(org: str, namespace: str, held: list[Held]) -> list[Held]:
    """Drop what a current rule forbids at every knowledge time: a value the
    blacklist now refuses (a `blacklist.add` sweep retires it from the head; its
    history must not serve it either)."""
    from server.memory.app.blacklist_guard import find_match

    out = []
    for h in held:
        if await find_match(org=org, namespace=namespace, value=h.a.value) is None:
            out.append(h)
    return out


async def _served_at(
    org: str,
    cut: Cut,
    *,
    scope: ReadFilters,
    memory_keys: Sequence[str] | None,
    agent: str | None,
    user_id: str | None,
    session_id: str | None,
    include_high: bool,
    memory_type: str | None,
    tags: Sequence[str],
    limit: int,
) -> dict[str, Held]:
    out: dict[str, Held] = {}
    rows = await store.assertions_at(
        org,
        cut.namespace,
        min(cut.seq, cut.watermark),
        scope=scope,
        memory_keys=memory_keys,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        include_high=include_high,
        memory_type=memory_type,
        tags=tags,
        erased=cut.erased,
        limit=limit,
    )
    for r in rows:
        out[str(r["assertion_id"])] = _held(r)
    if cut.seq > cut.watermark and "projection_not_ready" not in cut.gaps:
        await _reduce_tail(
            org,
            cut,
            out,
            scope=scope,
            memory_keys=memory_keys,
            agent=agent,
            user_id=user_id,
            session_id=session_id,
            include_high=include_high,
        )
    if not include_high and cut.head > cut.watermark:
        # Today's fence over commits not yet projected, up to the head and whatever
        # the cut: a key one of them made 'high' is withheld (fail closed).
        fenced = await store.high_after(org, cut.namespace, cut.watermark)
        out = {
            k: h
            for k, h in out.items()
            if (h.agent, h.user_id, h.session_id, h.memory_key) not in fenced
        }
    if len(rows) >= limit:
        cut.gaps.append(f"truncated:{limit}")
    return out


def _held(r: dict[str, Any]) -> Held:
    return Held(
        a=assertion_from_projection(r),
        agent=str(r["agent"]),
        user_id=str(r["user_id"]),
        session_id=str(r["session_id"]),
        memory_key=str(r["memory_key"]),
        memory_id=str(r["memory_id"]),
        born_seq=int(r["born_seq"]),
        born_at=tm.utc(r.get("born_at")),
        retired_seq=int(r["retired_seq"]) if r.get("retired_seq") is not None else None,
        retired_at=tm.utc(r.get("retired_at")),
    )


def _in_filter(
    c: dict[str, Any],
    scope: ReadFilters,
    memory_keys: Sequence[str] | None,
    agent: str | None,
    user_id: str | None,
    session_id: str | None,
) -> bool:
    if memory_keys is not None and str(c["memory_key"]) not in memory_keys:
        return False
    for col, want in (("agent", agent), ("user_id", user_id), ("session_id", session_id)):
        if want is not None and want != "*" and str(c[col]) != want:
            return False
    if scope.narrowed:
        if str(c["user_id"]) != scope.user_id:
            return False
        if scope.scope == "own_session" and str(c["session_id"]) != scope.session_id:
            return False
    return True


async def _reduce_tail(
    org: str,
    cut: Cut,
    out: dict[str, Held],
    *,
    scope: ReadFilters,
    memory_keys: Sequence[str] | None,
    agent: str | None,
    user_id: str | None,
    session_id: str | None,
    include_high: bool,
) -> None:
    """Apply the commits the projection has not applied yet, in sequence order, up
    to the cut."""
    commits = await store.commits_between(
        org, cut.namespace, cut.watermark, cut.seq, memory_keys=memory_keys, erased=cut.erased
    )
    for c in commits:
        if not _in_filter(c, scope, memory_keys, agent, user_id, session_id):
            continue
        for aid in c["retired_assertion_ids"]:
            out.pop(str(aid), None)
        for d in json.loads(c["assertions"]):
            a = tm.Assertion.from_json(d)
            if a.sensitivity == "high" and not include_high:
                continue
            out[a.assertion_id] = Held(
                a=a,
                agent=str(c["agent"]),
                user_id=str(c["user_id"]),
                session_id=str(c["session_id"]),
                memory_key=str(c["memory_key"]),
                memory_id=str(c["memory_id"]),
                born_seq=int(c["commit_seq"]),
                born_at=tm.utc(c["recorded_at"]),
            )


def _ident(c: dict[str, Any]) -> tuple[str, str, str, str]:
    return (str(c["agent"]), str(c["user_id"]), str(c["session_id"]), str(c["memory_key"]))


def statuses(*, include_proposed: bool = False, include_duplicates: bool = False) -> frozenset[str]:
    """The statuses a temporal read serves. A superseded assertion is retired from
    the served set, not a status, so there is no `include_superseded` here."""
    return frozenset(
        {"active", "conflicted"}
        | ({"duplicate"} if include_duplicates else set())
        | ({"proposed"} if include_proposed else set())
    )


def held_at(
    rows: Sequence[Held], cut: Cut, *, include: frozenset[str] = frozenset({"active", "conflicted"})
) -> dict[tuple[str, str, str, str], tuple[list[Held], bool]]:
    """Per identity (agent, user, session, key): the assertions that apply at V,
    and whether they dispute. Two identities' claims never mix."""
    by_ident: dict[tuple[str, str, str, str], list[Held]] = {}
    for h in rows:
        by_ident.setdefault((h.agent, h.user_id, h.session_id, h.memory_key), []).append(h)
    out: dict[tuple[str, str, str, str], tuple[list[Held], bool]] = {}
    for ident, hs in by_ident.items():
        index = {h.a.assertion_id: h for h in hs}
        valid, disputed = answer(
            [h.a for h in hs], cut.axes.valid, policy=_policy(cut.policy), include_statuses=include
        )
        if valid:
            out[ident] = ([index[a.assertion_id] for a in valid], disputed)
    return out


def held_newest(rows: Sequence[Held], cut: Cut) -> tuple[list[Held], bool] | None:
    """Of a key read's identities, the one whose applying assertions were recorded
    last (an exact identity read has only one)."""
    groups = held_at(rows, cut).values()
    return max(groups, key=lambda g: max(h.born_seq for h in g[0]), default=None)


def _policy(p: str) -> tm.ValidityPolicy:
    return "strict" if p == "strict" else "compat"


def applies_now(rows: Sequence[Held], cut: Cut, memory_key: str) -> list[Held]:
    """What the key's current-state row may carry, newest first: the assertions
    that apply at the cut's V, else the latest one that has begun (a closed fact
    stays the key's row). Empty when only plans remain: a plan never becomes the
    current row early."""
    held = held_newest(rows, cut) if rows else None
    if held:
        # The same rule the writer applies (`tm.current_claims`): of events, the latest.
        keep = {id(a) for a in tm.latest_events([h.a for h in held[0]])}
        shown = [h for h in held[0] if id(h.a) in keep]
        return sorted(shown, key=lambda h: h.born_seq, reverse=True)
    v = cut.axes.valid
    # An undated assertion has begun (its start is unknown, not later).
    begun = [h for h in rows if h.a.valid_from is None or h.a.valid_from <= v]
    return [max(begun, key=lambda h: h.a.valid_from or tm.MIN)] if begun else []
