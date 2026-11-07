"""Memory client. Surface covered:
- Core: get / set / pin / delete / list / search / ask / touch / clear
- By id: get_by_id / update_by_id
- Lifecycle: promote / demote / share / redact / outcome
- Bulk: bulk_set / bulk_get
- Inspection: history / lineage / related / search_trace / dedup_list /
  dedup_resolve
- Extraction shortcut: set_from_messages

Two save modes:
- `set(...)`   — autonomous-mode save (intent judgment)
- `pin(...)`   — explicit-mode save (unambiguous user pin request)

Classification: pass `tags=[...]` on `set` / `search`. `memory_type` defaults
to `"semantic"` and is optional — the user-facing axis is tags. Searchable
types (semantic, procedural, preference) are auto-embedded at write time;
episodic memory is session-scoped and not searchable.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

if TYPE_CHECKING:
    from loom.agent import Agent

# Free-form string. The default taxonomy is episodic/semantic/procedural/
# preference, but namespaces with a registered tag_registry accept any name.
MemoryType = str
# The provenance a writer may declare; the server stamps every other class.
SourceClass = Literal["derived", "external", "imported"]
SearchableMemoryType = str
Scope = Literal["private", "shared"]
SENTINEL = "-"

from loom.errors import from_response  # noqa: E402
from loom.records import MemoryRecordClient, MemorySchemaClient  # noqa: E402
from loom.run import credit_answer, current_run_id  # noqa: E402  (after the module constants)

# Set coalescing. From the COALESCE_MIN_BURST-th `set` inside
# one COALESCE_WINDOW_S window per identity, sets are gathered and flushed at
# the window's end as one `memory.bulk_set` (chunks of COALESCE_MAX_BATCH —
# the server's bulk_conflict_check_max_entries default, so every entry keeps
# its verdict). The first two sets of a window go out at once, after the
# identity's last flush: a lone or sequential writer pays no added latency; a
# burst pays one request per ~200 entries instead of one per set.
COALESCE_WINDOW_S = 0.2
COALESCE_MIN_BURST = 3
COALESCE_MAX_BATCH = 200
# `memory.set` payload keys a bulk entry accepts. A set carrying anything else
# (a precondition, an expiry policy, a resource id, a dry run) is never
# coalesced: it goes out as its own request.
_BULK_ENTRY_KEYS = frozenset(
    {
        "memory_key",
        "memory_type",
        "value",
        "confidence",
        "source_trace",
        "ttl_seconds",
        "tags",
        "entities",
        "valid_at",
        "valid_to",
        "char_limit",
        "source_class",
    }
)
_IDENTITY_KEYS = ("org", "namespace", "agent", "user_id", "session_id", "run_id")


class _CoalesceWindow:
    __slots__ = ("t0", "count", "pending", "timer", "identity", "first_sets")

    def __init__(self, t0: float, identity: dict[str, str]) -> None:
        self.t0 = t0
        self.count = 0
        self.pending: list[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]] = []
        self.timer: asyncio.TimerHandle | None = None
        self.identity = identity
        # The window's first sets, sent alone: its flush waits for them.
        self.first_sets: list[asyncio.Task[Any]] = []


_T = TypeVar("_T")


class _SetCoalescer:
    """Gathers burst `memory.set` calls of one identity into `memory.bulk_set`.

    `submit()` returns the set's own result: the bulk row's `memory_key` and
    `version` plus `coalesced: true`, `conflict_verdict` (the row's own
    verdict, as the point path spells it) and `noop` (version 0: a merge, or
    a restatement the server skipped, wrote nothing). A set the server refused
    raises the error the same set alone would have raised, and a failed bulk
    request fails every set of its flush still waiting with the same exception.
    The sets the blacklist could not check in the batch go out again in order,
    in smaller batches."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport
        self._windows: dict[tuple[str, ...], _CoalesceWindow] = {}
        # Every flush and first set not yet done: flush() waits for them all.
        self._inflight: set[asyncio.Task[Any]] = set()
        # The last flush of each identity. The next window's first sets and its flush start
        # after it, so a set sent again cannot land after a newer set of its key.
        self._flushes: dict[tuple[str, ...], asyncio.Task[None]] = {}

    def _key(self, identity: dict[str, str]) -> tuple[str, ...]:
        return tuple(str(identity.get(k, SENTINEL)) for k in _IDENTITY_KEYS)

    async def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = {k: str(payload.get(k, SENTINEL)) for k in _IDENTITY_KEYS}
        entry = {k: v for k, v in payload.items() if k not in _IDENTITY_KEYS}
        loop = asyncio.get_running_loop()
        now = loop.time()
        key = self._key(identity)
        window = self._windows.get(key)
        if window is not None and now - window.t0 >= COALESCE_WINDOW_S:
            self._close(key, window)  # its timer has not run yet: its flush goes first
            window = None
        if window is None:
            window = _CoalesceWindow(now, identity)
            self._windows[key] = window
            # It ends at its time, flushed if it gathered sets, so an idle one is not kept.
            window.timer = loop.call_later(COALESCE_WINDOW_S, self._close, key, window)
        window.count += 1
        if window.count < COALESCE_MIN_BURST:
            first = self._after(
                [self._flushes.get(key)], lambda: self._transport.post("/v1/memory.set", payload)
            )
            window.first_sets.append(first)
            self._inflight.add(first)
            first.add_done_callback(self._inflight.discard)
            # A caller that stops waiting leaves the set going: the window's flush follows it.
            direct: dict[str, Any] = await asyncio.shield(first)
            return direct
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        window.pending.append((entry, fut))
        if len(window.pending) >= COALESCE_MAX_BATCH:
            self._close(key, window)
        return await fut

    def _close(self, key: tuple[str, ...], window: _CoalesceWindow) -> None:
        if self._windows.get(key) is window:
            del self._windows[key]
        if window.timer is not None:
            window.timer.cancel()
            window.timer = None
        if not window.pending:
            return
        batch, window.pending = window.pending, []
        first_sets, window.first_sets = window.first_sets, []
        task = self._after(
            [self._flushes.get(key), *first_sets], lambda: self._flush(window.identity, batch)
        )
        self._flushes[key] = task
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        task.add_done_callback(lambda done: self._flush_done(key, done, batch))

    def _after(
        self, before: list[asyncio.Task[Any] | None], request: Callable[[], Awaitable[_T]]
    ) -> asyncio.Task[_T]:
        """A task that runs `request()` once every task in `before` is done."""

        async def run() -> _T:
            waits = [t for t in before if t is not None]
            if waits:
                await asyncio.wait(waits)
            return await request()

        return asyncio.get_running_loop().create_task(run())

    def _flush_done(
        self,
        key: tuple[str, ...],
        task: asyncio.Task[None],
        batch: list[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]],
    ) -> None:
        if self._flushes.get(key) is task:
            del self._flushes[key]
        if task.cancelled():
            # Nothing here cancels a flush; one cancelled from outside tells its callers.
            for _, fut in batch:
                fut.cancel()

    async def _flush(
        self,
        identity: dict[str, str],
        batch: list[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]],
    ) -> None:
        # Entries the blacklist could not check go out again in order, in batches the size
        # of what the last request checked; one unchecked at the front of a request, alone.
        sizes = {id(fut): _value_bytes(entry) for entry, fut in batch}
        pending = batch
        room: int | None = None  # the value bytes the last request checked
        while len(pending) > 1:
            size = len(pending) if room is None else _entries_within(pending, sizes, room)
            if size == 1:
                await self._set_alone(identity, *pending[0])
                pending = pending[1:]
                continue
            sent, pending = pending[:size], pending[size:]
            try:
                unchecked, room = await self._bulk_round(identity, sent, sizes)
            except Exception as e:  # noqa: BLE001 — every caller still waiting gets the failure
                for _, fut in sent + pending:
                    if not fut.done():
                        fut.set_exception(e)
                return
            if unchecked and unchecked[0][1] is sent[0][1]:
                await self._set_alone(identity, *unchecked[0])
                unchecked = unchecked[1:]
            pending = unchecked + pending
        for entry, fut in pending:
            await self._set_alone(identity, entry, fut)

    async def _bulk_round(
        self,
        identity: dict[str, str],
        batch: list[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]],
        sizes: dict[int, int],
    ) -> tuple[list[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]], int]:
        """Send `batch` as one memory.bulk_set and answer each entry from its row. Returns
        the entries the blacklist could not check, in order, and the value bytes (from
        `sizes`, by future) of the ones it did check."""
        payload: dict[str, Any] = dict(identity)
        payload["entries"] = [entry for entry, _ in batch]
        res = await self._transport.post("/v1/memory.bulk_set", payload)
        # Results are matched by key, in order, not by position: an older server leaves
        # a blacklisted entry out of `written`, which would shift every later result.
        rows_by_key: dict[str, list[dict[str, Any]]] = {}
        for written_row in res.get("written") or []:
            rows_by_key.setdefault(str(written_row.get("memory_key")), []).append(written_row)
        unchecked: list[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]] = []
        checked = 0
        for entry, fut in batch:
            rows = rows_by_key.get(str(entry["memory_key"])) or []
            # popped even for a caller gone: the next entry of this key keeps its own row
            row = dict(rows.pop(0)) if rows else {"memory_key": entry["memory_key"], "version": 0}
            row["coalesced"] = True
            # Only the verdict the server stamped on this row counts. The key
            # lists can name a key for another entry of the same key, so an
            # unstamped row (unchecked, or a restatement skipped) is clear.
            row["conflict_verdict"] = row.get("conflict_verdict") or "clear"
            row["noop"] = int(row.get("version") or 0) == 0
            if row["conflict_verdict"] == "blacklist_unchecked":
                unchecked.append((entry, fut))
                continue
            # The blacklist read this entry's value: it was written, matched a pattern, or
            # reached the conflict check. A skipped restatement or a pinned or locked key
            # never got that far.
            if row["version"] or row["conflict_verdict"] not in _NOT_CHECKED_VERDICTS:
                checked += sizes[id(fut)]
            if fut.done():
                continue
            # A refused entry (a pinned or read_only key, a blacklisted value, a write
            # close to a pinned row) raises what the same set alone would have. The row
            # says so, not the key's list, since a restatement of the same key in the
            # batch is not refused.
            refusal = _COALESCED_REFUSALS.get(row["conflict_verdict"])
            if refusal is not None:
                status, detail = _refusal_detail(row, *refusal)
                fut.set_exception(from_response({"detail": detail}, status))
                continue
            fut.set_result(row)
        return unchecked, checked

    async def _set_alone(
        self, identity: dict[str, str], entry: dict[str, Any], fut: asyncio.Future[dict[str, Any]]
    ) -> None:
        """Send one entry as a memory.set and hand its caller the answer."""
        try:
            res = await self._transport.post("/v1/memory.set", {**identity, **entry})
        except Exception as e:  # noqa: BLE001 — hand the caller its own failure
            if not fut.done():
                fut.set_exception(e)
            return
        if not fut.done():
            fut.set_result(res)

    async def flush(self) -> None:
        """Send every gathered set now and wait for the writes to land: those there now,
        then those made while they went out (a writer that never stops is not waited
        for)."""
        for _ in range(2):
            for key, window in list(self._windows.items()):
                self._close(key, window)
            if self._inflight:
                # wait, not gather: a flush_sets() call cancelled leaves the writes going
                await asyncio.wait(list(self._inflight))


def _value_bytes(entry: dict[str, Any]) -> int:
    """The bytes of an entry's value as the server checks it: its JSON, which is ASCII."""
    return len(json.dumps(entry.get("value")))


# Verdicts of a row the server answered before the blacklist read its value: unchanged
# (a restatement, an identity duplicate), a pinned or locked key, an unchecked one.
_NOT_CHECKED_VERDICTS = frozenset({"clear", "pinned", "locked", "blacklist_unchecked"})


def _entries_within(
    pending: list[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]],
    sizes: dict[int, int],
    room: int,
) -> int:
    """How many entries from the front of `pending` fit in `room` value bytes, at least one."""
    total = 0
    for n, (_, fut) in enumerate(pending):
        total += sizes[id(fut)]
        if total > room:
            return max(n, 1)
    return len(pending)


# A coalesced entry's refusal by its row's verdict: the status, code and message the
# same memory.set alone answers.
_COALESCED_REFUSALS: dict[str, tuple[int, str, str]] = {
    "pinned": (409, "PINNED_CONFLICT", "the key is pinned"),
    "rejected": (409, "PINNED_CONFLICT", "write conflicts with a pinned memory"),
    "locked": (423, "LOCKED", "the key is locked read_only"),
    "blacklisted": (422, "BLACKLISTED", "value matches a blacklist pattern"),
}


def _refusal_detail(
    row: dict[str, Any], status: int, code: str, message: str
) -> tuple[int, dict[str, Any]]:
    """The error detail for a refused coalesced entry. A pinned refusal names the
    blocker and the peers, as memory.set does; the row carries no lock or blacklist
    pattern, so those name the key only."""
    detail: dict[str, Any] = {"code": code, "message": message, "memory_key": row["memory_key"]}
    if code == "PINNED_CONFLICT":
        blocker = str(row.get("pinned_blocker_id") or "")
        peer_ids = [str(p.get("memory_id") or "") for p in row.get("peers") or []]
        detail["pinned_blocker_id"] = blocker
        detail["peer_ids"] = peer_ids or ([blocker] if blocker else [])
    return status, detail


def _opt_int(v: Any) -> int | None:
    """A counter the server sent (`include_usage`), or None when it did not."""
    return None if v is None else int(v)


def _opt_str(v: Any) -> str | None:
    return None if v is None else str(v)


@dataclass(frozen=True)
class MemorySearchHit:
    memory_key: str
    memory_type: str
    content_excerpt: str
    distance: float = 0.0
    # The un-boosted score: the cosine of the row to the query, in [0, 1] —
    # the number to threshold on.
    similarity: float = 0.0
    namespace: str = ""
    # The hit's worst link to code ('' when it names no indexed code): `fresh`,
    # `moved`, `changed` (check the code before relying on it), `unresolved` (gone
    # where it was, maybe renamed) or `missing`.
    code_state: str = ""
    # Final blended rank score: the value the served order follows,
    # mode-dependent (about 0.03 under rrf), not a similarity. The terms that
    # moved the row are itemised in `score_components`. After a reranker that
    # changed the order it is the score of the rank the row holds; its own is
    # `score_components["rank_score_pre_rerank"]`.
    rank_score: float = 0.0
    # True when this row's `similarity` is below the abstain threshold the
    # response's gate used; the server sends the key only
    # when set. Drop these rows for an "abstain means abstain" reading.
    below_threshold: bool = False
    # Date metadata from the row; empty when the row had no date dimension.
    temporal_anchor: str = ""
    # How precisely the source pinned temporal_anchor (0..1; 0 = unscored).
    temporal_confidence: float = 0.0
    valid_at: str = ""
    # Retrieval layer that surfaced this hit: "fact" (distilled) or "chunk" (verbatim).
    source: str = "fact"
    # Chunk hits: the key of the fact the chunk was written under; "" otherwise.
    parent_memory_key: str = ""
    # Graph mode: chain from a query entity to one of the hit's entities,
    # seed first; empty in other modes.
    via_path: tuple[str, ...] = ()
    # The row's tags, served when `search(include_tags=True)`:
    # a caller whose classification lives in tags reads it here instead of
    # one `get` per hit. Empty when not requested and for chunk / digest hits.
    tags: tuple[str, ...] = ()
    # Why the hit was served: the ranking terms that placed it
    # (`rank_score_raw`, `key_match_boost_effective`, `date_proximity_boost`,
    # `entity_ann_boost`, `foreign`, …) and the server's compact
    # provenance note. Every wire field the SDK once dropped rides here too.
    score_components: dict[str, float] = field(default_factory=dict)
    explanation: str = ""
    # Row state (active | superseded | duplicate | conflicted); "" for chunks.
    status: str = ""
    # A superseded hit (`include_superseded`): the successor's key and the
    # retirement instant; None otherwise.
    superseded_by: str | None = None
    superseded_at: str | None = None
    # When this hit is a canonical seated in the slot of a
    # hidden `duplicate` row, that row's memory_key; None otherwise.
    resolved_from: str | None = None
    version: int = 0
    # Ingest time (ISO); "" when unknown.
    created_at: str = ""
    # Provenance class (asserted | extracted | observed | derived | imported |
    # external), the source record, whether an attestation is live.
    source_class: str = ""
    source_resource_id: str = ""
    verified: bool = False
    # The identity axes the row was written under, when the server read them.
    user_id: str = ""
    session_id: str = ""
    speaker: str = ""
    about_other: bool = False
    entities: tuple[str, ...] = ()
    # Digest hits: the keys the digest compressed.
    derived_from: tuple[str, ...] = ()
    # bodies="unit": the whole value did not fit; `next` fetches it.
    body_truncated: bool = False
    next: str = ""
    # Reliance counters, served when `search(include_usage=True)`: None when
    # not requested. `reinforcement_count` is the folded rank
    # signal; `used_count` an agent's label (memory.outcome used /
    # run.complete memories_used); `used_cited_count` memory.ask's own credit.
    reinforcement_count: int | None = None
    used_count: int | None = None
    used_cited_count: int | None = None
    harm_count: int | None = None
    evidence_count: int | None = None
    acted_count: int | None = None
    pinned: bool | None = None


class MemorySearchResult(list[MemorySearchHit]):
    """The ranked hits (it IS a ``list[MemorySearchHit]``) plus the server's
    response envelope:

      • ``abstain`` — the best hit was too weak to answer from: say "I don't
        have that information" rather than answer from low-similarity rows.
        The rows are still served; each one below the abstain threshold
        carries ``below_threshold=True``.
      • ``abstain_code`` — "" | "no_match" | "below_threshold" |
        "entity_absent" | "low_calibrated_confidence" | "page_expired".
        Branch on it; ``abstain_reason`` is free text with the values in it.
      • ``retrieval_risk`` — "low" | "medium" | "high" | "abstain".
      • ``reinforcement`` — "credited", "not_credited (measurement_label |
        no_consumer | no_closer)", or "" when the read asked for no credit
        (``auto_touch=False``). A reader is credited when it names a
        ``consumer`` and reads inside a run or has posted ``run.complete`` /
        ``memory.outcome`` in the last 14 days.
      • ``pending_extraction`` — deferred ``set_from_messages`` batches not yet
        extracted in the searched scope; above 0, an empty or thin result is
        not evidence of absence.
      • ``degraded`` — providers that fell back on this read and what the
        response did instead (``rerank: input_order_kept (ReadTimeout)``).
      • ``next`` — inside a run, how to report the hits the agent relied on.
    """

    def __init__(
        self,
        hits: list[MemorySearchHit],
        *,
        abstain: bool = False,
        abstain_reason: str = "",
        abstain_code: str = "",
        retrieval_risk: str = "",
        reinforcement: str = "",
        pending_extraction: int = 0,
        degraded: tuple[str, ...] = (),
        next: str = "",
    ) -> None:
        super().__init__(hits)
        self.abstain = abstain
        self.abstain_reason = abstain_reason
        self.abstain_code = abstain_code
        self.reinforcement = reinforcement
        self.retrieval_risk = retrieval_risk
        self.pending_extraction = pending_extraction
        self.degraded = degraded
        self.next = next


# memory.search knobs where 0, False or "" is a value the server must see.
# Every other optional knob rides only when truthy.
_SEARCH_FALSY_IS_A_VALUE = frozenset(
    {
        "memory_type",
        "user_id",
        "session_id",
        "run_id",
        "hyde",
        "retrieval_budget",
        "confidence_weight",
        "reinforcement_weight",
        "reinforcement_half_life_days",
        "body_budget_tokens",
        "explain",
    }
)


def _search_payload(
    *,
    org: str,
    namespace: str,
    query: str,
    scope: str,
    top_k: int,
    search_mode: str,
    alpha: float,
    auto_touch: bool,
    agent_id: str | None = None,
    tag_boost: float = 0.3,
    category_boost: float = 0.3,
    **knobs: Any,
) -> dict[str, Any]:
    """The `/v1/memory.search` body for `MemoryClient.search`: the wire
    defaults always, each optional knob only when set (None is unset), so the
    server's own default governs any unset field. Knobs ride in the order given."""
    payload: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        # Membership is the read boundary: omitting agent_id reads across
        # ALL agents in the accessible namespaces (the "*" sentinel). Pass
        # an explicit agent_id only to narrow to that one author's rows.
        # (Writes still bind the constructor agent — authorship.)
        "agent": agent_id or "*",
        "query": query,
        "scope": scope,
        "top_k": top_k,
        "search_mode": search_mode,
        "alpha": alpha,
        "auto_touch": auto_touch,
    }
    # Soft metadata: tags / category become a rank boost instead of a hard
    # filter; the boost weight rides only with its switch.
    boosts = {
        "tags_soft": ("tag_boost", tag_boost),
        "category_soft": ("category_boost", category_boost),
    }
    for name, v in knobs.items():
        if name == "run_id" and v is None:
            v = current_run_id()
        if v is None or (not v and name not in _SEARCH_FALSY_IS_A_VALUE):
            continue
        if name in boosts:
            boost_name, weight = boosts[name]
            payload[name] = True
            payload[boost_name] = weight
        elif isinstance(v, datetime):
            payload[name] = v.isoformat()
        elif isinstance(v, (list, tuple)):
            payload[name] = list(v)
        elif name == "body_budget_tokens":
            payload[name] = int(v)
        else:
            payload[name] = v
    return payload


def _parse_search_hit(r: dict[str, Any]) -> MemorySearchHit:
    """One wire hit → `MemorySearchHit`, every field the server may send;
    the `include_usage` counters stay None when absent."""
    return MemorySearchHit(
        memory_key=r["memory_key"],
        memory_type=r["memory_type"],
        content_excerpt=r["content_excerpt"],
        distance=float(r["distance"]),
        similarity=float(r["similarity"]),
        namespace=str(r.get("namespace") or ""),
        code_state=str(r.get("code_state") or ""),
        rank_score=float(r.get("rank_score") or 0.0),
        below_threshold=bool(r.get("below_threshold") or False),
        temporal_anchor=str(r.get("temporal_anchor") or ""),
        temporal_confidence=float(r.get("temporal_confidence") or 0.0),
        valid_at=str(r.get("valid_at") or ""),
        source=str(r.get("source") or "fact"),
        parent_memory_key=str(r.get("parent_memory_key") or ""),
        via_path=tuple(str(x) for x in (r.get("via_path") or [])),
        tags=tuple(str(x) for x in (r.get("tags") or [])),
        score_components={
            str(k): float(v)
            for k, v in (r.get("score_components") or {}).items()
            if isinstance(v, (int, float))
        },
        explanation=str(r.get("explanation") or ""),
        status=str(r.get("status") or ""),
        superseded_by=_opt_str(r.get("superseded_by")),
        superseded_at=_opt_str(r.get("superseded_at")),
        resolved_from=_opt_str(r.get("resolved_from")),
        version=int(r.get("version") or 0),
        created_at=str(r.get("created_at") or ""),
        source_class=str(r.get("source_class") or ""),
        source_resource_id=str(r.get("source_resource_id") or ""),
        verified=bool(r.get("verified") or False),
        user_id=str(r.get("user_id") or ""),
        session_id=str(r.get("session_id") or ""),
        speaker=str(r.get("speaker") or ""),
        about_other=bool(r.get("about_other") or False),
        entities=tuple(str(x) for x in (r.get("entities") or [])),
        derived_from=tuple(str(x) for x in (r.get("derived_from") or [])),
        body_truncated=bool(r.get("body_truncated") or False),
        next=str(r.get("next") or ""),
        reinforcement_count=_opt_int(r.get("reinforcement_count")),
        used_count=_opt_int(r.get("used_count")),
        used_cited_count=_opt_int(r.get("used_cited_count")),
        harm_count=_opt_int(r.get("harm_count")),
        evidence_count=_opt_int(r.get("evidence_count")),
        acted_count=_opt_int(r.get("acted_count")),
        pinned=(None if r.get("pinned") is None else bool(r.get("pinned"))),
    )


def _search_result(result: dict[str, Any]) -> MemorySearchResult:
    return MemorySearchResult(
        [_parse_search_hit(r) for r in result.get("results", [])],
        abstain=bool(result.get("abstain", False)),
        abstain_reason=str(result.get("abstain_reason") or ""),
        abstain_code=str(result.get("abstain_code") or ""),
        retrieval_risk=str(result.get("retrieval_risk") or ""),
        reinforcement=str(result.get("reinforcement") or ""),
        pending_extraction=int(result.get("pending_extraction") or 0),
        degraded=tuple(str(x) for x in (result.get("degraded") or ())),
        next=str(result.get("next") or ""),
    )


class MemoryClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._coalescer: _SetCoalescer | None = None
        # Typed records: `agent.memory.record.write / query /
        # history / conflicts` and the schemas they validate against,
        # `agent.memory.schema.register / list` — see loom/records.py.
        self.record = MemoryRecordClient(agent)
        self.schema = MemorySchemaClient(agent)

    def _set_coalescer(self) -> _SetCoalescer | None:
        if not bool(getattr(self._agent, "coalesce_sets", False)):
            return None
        if self._coalescer is None or self._coalescer._transport is not self._agent.transport:
            self._coalescer = _SetCoalescer(self._agent.transport)
        return self._coalescer

    async def flush_sets(self) -> None:
        """Send any `set` calls still gathered for a coalesced bulk write.
        `Agent.close()` calls this; a caller that wants its burst on the
        server before reading it back can call it directly."""
        if self._coalescer is not None:
            await self._coalescer.flush()

    async def _wait_for_sets(self, timeout_s: float) -> None:
        """`flush_sets()`, for at most `timeout_s`: the writes it has not seen land by then
        keep going."""
        if self._coalescer is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._coalescer.flush(), timeout_s)

    def _identity(
        self,
        user_id: str | None,
        session_id: str | None,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """`agent_id` overrides the constructor-bound agent for this call
        only — lets one Agent instance drive many agent identities,
        useful for orchestrators that multiplex over a single client.
        `run_id` scopes this write/read to one task execution; unset, the
        active `run_scope`'s."""
        return {
            **self._agent.identity(
                user_id=user_id,
                session_id=session_id,
                run_id=run_id or current_run_id(),
                agent_id=agent_id,
            )
        }

    def _set_payload(
        self,
        memory_key: str,
        value: Any,
        memory_type: MemoryType,
        *,
        user_id: str | None,
        session_id: str | None,
        run_id: str | None,
        agent_id: str | None,
        confidence: float,
        source_trace: str,
        **options: Any,
    ) -> dict[str, Any]:
        """The `memory.set` / `memory.pin` body. An option rides only when set:
        None, False and an empty string or list are unset; 0 is a value."""
        payload = self._identity(user_id, session_id, run_id=run_id, agent_id=agent_id)
        payload.update(
            {
                "memory_key": memory_key,
                "memory_type": memory_type,
                "value": value,
                "confidence": confidence,
                "source_trace": source_trace,
            }
        )
        for name, v in options.items():
            if v is None or v is False or (isinstance(v, (str, list, tuple)) and not v):
                continue
            if isinstance(v, datetime):
                v = v.isoformat()
            elif isinstance(v, (list, tuple)):
                v = list(v)
            payload[name] = v
        return payload

    async def get(
        self,
        memory_key: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        as_of: datetime | None = None,
        if_version: int | None = None,
        fields: builtins.list[str] | None = None,
        follow: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch a memory entry. Returns None if not found.

        `follow=True` serves the live canonical a
        `status='duplicate'` row points to; the response then carries
        `resolved_from` (the key asked for) and `canonical_key` (the key
        served). Default False: the exact row, with `canonical_key` naming
        the canonical when the row is a duplicate.

        `as_of` replays valid time (not transaction time): the row that was
        true at that real-world moment, filtered by
        `coalesce(valid_at, created_at) <= as_of`.

        `fields` whitelists the output fields — e.g.
        `["value", "tags", "status"]` — so the response carries only
        those plus `found`. None = every field.
        """
        payload: dict[str, Any] = self._identity(
            user_id, session_id, run_id=run_id, agent_id=agent_id
        )
        payload["memory_key"] = memory_key
        if as_of is not None:
            payload["as_of"] = as_of.isoformat()
        if fields:
            payload["fields"] = list(fields)
        if if_version is not None:
            # A poll that holds the version gets `unchanged:
            # true` and the version back, no body. Callers re-reading a row
            # they already have pass the version they hold.
            payload["if_version"] = int(if_version)
        if follow:
            payload["follow"] = True
        result = await self._agent.transport.post("/v1/memory.get", payload)
        if not result.get("found"):
            return None
        return result

    async def set(
        self,
        memory_key: str,
        value: Any,
        memory_type: MemoryType = "semantic",
        *,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        confidence: float = 1.0,
        ttl_seconds: int | None = None,
        on_expire: Literal["", "tombstone", "demote", "anonymize"] = "",
        expire_reason: str = "",
        source_trace: str = "",
        source_resource_id: str = "",
        source_class: SourceClass | None = None,
        if_match_sha256: str | None = None,
        valid_at: datetime | None = None,
        valid_to: datetime | None = None,
        char_limit: int | None = None,
        relations: list[tuple[str, str, str]] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Write a memory entry.

        `tags` is the recommended classification axis — free-form list of
        strings ("ops", "critical", "team-foo"). `memory_type` defaults to
        `"semantic"` and is rarely worth setting explicitly.

        `entities` seeds named things (identifiers, error codes, symbols)
        on the row directly, so `entity_filter` search and `memory.timeline`
        find them immediately — without waiting on server-side LLM extraction
        (which skips point writes under a keyless/fake provider). Stored
        lowercased.

        `valid_at`: real-world time when this fact became true. Distinct
        from `created_at` (when Loom records the row). Read paths accept
        an `as_of` kwarg that filters by
        `coalesce(valid_at, created_at) <= as_of` for replay queries.
        Most callers leave this NULL; `set_from_messages` auto-fills it on
        past-tense state changes ("user moved to NYC in 2023").

        `valid_to`: the date the fact stopped being true ("until 2024").
        NULL = still valid. Default reads drop a row past its `valid_to` the
        way they drop one past its TTL; `as_of` reads still see it.

        `char_limit`: server-enforced cap on the stringified value length.
        Server returns 422 if exceeded.

        `relations`: the (subject, relation, object) facts the value states,
        written to the fact graph and linked to the row. A set carrying them
        is sent on its own, never coalesced into a bulk write.

        `source_resource_id`: provenance pointer to the primary source this
        fact was cited from — a file path, document id, or URL. Bloom-indexed
        (filterable) and surfaced by `memory.provenance`.

        `source_class`: declare content that came from outside ("external",
        "imported") or was derived ("derived"). The server stamps every other
        class from the token; a request may only downgrade.

        Returns the full server response — `version`, `pinned`, `memory_id`,
        `content_sha256`. The content_sha256 is the SHA-256 of the value
        just written; pass it back as `if_match_sha256` on the next write
        to that key for guaranteed-no-clobber updates. On precondition
        failure the server raises an `IdempotentConflict` you can catch
        and decide whether to re-read + retry. From the third set of one
        identity inside 200 ms the SDK coalesces sets into one
        `memory.bulk_set`, and such a result carries `coalesced: true` and no
        `memory_id` / `content_sha256`: read the hash with `get()` (a set
        with `if_match_sha256` is never coalesced). A blacklisted value raises
        the `ValidationError` (`BLACKLISTED`, 422); a new value for a pinned
        key, or a close write against a pinned row, the `PreconditionFailed`
        (`PINNED_CONFLICT`, 409); a write to a `read_only` key a `LoomError`
        with code `LOCKED` (423); coalesced or not.

        The response also carries the write-time conflict verdict
        (`conflict_verdict`: clear | merge | duplicate | conflict | refines),
        the `peers` it was reached against (`memory_id`, `memory_key`,
        `excerpt`, `valid_at`, `similarity`), a `next` hint naming the op to
        reach for (`memory.update(key)` on a conflict, `memory.pin` on a
        repeated merge) and `noop` (true when nothing was written).

        `dry_run=True` runs the check and returns the verdict, peers and
        `next` without writing anything — no row, no embedding, no audit
        hash; `version` is 0 and `noop` is true.
        """
        payload = self._set_payload(
            memory_key,
            value,
            memory_type,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
            confidence=confidence,
            source_trace=source_trace,
            tags=tags,
            entities=entities,
            ttl_seconds=ttl_seconds,
            on_expire=on_expire,
            expire_reason=expire_reason,
            source_resource_id=source_resource_id,
            source_class=source_class,
            if_match_sha256=if_match_sha256,
            valid_at=valid_at,
            valid_to=valid_to,
            char_limit=char_limit,
            relations=[list(f) for f in relations or []],
            dry_run=dry_run,
        )
        coalescer = self._set_coalescer()
        if coalescer is not None and set(payload) - set(_IDENTITY_KEYS) <= _BULK_ENTRY_KEYS:
            # From the third set inside one 200 ms window this
            # identity's sets travel as one memory.bulk_set (the same four
            # verdicts). The result then carries the bulk
            # row's `memory_key` / `version` plus `coalesced`,
            # `conflict_verdict` and `noop`; `memory_id` / `content_sha256`
            # are point-path fields a coalesced write does not return.
            return await coalescer.submit(payload)
        return await self._agent.transport.post("/v1/memory.set", payload)

    async def ask(
        self,
        query: str,
        *,
        top_k: int = 15,
        memory_type: str | None = None,
        allow_synthesis: bool = True,
        max_answer_chars: int = 0,
        bodies: str | None = None,
        body_budget_tokens: int | None = None,
        search_mode: str | None = None,
        namespace_scope: str | None = None,
        read_scope: str = "namespace",
        agent_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        as_of: datetime | None = None,
        as_of_basis: str | None = None,
        tags: builtins.list[str] | None = None,
        tags_soft: bool = False,
        tag_boost: float | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Ask memory a question: retrieval plus a grounded synthesized answer.

        Maps to `memory.ask`. Phrase `query` as a question — the answer is
        synthesized by an LLM grounded only in the recalled memories.
        Reads across every author in the namespace by default
        (`agent_id="*"`, the server's own default: membership, not
        authorship, is the read boundary); pass `agent_id` to narrow.

        `tags` restricts the memories the answer is
        synthesized from to rows carrying any of them — the same predicate
        `search(tags=...)` applies; `tags_soft=True` turns it into a rank
        boost of `tag_boost` instead of a filter.

        Returns the server response: `answer`, `source` (synthesized /
        retrieval_only / abstained / record / precedent), `abstain` and
        `abstain_reason`, `memory_keys` the answer cited, `results` (the
        hits it read) and `next`. Inside `agent.run_scope(...)` the cited
        keys of a non-abstained answer become the run's `memories_used` —
        the ledger signal `run.complete` credits.
        """
        payload: dict[str, Any] = self._identity(
            user_id, session_id, run_id=run_id, agent_id=agent_id or "*"
        )
        payload["query"] = query
        payload["top_k"] = int(top_k)
        payload["allow_synthesis"] = bool(allow_synthesis)
        if memory_type is not None:
            payload["memory_type"] = memory_type
        if max_answer_chars:
            payload["max_answer_chars"] = int(max_answer_chars)
        if bodies is not None:
            payload["bodies"] = bodies
        if body_budget_tokens is not None:
            payload["body_budget_tokens"] = int(body_budget_tokens)
        if search_mode is not None:
            payload["search_mode"] = search_mode
        if namespace_scope is not None:
            payload["namespace_scope"] = namespace_scope
        if read_scope != "namespace":
            payload["read_scope"] = read_scope
        if as_of is not None:
            payload["as_of"] = as_of.isoformat()
        if as_of_basis is not None:
            payload["as_of_basis"] = as_of_basis
        if tags:
            payload["tags"] = list(tags)
            if tags_soft:
                payload["tags_soft"] = True
            if tag_boost is not None:
                payload["tag_boost"] = float(tag_boost)
        result = await self._agent.transport.post("/v1/memory.ask", payload, timeout_s=timeout_s)
        credit_answer(result)
        return result

    async def pin(
        self,
        memory_key: str,
        value: Any,
        memory_type: MemoryType = "semantic",
        *,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        confidence: float = 1.0,
        ttl_seconds: int | None = None,
        on_expire: Literal["", "tombstone", "demote", "anonymize"] = "",
        expire_reason: str = "",
        source_trace: str = "",
        source_resource_id: str = "",
        source_class: SourceClass | None = None,
        if_match_sha256: str | None = None,
        valid_at: datetime | None = None,
        valid_to: datetime | None = None,
        char_limit: int | None = None,
        relations: list[tuple[str, str, str]] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reinforce: `set` with `pinned=True`, taking the same arguments. A
        later write close to a pinned row is refused (409 PINNED_CONFLICT)
        instead of conflicting.

        Returns the server response, as `set` does: a merged or no-op pin
        says so in `conflict_verdict` / `noop` (its `version` is 0).
        """
        payload = self._set_payload(
            memory_key,
            value,
            memory_type,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
            confidence=confidence,
            source_trace=source_trace,
            tags=tags,
            entities=entities,
            ttl_seconds=ttl_seconds,
            on_expire=on_expire,
            expire_reason=expire_reason,
            source_resource_id=source_resource_id,
            source_class=source_class,
            if_match_sha256=if_match_sha256,
            valid_at=valid_at,
            valid_to=valid_to,
            char_limit=char_limit,
            relations=[list(f) for f in relations or []],
            dry_run=dry_run,
        )
        return await self._agent.transport.post("/v1/memory.pin", payload)

    async def verify(
        self,
        memory_key: str | None = None,
        *,
        memory_id: str | None = None,
        revoke: bool = False,
        actor: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Attest to one memory as the calling identity (or revoke).

        Reinforce with provenance: `pin` protects a fact from conflict
        eviction; `verify` records WHO checked it and against WHICH content.
        Under auth the actor is the token subject as `human:<subject>` or
        `process:<subject>` (service account) — `actor` is honoured only
        by a dev-mode server with no auth context.

        Address by `memory_key` (with this Agent's identity) or `memory_id`.
        Returns the server's `MemoryVerifyOutput` dict: `found`, `changed`,
        `verified_by`, `verified_at`, `verification`
        (human | process | stale | none), `content_sha256`.
        """
        if not memory_key and not memory_id:
            raise ValueError("verify() needs memory_key or memory_id")
        payload: dict[str, Any] = {"org": self._agent.org, "revoke": revoke}
        if memory_id:
            payload["memory_id"] = memory_id
        else:
            payload.update(self._identity(user_id, session_id))
            payload["memory_key"] = memory_key
        if actor:
            payload["actor"] = actor
        return await self._agent.transport.post("/v1/memory.verify", payload)

    async def touch(
        self,
        memory_key: str = "",
        *,
        ttl_seconds: int | None = None,
        clear_expiry: bool = False,
        memory_keys: list[str] | None = None,
        on_expire: Literal["", "tombstone", "demote", "anonymize"] = "",
        expire_reason: str = "",
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Change only a memory's expiry, without rewriting its value,
        entities, conflict state or reinforcement: a new TTL
        (`ttl_seconds`) or none at all (`clear_expiry=True`). Pass one
        `memory_key`, or up to 1,000 `memory_keys` of the same identity in
        one call.

        `on_expire` re-declares what the expiry sweep does when the new
        expiry passes: tombstone (default), demote (clear expiry, scale
        confidence down) or anonymize (replace the value with a marker).

        Returns {"touched": bool, "version": int|None, "expires_at": str|None};
        with `memory_keys`, also "results" (one per key) and "touched_count".
        `touched=False` means the key doesn't exist (or is tombstoned).
        """
        payload = self._identity(user_id, session_id)
        if memory_keys:
            payload["memory_keys"] = list(memory_keys)
        else:
            payload["memory_key"] = memory_key
        if clear_expiry:
            payload["clear_expiry"] = True
        else:
            payload["ttl_seconds"] = ttl_seconds
        if on_expire:
            payload["on_expire"] = on_expire
        if expire_reason:
            payload["expire_reason"] = expire_reason
        return await self._agent.transport.post("/v1/memory.touch", payload)

    async def lock(
        self,
        memory_key: str,
        *,
        reason: str,
        mode: Literal["read_only", "no_delete"] = "read_only",
        until: datetime | str | None = None,
        ttl_seconds: int | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Lock a memory against a class of writes.

        `read_only` refuses a changed value, a changed expiry and deletion;
        `no_delete` lets the value change but refuses tombstone / redact /
        clear / consolidate-away. Guarded ops answer 423 `LOCKED` with the
        mode, reason, locker and `unlock_via`. A writer may only tighten an
        existing lock; loosening is `unlock()` (admin role).

        Returns {"locked", "memory_id", "version", "lock_mode", "lock_until",
        "locked_by", "previous_mode"}; `locked=False` means no live row.
        """
        payload: dict[str, Any] = self._identity(user_id, session_id)
        payload.update({"memory_key": memory_key, "mode": mode, "reason": reason})
        if until is not None:
            payload["until"] = until.isoformat() if isinstance(until, datetime) else until
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        return await self._agent.transport.post("/v1/memory.lock", payload)

    async def apply(
        self,
        action: Literal["delete", "expire", "lock", "label", "demote"],
        *,
        reason: str,
        keys: Sequence[str] | None = None,
        filter: dict[str, Any] | None = None,
        search: str | dict[str, Any] | None = None,
        limit: int | None = None,
        dry_run: bool = True,
        expire: dict[str, Any] | None = None,
        lock: dict[str, Any] | None = None,
        label: dict[str, Any] | None = None,
        demote: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """One scope-bounded write over many rows.

        Exactly one of `keys`, `filter` (e.g. {"tags_any": ["q3-migration"]},
        {"created_before": ..., "confidence_lte": 0.3}) or `search` (a query
        string or {"query", "min_score"}); `limit` is required for filter and
        search. `dry_run` defaults to True: the first call previews the rows,
        the second call with dry_run=False writes. Per-row statuses come back
        in `rows` (would_apply | applied | refused | noop | failed); a lock or
        policy refusal is a row status, never a failed batch.

        Action blocks: expire={"ttl_seconds"|"until", "on_expire"?},
        lock={"mode", "until"?|"ttl_seconds"?}, label={"tags", "mode"},
        demote={"confidence_delta"|"confidence_factor", "floor"?}.
        """
        payload: dict[str, Any] = self._identity(user_id, session_id)
        target: dict[str, Any] = {}
        if keys is not None:
            target["keys"] = list(keys)
        if filter is not None:
            target["filter"] = filter
        if search is not None:
            target["search"] = {"query": search} if isinstance(search, str) else search
        payload.update({"target": target, "action": action, "reason": reason, "dry_run": dry_run})
        if limit is not None:
            payload["limit"] = limit
        blocks = (("expire", expire), ("lock", lock), ("label", label), ("demote", demote))
        for name, block in blocks:
            if block is not None:
                payload[name] = block
        return await self._agent.transport.post("/v1/memory.apply", payload)

    async def unlock(
        self,
        memory_key: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Lift a lock (admin role). Returns {"unlocked", "memory_id",
        "version", "previous_mode"}; `unlocked=False` when nothing was locked."""
        payload: dict[str, Any] = self._identity(user_id, session_id)
        payload["memory_key"] = memory_key
        return await self._agent.transport.post("/v1/memory.unlock", payload)

    async def outcome(
        self,
        memory_id: str,
        *,
        outcome: Literal["positive", "negative", "used"] = "positive",
        weight: int = 1,
        source: str = "",
    ) -> dict[str, Any]:
        """Record an outcome callback against a memory row.

        Positive outcomes bump `evidence_count`. Negatives are logged via
        audit but don't decrement — memories don't unlearn from one bad
        reference.

        Returns {"recorded": bool, "evidence_count_after": int}.
        """
        return await self._agent.transport.post(
            "/v1/memory.outcome",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "memory_id": memory_id,
                "outcome": outcome,
                "weight": weight,
                "source": source,
            },
        )

    async def outcome_by_key(
        self,
        memory_key: str,
        *,
        outcome: Literal["positive", "negative", "used"] = "positive",
        weight: int = 1,
        source: str = "",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an outcome against a memory identified by KEY, not memory_id.

        Recall paths (`search`/`get`) hand back `memory_key`; the server
        resolves key→id (namespace-wide, agent="*"), so this is a single call —
        a caller that just recalled a row can reinforce it without tracking the
        id or the writer agent. Returns the `outcome` response, or
        `{"recorded": False}` if the key doesn't resolve (tombstoned / never
        written).
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "memory_key": memory_key,
            "outcome": outcome,
            "weight": weight,
            "source": source,
        }
        if user_id is not None:
            payload["user_id"] = user_id
        return await self._agent.transport.post("/v1/memory.outcome", payload)

    async def outcome_run(
        self,
        memory_keys: list[str] | None = None,
        *,
        outcome: Literal["positive", "negative", "used"] = "positive",
        weight: int = 1,
        source: str = "",
        run_id: str = "",
    ) -> dict[str, Any]:
        """Apply one outcome to many recalled memories in a single call — the
        bulk form of `outcome`, so you skip a per-key loop.

        Pass the keys you recalled in `memory_keys`. If you omit them and pass
        `run_id`, the server derives the key set from the recall telemetry
        (loom.search_trace_log) for that run instead. Returns
        {"applied": int, "keys_matched": int, "run_id": str}.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "outcome": outcome,
            "weight": weight,
            "source": source,
        }
        if memory_keys:
            payload["memory_keys"] = list(memory_keys)
        if run_id or current_run_id():
            payload["run_id"] = run_id or current_run_id()
        return await self._agent.transport.post("/v1/memory.outcome_run", payload)

    async def search_trace_log(
        self,
        *,
        since_hours: int = 24,
        agent: str = "",
        run_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read per-query recall telemetry (loom.search_trace_log) for this
        namespace — query, returned keys+scores, abstain/risk, latency — to
        correlate recalled memories with downstream outcomes over the wire.
        Requires LOOM_SEARCH_TRACE_LOG_ENABLED on the server. Returns
        {"rows": [...]}.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "since_hours": since_hours,
            "limit": limit,
        }
        if agent:
            payload["agent"] = agent
        if run_id or current_run_id():
            payload["run_id"] = run_id or current_run_id()
        return await self._agent.transport.post("/v1/memory.search_trace_log", payload)

    async def delete(
        self,
        memory_key: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """Soft-delete an entry. Returns True if a tombstone was written,
        False if the entry didn't exist (idempotent no-op)."""
        payload = self._identity(user_id, session_id)
        payload["memory_key"] = memory_key
        result = await self._agent.transport.post("/v1/memory.delete", payload)
        return bool(result.get("tombstoned"))

    async def restore(
        self,
        memory_key: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        if_superseded_by: str | None = None,
        only_if_superseded: bool = False,
    ) -> dict[str, Any]:
        """Bring back a deleted memory (within the 14-day grace window) or one
        Loom retired on its own (status `superseded`).

        `if_superseded_by` restores the row only while it is still retired
        behind that memory id (as a `memory.auto_supersede` record names it);
        `only_if_superseded` restores it only while it is retired at all, never
        undoing a delete.

        Returns the full memory.restore response:
          - `restored: bool` — True if a live row was written.
          - `memory_id: str | None` — UUID of the restored row.
          - `version: int | None` — new version assigned.
          - `expired: bool` — True when the tombstone aged past the 14d
            grace window. The data is being GC'd; restoration is no longer
            possible.
          - `from_status: str` — `"tombstoned"` (a delete undone) or
            `"superseded"` (a retirement reversed); empty on a no-op.

        Preserves the original char_limit, pinned, tags, and entities
        across the restore. An undone delete clears `expires_at`, so the row
        gets a fresh lease (its old expiry may have passed, which would
        tombstone it again at once); an undone retirement keeps it.
        """
        payload = self._identity(user_id, session_id)
        payload["memory_key"] = memory_key
        if if_superseded_by:
            payload["if_superseded_by"] = if_superseded_by
        if only_if_superseded:
            payload["only_if_superseded"] = True
        return await self._agent.transport.post("/v1/memory.restore", payload)

    async def clear(
        self,
        confirm_subject: str,
        *,
        user_id: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Bulk-tombstone all memories in one scope. ADMIN role required.

        At least one of `user_id` or `agent` must name a concrete value
        other than `'-'`; `"*"` on the other clears every user's or every
        agent's rows.
        `confirm_subject` must equal a concrete value (or one of them, when
        both are set) — defense against accidental clears.

        Returns `{cleared_at, rows_affected: {table: count}, total, locked_skipped}`;
        locked rows stay and are counted in `locked_skipped`.
        Tombstone-not-hard-delete: cleared rows are recoverable for the
        14-day grace window by re-writing the same key. For
        cross-system erasure use `erasure.request_user_id` or
        `erasure.request_namespace`.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "confirm_subject": confirm_subject,
        }
        if user_id is not None:
            payload["user_id"] = user_id
        if agent is not None:
            payload["agent"] = agent
        return await self._agent.transport.post("/v1/memory.clear", payload)

    async def list(
        self,
        *,
        prefix: str | None = None,
        memory_type: MemoryType | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None = None,
        session_id: str | None = None,
        sort_by: str = "recency",
        quotas: dict[str, int] | None = None,
        as_of: datetime | None = None,
        include_duplicates: bool = False,
        tags: builtins.list[str] | None = None,
        tags_every: bool = False,
        fields: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """List memory entries under this identity tuple.

        Two modes:
          • `sort_by="recency"` (default) — order by memory_key.
          • `sort_by="score"` — order by `evidence_count + recall_count*0.5`
            DESC. The "hot memory" view. Optional `quotas: dict[memory_type, int]`
            caps per-type slot counts.

        `as_of` replays valid time — see `get(as_of=...)`.
        `include_duplicates=True` also lists `status='duplicate'` rows, which
        are skipped by default (their canonical is listed).

        `tags` keeps only rows carrying any one of them, in any case (search
        matches exactly); `tags_every=True` needs every one. `fields`
        whitelists the entry fields (`memory_key` always rides), e.g.
        `["memory_key", "value", "tags"]`.

        Returns a dict with `entries` (list of metadata records, no values
        — call `get` for values) and `truncated` (bool, true if more exist
        beyond `limit`); `offset` skips that many rows, so a caller pages
        with `offset += limit` while `truncated`. Score-ranked entries carry
        `evidence_count`,
        `recall_count`, and `last_recalled_at` on every row.
        """
        payload: dict[str, Any] = self._identity(user_id, session_id)
        payload["limit"] = limit
        payload["sort_by"] = sort_by
        if offset:
            payload["offset"] = offset
        if prefix is not None:
            payload["prefix"] = prefix
        if memory_type is not None:
            payload["memory_type"] = memory_type
        if quotas:
            payload["quotas"] = quotas
        if as_of is not None:
            payload["as_of"] = as_of.isoformat()
        if include_duplicates:
            payload["include_duplicates"] = True
        if tags:
            payload["tags"] = list(tags)
        if tags_every:
            payload["tags_every"] = True
        if fields:
            payload["fields"] = list(fields)
        return await self._agent.transport.post("/v1/memory.list", payload)

    async def dedup_list(
        self,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List clusters of status='duplicate' rows in this namespace
        (ADMIN role). Operator triage surface."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "limit": limit,
        }
        return await self._agent.transport.post("/v1/memory.dedup_list", payload)

    async def dedup_resolve(
        self,
        canonical_id: str,
        duplicate_id: str,
        *,
        decision: Literal["keep_canonical", "keep_duplicate", "keep_both"],
        confirm_canonical_key: str,
    ) -> dict[str, Any]:
        """Resolve one duplicate pair (ADMIN role), named by the two memory
        ids of a `dedup_list` cluster.

        `confirm_canonical_key` must equal the canonical row's key —
        mid-weight defense mirroring memory.clear's `confirm_subject`.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "canonical_id": canonical_id,
            "duplicate_id": duplicate_id,
            "decision": decision,
            "confirm_canonical_key": confirm_canonical_key,
        }
        return await self._agent.transport.post("/v1/memory.dedup_resolve", payload)

    async def dedup_collapse(
        self,
        *,
        prefix: str = "",
        dry_run: bool = True,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Collapse every duplicate pair of this namespace onto its canonical
        (WRITER role). No LLM, every key kept, nothing
        tombstoned: duplicates are already hidden from search and list; this
        repairs the search index's status mirror so the leg skips them
        before the vector sort. `dry_run` (default) only counts `pairs`,
        `orphans` and `mirror_stale`; `prefix` narrows the pass."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "prefix": prefix,
            "dry_run": dry_run,
            "limit": limit,
        }
        return await self._agent.transport.post("/v1/memory.dedup_collapse", payload)

    async def related(
        self,
        memory_key: str,
        *,
        top_k: int = 10,
        min_overlap: int = 1,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Entity-overlap recall.

        Returns memories sharing entities with the source, ranked by
        overlap count (vector distance ignored). Deterministic,
        symbol-anchored recall for indirect connections.

        Requires `LOOM_ENTITY_EXTRACT_ENABLED=true` on the server.
        With the extractor disabled, the source row's `entities` is
        `[]` and the call returns `results=[]` with
        `reason="source_has_no_entities"`. `reason="source_not_found"`
        means the memory_key doesn't exist under this identity.

        Returns `{results: [...], source_entities: [...], reason: ""}`.
        """
        payload = self._identity(user_id, session_id)
        payload["memory_key"] = memory_key
        payload["top_k"] = top_k
        payload["min_overlap"] = min_overlap
        return await self._agent.transport.post("/v1/memory.related", payload)

    async def provenance(self, memory_key: str) -> dict[str, Any]:
        """Where a memory came from and what it is linked to: source kind and
        label, the conversation thread (if retained), and every authored edge
        touching it with its relation, explanation, evidence and confidence."""
        return await self._agent.transport.post(
            "/v1/memory.provenance",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": self._agent.agent,
                "memory_key": memory_key,
            },
        )

    async def lineage(
        self,
        memory_key: str,
        *,
        candidate_match_window_s: int = 300,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Provenance walk for a shared memory — "how did this get here?"

        Returns `{found, memory_key, promotion_event, promoted_by,
        promoted_at, rationale, mechanism, supporters, candidate,
        audit_chain}`. `mechanism` is `'auto'` (auto-promote worker),
        `'review'` (learnings.review), or `'unknown'`. `candidate` is
        populated only on the review path when a best-effort match landed
        inside `candidate_match_window_s` — tagged
        `match_confidence='inferred'`.
        """
        payload = self._identity(user_id, session_id)
        payload["memory_key"] = memory_key
        payload["candidate_match_window_s"] = candidate_match_window_s
        return await self._agent.transport.post("/v1/memory.lineage", payload)

    async def promote(
        self,
        memory_key: str,
        *,
        target_key: str | None = None,
        rationale: str = "",
        approver: str | None = None,
        reversible_for_days: int = 7,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Promote a private memory to this namespace's shared layer.

        Reversible within `reversible_for_days` (default 7) via demote.
        After the window, demote still works but requires admin role.
        """
        payload: dict[str, Any] = self._identity(user_id, session_id)
        payload["memory_key"] = memory_key
        payload["rationale"] = rationale
        payload["reversible_for_days"] = reversible_for_days
        if target_key is not None:
            payload["target_key"] = target_key
        if approver is not None:
            payload["approver"] = approver
        return await self._agent.transport.post("/v1/memory.promote", payload)

    async def get_by_id(
        self,
        memory_id: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a memory by opaque UUID — the one returned from
        `memory.set` (or surfaced by an external system that received
        the id as a reference). Resolves across (namespace, agent,
        user_id, session_id) without the caller knowing which.
        Returns None when no live row matches.

        `as_of` replays valid time — see `get(as_of=...)`.
        """
        payload: dict[str, Any] = {"org": self._agent.org, "memory_id": memory_id}
        if as_of is not None:
            payload["as_of"] = as_of.isoformat()
        result = await self._agent.transport.post(
            "/v1/memory.get_by_id",
            payload,
        )
        if not result.get("found"):
            return None
        return result

    async def recall(
        self,
        *,
        query: str = "",
        since: Any = None,
        until: Any = None,
        thread: str = "",
        role: str = "",
        match: str = "all",
        order: str = "auto",
        top_k: int = 20,
        cursor: str = "",
        offset: int = 0,
        max_chars: int = 600,
        read_scope: str = "namespace",
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Search the raw conversation turns behind this namespace's memories
        by text and by time — the messages
        `set_from_messages` extracted from, not the extracted facts.

        At least one of `query` / `since` / `until` / `thread` is required
        (server refuses an unscoped read with 400 RECALL_REQUIRES_FILTER).
        `match="all"` needs every query token in a turn; `"any"` admits a
        turn carrying any of them, ranked by how many. `order="auto"` is
        relevance with a query and newest-first without one; newest-first
        pages by `cursor`, relevance by `offset`.

        Returns `{"turns": [...], "total_matched": int, "has_more": bool,
        "next_cursor": str, "next_offset": int, ...}`.
        """
        if not (query.strip() or since is not None or until is not None or thread):
            raise ValueError(
                "memory.recall requires at least one of query / since / until / thread"
            )
        payload: dict[str, Any] = self._identity(user_id, session_id)
        payload.update(
            {
                "query": query,
                "thread": thread,
                "role": role,
                "match": match,
                "order": order,
                "top_k": top_k,
                "cursor": cursor,
                "offset": offset,
                "max_chars": max_chars,
                "read_scope": read_scope,
            }
        )
        if since is not None:
            payload["since"] = since.isoformat() if hasattr(since, "isoformat") else since
        if until is not None:
            payload["until"] = until.isoformat() if hasattr(until, "isoformat") else until
        return await self._agent.transport.post("/v1/memory.recall", payload)

    async def timeline(
        self,
        *,
        entity: str = "",
        tags: builtins.list[str] | None = None,
        memory_key_prefix: str = "",
        since: Any = None,
        until: Any = None,
        top_k: int = 50,
        order: str = "desc",
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Return memories about an entity (or matching tags / key
        prefix) in strict temporal order — newest first by default.

        Sort key is `coalesce(valid_at, created_at)`; events with an
        explicit `valid_at` carry that as their position, undated
        events sort by insertion time. Filter shape: at least one of
        `entity` / `tags` / `memory_key_prefix` must be set so the
        result is scoped (server refuses unscoped queries with 400
        TIMELINE_REQUIRES_FILTER).

        Returns `{"events": [...], "total_matching": int}` — the
        latter lets you tell if there are more events beyond the page.
        """
        if not (entity or tags or memory_key_prefix):
            raise ValueError(
                "memory.timeline requires at least one of entity / tags / memory_key_prefix"
            )
        payload: dict[str, Any] = self._identity(user_id, session_id)
        payload.update(
            {
                "entity": entity,
                "tags": list(tags or []),
                "memory_key_prefix": memory_key_prefix,
                "top_k": top_k,
                "order": order,
            }
        )
        if since is not None:
            payload["since"] = since.isoformat() if hasattr(since, "isoformat") else since
        if until is not None:
            payload["until"] = until.isoformat() if hasattr(until, "isoformat") else until
        return await self._agent.transport.post("/v1/memory.timeline", payload)

    async def history(
        self,
        memory_key: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Enumerate the version history of `memory_key`. Includes
        tombstoned + redacted rows so the timeline is complete — the
        natural complement of memory.redact (find the version to redact)
        and a full audit trail surface for compliance flows.
        """
        payload: dict[str, Any] = self._identity(
            user_id, session_id, run_id=run_id, agent_id=agent_id
        )
        payload.update({"memory_key": memory_key, "limit": limit})
        r = await self._agent.transport.post("/v1/memory.history", payload)
        return list(r.get("rows", []))

    async def redact(
        self,
        memory_id: str,
        version: int,
        *,
        reason: str = "",
        redacted_by: str = "",
    ) -> dict[str, Any]:
        """Scrub the value of one historical memory row, preserving the
        audit chain. Addressed by (memory_id, version) — every version of a
        memory shares one memory_id, so `version` (from a `set` response or
        `history`) picks the specific row. Refuses to redact the live head —
        caller must memory.delete first, then redact the prior row.

        Returns `{"redacted": bool, "refused_head": bool, "redacted_at": ...}`.
        """
        return await self._agent.transport.post(
            "/v1/memory.redact",
            {
                "org": self._agent.org,
                "memory_id": memory_id,
                "version": version,
                "reason": reason,
                "redacted_by": redacted_by,
            },
        )

    async def update_by_id(
        self,
        memory_id: str,
        value: Any = None,
        *,
        if_match_sha256: str | None = None,
        char_limit: int | None = None,
        tags: Sequence[str] | None = None,
        concepts: Sequence[str] | None = None,
        relations: Sequence[tuple[str, str, str]] | None = None,
        revive: bool = False,
    ) -> dict[str, Any]:
        """Update a memory's `value` by opaque memory_id. Preserves
        every other column (memory_type, identity, confidence, ...).
        Bumps the version and (when searchable) refreshes the embedding.

        Returns `{"updated": True, "new_version": <int>, "content_sha256": ...}`
        on hit, `{"updated": False, "new_version": 0}` if memory_id
        doesn't resolve to a live row. Pass `if_match_sha256` for a
        guaranteed-no-clobber update — server returns 409 on mismatch.

        `char_limit`: same semantics as `set()`. Server writes through —
        callers that want to preserve an existing cap read the row's
        current `char_limit` first and pass it back here.
        `value=None` keeps the stored value: pass `tags` / `concepts` alone
        to rewrite the row's facets (the whole row is re-emitted and the
        embedding refreshed, so a key-facet vector follows the tags).
        `relations`: the facts the new value states, as `set()` takes them.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "memory_id": memory_id,
        }
        if value is not None:
            payload["value"] = value
        if revive:
            payload["revive"] = True  # an expired row answers 409 EXPIRED otherwise
        if if_match_sha256 is not None:
            payload["if_match_sha256"] = if_match_sha256
        if char_limit is not None:
            payload["char_limit"] = char_limit
        if tags is not None:
            payload["tags"] = list(tags)
        if concepts is not None:
            payload["concepts"] = list(concepts)
        if relations:
            payload["relations"] = [list(f) for f in relations]
        return await self._agent.transport.post(
            "/v1/memory.update_by_id",
            payload,
        )

    async def update(
        self,
        memory_key: str,
        value: Any = None,
        *,
        namespace: str | None = None,
        agent: str = "",
        user_id: str = "",
        session_id: str = "",
        if_match_sha256: str | None = None,
        char_limit: int | None = None,
        tags: Sequence[str] | None = None,
        concepts: Sequence[str] | None = None,
        confidence: float | None = None,
        reason: str = "",
        relations: Sequence[tuple[str, str, str]] | None = None,
        revive: bool = False,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Update a memory by key — the fact changed or got more specific.
        Resolves `memory_key` namespace-wide unless
        narrowed by `agent` / `user_id` / `session_id`; the row keeps its
        type, identity, tags, confidence and lock, gets a new version and a
        refreshed embedding.

        Returns `{"updated": True, "memory_id", "memory_key", "new_version",
        "content_sha256"}` on hit, `{"updated": False, "reason": "not_found"}`
        when no live row carries the key, and raises on 409 AMBIGUOUS_KEY
        (detail lists the candidate rows) or PRECONDITION_FAILED.

        `run_id` stamps the revision with a run; unset, the active
        `run_scope`'s.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": namespace or self._agent.namespace,
            "memory_key": memory_key,
        }
        if value is not None:
            payload["value"] = value
        for k, v in (("agent", agent), ("user_id", user_id), ("session_id", session_id)):
            if v:
                payload[k] = v
        if revive:
            payload["revive"] = True
        if if_match_sha256 is not None:
            payload["if_match_sha256"] = if_match_sha256
        if char_limit is not None:
            payload["char_limit"] = char_limit
        if tags is not None:
            payload["tags"] = list(tags)
        if concepts is not None:
            payload["concepts"] = list(concepts)
        if confidence is not None:
            payload["confidence"] = confidence
        if reason:
            payload["reason"] = reason
        if relations:
            payload["relations"] = [list(f) for f in relations]
        run_id = run_id or current_run_id()
        if run_id:
            payload["run_id"] = run_id
        return await self._agent.transport.post("/v1/memory.update", payload)

    async def share(
        self,
        memory_key: str,
        *,
        target_key: str | None = None,
        rationale: str = "",
        reversible_for_days: int = 7,
        approver: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Share a per-agent memory with every agent in this namespace.

        Thin alias over `promote()` — same wire op, naming that matches the
        intent: *"this learning should propagate"*. Other agents in the
        namespace pick it up automatically through `memory.search(scope="shared")`.

        Reversible within `reversible_for_days` (default 7) via `demote()`.
        """
        return await self.promote(
            memory_key,
            target_key=target_key,
            rationale=rationale,
            reversible_for_days=reversible_for_days,
            approver=approver,
            user_id=user_id,
            session_id=session_id,
        )

    async def demote(
        self,
        memory_key: str,
        *,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Reverse a promotion. Idempotent: demoting a non-existent shared
        key returns `demoted=False`."""
        return await self._agent.transport.post(
            "/v1/memory.demote",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "memory_key": memory_key,
                "rationale": rationale,
            },
        )

    async def bulk_set(
        self,
        entries: builtins.list[dict[str, Any]],
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Write many memory entries in one batched insert.

        Each entry is a dict with at least `memory_key`, `value`, `memory_type`.
        Optional per-entry: `confidence`, `source_trace`, `ttl_seconds`,
        `tags`, `entities`, `valid_at`, `char_limit`, `pinned`.

        Returns the server response: `written` in input order (each row's
        `memory_key`, `version`, `conflict_verdict`), and the keys that were
        not written and why — `skipped_pinned` / `skipped_locked` (the live
        row is pinned or locked), `skipped_blacklisted`, `merged`,
        `rejected_pinned`. A row not written has version 0, and its
        `conflict_verdict` says why: `blacklisted`, `blacklist_unchecked`
        (too long for this request's blacklist regexes: send it in a smaller
        request), `pinned`, `locked` or `rejected` (close to a pinned row), with
        `pinned_blocker_id` naming the pinned row for the last two.

        `idempotency_key`: unlike `set` (idempotent on identity+memory_key),
        a bulk insert is replayed verbatim — an app-level retry of a failed
        chunk double-writes. Pass a stable key derived from the chunk
        (e.g. a hash of the memory_keys) so the server collapses retries;
        omitting it lets the transport mint a fresh per-call uuid (no
        retry protection). Batch backfills that retry on transient failure
        MUST pass one.
        """
        payload: dict[str, Any] = self._identity(user_id, session_id)
        payload["entries"] = entries
        return await self._agent.transport.post(
            "/v1/memory.bulk_set", payload, idempotency_key=idempotency_key
        )

    async def bulk_get(
        self,
        memory_keys: builtins.list[str],
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        fields: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch many memory entries in one round-trip.

        Returns {"results": {memory_key: entry, ...}, "missing": [keys, ...]}.
        The map key is `results` (not `found`) so it doesn't read as
        `found.found` against each entry's own `found: bool`.

        `fields` whitelists the fields of every entry
        (each keeps `found`); None = every field."""
        payload: dict[str, Any] = self._identity(user_id, session_id)
        payload["memory_keys"] = list(memory_keys)
        if fields:
            payload["fields"] = list(fields)
        return await self._agent.transport.post("/v1/memory.bulk_get", payload)

    async def set_from_messages(
        self,
        messages: builtins.list[dict[str, object]],
        *,
        extra_tags: builtins.list[str] | None = None,
        max_tokens: int = 4096,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        valid_at: datetime | None = None,
        observation_date: str = "",
        record_schema: dict[str, str | builtins.list[str]] | None = None,
        memory_key: str | None = None,
        judge_extraction: bool = False,
        defer: bool = False,
    ) -> dict[str, object]:
        """Extract memorable facts from a raw conversation and save them.

        `observation_date` (ISO ``YYYY-MM-DD``) anchors relative-time
        phrases in the messages ("last week", "two months ago") so the
        extractor resolves them to absolute dates. Pass the date the
        conversation HAPPENED — for live traffic that's today; for
        backfills it's the transcript's own timestamp.

        Without `record_schema` the LLM decides what is worth saving, as
        free-form facts under generated keys: for callers that cannot decide
        inline what to save. For pre-decided memories use `set(...)`.

        With `record_schema` it extracts ONE record into `memory_key`
        (required): a dict of field name → "string" | "number" | "integer" |
        "boolean", or a list of strings for an enum the LLM must pick from,
        e.g. `{"amount": "number", "category": ["food", "transit"]}`. Use
        enums for categorical fields so `aggregate` does not fragment over
        "Coffee" / "coffee" / "food".

        A message may carry `timestamp` (ISO datetime): the evidence row
        records that moment and facts about the user's state at the time of
        speaking are anchored to the turn, not the session day.

        `defer=True` queues the request instead of extracting inline: the
        call returns in tens of milliseconds with `deferred: True` and a
        `queue_id`; the server's extraction_queue worker writes the same
        memories shortly after. Until then `search`/`ask` report
        `pending_extraction > 0` for the scope, and `extraction_status()`
        shows the batch. Use on an agent's hot path; keep the default when
        you need the written keys back now.

        Returns a dict with `written: list[{memory_key, version}]`,
        `refused: list[{memory_key, verdict}]` (the extracted facts the write
        refused: a blacklisted, pinned or locked key, or a pinned row close
        by), `tokens_in`, `tokens_out`, `llm_provider`, `llm_model` (or
        `deferred: True, queue_id` when deferred).
        """
        payload: dict[str, object] = self._identity(
            user_id, session_id, run_id=run_id, agent_id=agent_id
        )
        payload["messages"] = list(messages)
        payload["max_tokens"] = max_tokens
        if defer:
            payload["defer"] = True
        if judge_extraction:
            payload["judge_extraction"] = True
        if extra_tags:
            payload["extra_tags"] = list(extra_tags)
        if valid_at is not None:
            payload["valid_at"] = valid_at.isoformat()
        if observation_date:
            payload["observation_date"] = observation_date
        if record_schema is not None:
            if memory_key is None:
                raise ValueError(
                    "memory_key is required when record_schema is set — "
                    "the typed record needs a stable address for aggregate / "
                    "predicate queries to address."
                )
            payload["record_schema"] = record_schema
            payload["memory_key"] = memory_key
        return await self._agent.transport.post(
            "/v1/memory.set_from_messages",
            payload,
        )

    async def extraction_status(
        self,
        *,
        queue_id: str = "",
        status: str = "",
        limit: int = 50,
        user_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, object]:
        """Lifecycle of deferred `set_from_messages(defer=True)` batches in
        scope: `batches: [{queue_id, status, attempts, written, last_error,
        ...}]`, `pending`, `failed`. `status` filters to one of
        pending | running | done | failed; `queue_id` to one batch."""
        payload: dict[str, object] = dict(self._identity(user_id, session_id, agent_id=agent_id))
        if queue_id:
            payload["queue_id"] = queue_id
        if status:
            payload["status"] = status
        payload["limit"] = limit
        return await self._agent.transport.post(
            "/v1/memory.extraction_status",
            payload,
        )

    async def aggregate(
        self,
        *,
        agg_func: str = "count",
        agg_field: str = "*",
        group_by: builtins.list[str] | None = None,
        filter: dict[str, str] | None = None,
        limit: int = 100,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate over typed records in memory.

        Pairs with `set_from_messages(record_schema=...)`. The server
        runs deterministic SQL aggregation (count / sum / avg / min /
        max) over JSON-extracted fields in `loom.memory.value`, scoped
        to the caller's identity tuple.

        Use this when the question needs exact arithmetic, not similarity
        ranking — "how many BBQ events this year", "total expenses by
        category", "average sleep hours". `memory.search` can't answer
        these because vector similarity doesn't preserve predicate
        truth.

        `agg_field` is the JSON field (or "*" with count); `group_by` takes
        up to 3 fields; `filter` is an equality filter on string fields,
        ANDed with the identity tuple; `limit` is capped at 1000.

        Returns dict with `rows: [{group: {...}, metric: float}, ...]`
        and `sql_executed` (the actual SQL the server ran, for debugging).
        Rows whose `value` isn't JSON-shaped are silently excluded —
        the JSON-extract path returns empty / 0 for non-typed memories
        rather than failing the query.
        """
        payload: dict[str, Any] = self._identity(
            user_id, session_id, run_id=run_id, agent_id=agent_id
        )
        payload["agg_func"] = agg_func
        payload["agg_field"] = agg_field
        payload["limit"] = limit
        if group_by:
            payload["group_by"] = list(group_by)
        if filter:
            payload["filter"] = dict(filter)
        return await self._agent.transport.post("/v1/memory.aggregate", payload)

    async def demonstrate(
        self,
        query: str,
        *,
        k: int = 8,
        example_tag: str = "example",
        label_prefix: str = "label:",
        min_margin: float = 0.0,
    ) -> dict[str, Any]:
        """Nearest labelled examples for `query`: demonstrations, a
        similarity-weighted label vote, `predicted_label`, `margin`,
        `abstain`, and a `few_shot_block`. Examples are
        memories tagged `example_tag` plus `label_prefix + label`."""
        return await self._agent.transport.post(
            "/v1/memory.demonstrate",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": "*",
                "query": query,
                "k": k,
                "example_tag": example_tag,
                "label_prefix": label_prefix,
                "min_margin": min_margin,
            },
        )

    async def search(
        self,
        query: str,
        *,
        memory_type: SearchableMemoryType | None = None,
        tags: builtins.list[str] | None = None,
        scope: Scope = "private",
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        entity_filter: builtins.list[str] | None = None,
        top_k: int = 5,
        search_mode: str = "rrf",
        alpha: float = 0.5,
        auto_touch: bool = True,
        namespace_scope: str | None = None,
        bodies: str | None = None,
        body_budget_tokens: int | None = None,
        recipe: str = "",
        hyde: bool | None = None,
        rerank: str = "",
        retrieval_budget: str | None = None,
        category: str = "",
        observation_date: str = "",
        include_top_n_unmatched: int = 0,
        tags_soft: bool = False,
        tag_boost: float = 0.3,
        category_soft: bool = False,
        category_boost: float = 0.3,
        confidence_weight: float | None = None,
        demote_tags: builtins.list[str] | None = None,
        reinforcement_weight: float | None = None,
        reinforcement_half_life_days: float | None = None,
        as_of: datetime | None = None,
        include_tags: bool = False,
        include_usage: bool = False,
        include_duplicates: bool = False,
        exclude_keys: builtins.list[str] | None = None,
        explain: bool | None = None,
        **extra: Any,
    ) -> MemorySearchResult:
        """Search this agent's memory: ranked hits over the searchable types
        (`semantic`, `procedural`, `preference`; episodic and profile rows are
        not embedded), each with cosine `distance` and `similarity`.

        Defaults are the server's wire defaults, so this and a raw
        `/v1/memory.search` POST behave the same:
          • `search_mode="rrf"` — Reciprocal Rank Fusion of the lexical and
            semantic legs, the right default for mixed prose + identifier
            queries. Others: "semantic", "lexical", "hybrid" (alpha blend),
            "bm25_hybrid", "related", "graph", "temporal".
          • `namespace_scope=None` — the deployment default (`this`).
            "all_mine" also reads the caller's own namespaces, up to the
            fan-out cap; "this" pins the addressed one.

        Every other knob rides only when set, so the server's default governs
        an unset one; `loom.ops.MemorySearchInput` documents each field, and a
        field without a kwarg here rides `**extra`. `tags_soft` /
        `category_soft` turn the `tags` / `category` filter into a rank boost
        of `tag_boost` / `category_boost`; `demote_tags` sinks rows, it does
        not drop them; `hyde=False` forces raw vector recall on cheap hot-loop
        scans.

        `auto_touch=True` bumps `recall_count` / `last_recalled_at` on every
        served row; pass False on hot-loop scans. A reinforcement credit is
        written only for a reader that can close the loop: a named `consumer`
        on the Agent plus a run (`run_scope` / `auto_run`), a `session_id`, or
        a closer posted under that consumer within 14 days. The result's
        `reinforcement` says which. The default cross-agent read (`agent:
        "*"`) is credited like any other when the Agent names a `consumer`;
        without one it is a measurement read, as are the bench labels
        (`eval-*`, `latency-bench`, `f1-drive`). `session_id` (like `run_id`)
        filters the read to rows written under it; a run opened by
        `run_scope` / `auto_run` labels the read without filtering it.

        `as_of` replays valid time: rows whose `coalesce(valid_at,
        created_at) > as_of` are excluded. `as_of_basis="knowledge_time"`
        (through `**extra`) replays what Loom had recorded by then instead.

        Above `top_k` 15 an unpinned `bodies` serves the excerpt tier and hits
        carry no `explanation` / `score_components`; `bodies="full"` and/or
        `explain=True` keep them, and `explain=False` drops them at any k.

        Returns a `MemorySearchResult`: the hits plus the response envelope.
        Check `abstain` before answering from weak hits, and branch on
        `abstain_code`, not the free-text `abstain_reason`.
        """
        payload = _search_payload(
            org=self._agent.org,
            namespace=self._agent.namespace,
            query=query,
            scope=scope,
            top_k=top_k,
            search_mode=search_mode,
            alpha=alpha,
            auto_touch=auto_touch,
            agent_id=agent_id,
            tag_boost=tag_boost,
            category_boost=category_boost,
            namespace_scope=namespace_scope,
            memory_type=memory_type,
            tags=tags,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            entity_filter=entity_filter,
            recipe=recipe,
            hyde=hyde,
            rerank=rerank,
            retrieval_budget=retrieval_budget,
            category=category,
            observation_date=observation_date,
            include_top_n_unmatched=include_top_n_unmatched,
            tags_soft=tags_soft,
            category_soft=category_soft,
            confidence_weight=confidence_weight,
            demote_tags=demote_tags,
            reinforcement_weight=reinforcement_weight,
            reinforcement_half_life_days=reinforcement_half_life_days,
            as_of=as_of,
            bodies=bodies,
            body_budget_tokens=body_budget_tokens,
            include_tags=include_tags,
            include_usage=include_usage,
            include_duplicates=include_duplicates,
            # Keys the caller already holds leave the pool before the window
            # is cut, so the window is still top_k long.
            exclude_keys=exclude_keys,
            explain=explain,
        )
        # Any further server field rides the body as-is; see
        # loom.ops.MemorySearchInput for the names.
        payload.update(extra)
        result = await self._agent.transport.post("/v1/memory.search", payload)
        return _search_result(result)

    async def read_traces(
        self,
        query: str,
        *,
        top_k: int = 25,
        body_budget_tokens: int = 32000,
        **kwargs: Any,
    ) -> MemorySearchResult:
        """Read run traces through their facts: a fact-sized
        window (`top_k=25`) retrieves by the extracted facts — the actor, the
        slot, the decided value — and `bodies="source"` serves
        each hit its linked source passage, one per passage, within a
        trace-sized budget. Measured on MemoryArena group travel: 0.936 against
        0.778 for the same facts read as facts and 0.914 for whole traces
        retrieved directly. Any `search()` keyword passes through."""
        return await self.search(
            query,
            top_k=top_k,
            bodies="source",
            body_budget_tokens=body_budget_tokens,
            **kwargs,
        )

    async def search_trace(
        self,
        query: str,
        *,
        memory_type: SearchableMemoryType | None = None,
        scope: Scope = "private",
        user_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        entity_filter: builtins.list[str] | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Same shape as `search()`; returns the vector leg's per-candidate
        scoring breakdown (no fusion, assembly or reranker): each row carries
        `similarity`, `entity_boost`, `rank_score` and `rank`. Why a served row
        ranked where it did: `search(..., explain=True)` and its
        `score_components`.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "agent": agent_id or self._agent.agent,
            "query": query,
            "scope": scope,
            "top_k": top_k,
        }
        if memory_type is not None:
            payload["memory_type"] = memory_type
        if user_id is not None:
            payload["user_id"] = user_id
        if run_id is not None or current_run_id():
            payload["run_id"] = run_id if run_id is not None else current_run_id()
        if entity_filter:
            payload["entity_filter"] = entity_filter
        return await self._agent.transport.post("/v1/memory.search_trace", payload)

    async def assign_category(
        self,
        memory_key: str,
        category_name: str,
        *,
        tombstone: bool = False,
    ) -> dict[str, Any]:
        """Link a memory to a registered category in this namespace.

        Many-to-many — a memory can be in multiple categories. The
        category must exist via `namespaces.register_category(...)`
        first (404 if not).

        Pass `tombstone=True` to rescind a prior assignment.
        """
        return await self._agent.transport.post(
            "/v1/memory.assign_category",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "memory_key": memory_key,
                "category_name": category_name,
                "tombstone": bool(tombstone),
            },
        )

    async def link(
        self,
        from_key: str,
        to_key: str,
        *,
        relation: str = "",
        label: str = "",
        explanation: str = "",
        evidence: Sequence[str] | None = None,
        confidence: float = 1.0,
        run_id: str = "",
        weight: float = 1.0,
        tombstone: bool = False,
    ) -> dict[str, Any]:
        """Author an explicit edge between two memories (or entities).

        Loom's `loom.entity_edges` is co-occurrence-derived; this adds
        an operator-authored layer for relationships text doesn't make
        obvious ("X drove Y", "this blocks that"). Graph-mode search
        will traverse both legs.

        `relation` is canonicalized server-side (supports, depends_on,
        caused, contradicts, refines, implements, validates, supersedes,
        blocks, fixes, derived_from, related_to; synonyms fold onto these).
        Carry the edge's justification: one sentence of `explanation`, the
        `evidence` behind it, and `confidence` in the relationship itself.
        `contradicts` between two live memories also opens a conflict
        (`conflict_opened` in the response). Pass `tombstone=True` to
        rescind a prior link.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "from_key": from_key,
            "to_key": to_key,
            "relation": relation,
            "label": label,
            "explanation": explanation,
            "evidence": list(evidence or []),
            "confidence": float(confidence),
            "weight": float(weight),
            "tombstone": bool(tombstone),
        }
        if run_id or current_run_id():
            payload["run_id"] = run_id or current_run_id()
        return await self._agent.transport.post("/v1/memory.link", payload)
