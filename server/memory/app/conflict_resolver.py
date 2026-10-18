"""The conflict resolver: one LLM judgement per marked row, against its nearest live
peers, and what it does about each pair (`domain.conflict_resolution.action_for`):
retire the peer behind the row (`apply_refines`, recorded and followed), flag the pair,
clear a flag between them, or nothing.

Every write follows one rule: re-read the row under its key's write lock, check the
org's erasure, and write at the read version + 1, only while the row still holds the
content judged. A pinned row is never touched. The row is then stamped judged at its
content hash. The invariants are listed in docs/architecture/memory-architecture.md."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import server.memory.store as memory_repo
from server.core.datetimes import naive_utc_now, to_naive_utc
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain import conflict_resolution as cr
from server.memory.domain.conflicts import ConflictPeer
from server.memory.store import conflict_resolver as queue
from server.settings import settings

log = logging.getLogger(__name__)

AUDIT_EVENT = "memory.conflict_resolver"
# The judge's thresholds were measured at this reasoning effort (OpenAI models).
REASONING_EFFORT = "low"
_LIVE = ("active", "conflicted")


class ModelUnavailable(RuntimeError):
    """The provider refused the resolver's model or key (401, 403, 404): the same
    answer for every row, so the pass stops and the rows stay owed."""


class CutOff(RuntimeError):
    """The answer stopped at the output cap twice: no judgement, and asking again gets
    the same, so the row is stamped judged and counted errored."""


NO_KEY = "no key for"

# The stop reasons of an answer cut at the output cap (OpenAI, Anthropic).
_CUT = ("length", "max_tokens")


class Unjudgeable(RuntimeError):
    """The provider refused this row's request for a reason of its own (a 4xx other
    than 408, 409 and 429): asking again gets the same answer."""


@dataclass
class Counts:
    """One pass, for the worker status and the log line."""

    judged: int = 0
    retired: int = 0
    flagged: int = 0
    cleared: int = 0
    skipped_low_confidence: int = 0
    skipped_changed: int = 0
    skipped_pinned: int = 0
    skipped_budget: int = 0
    # Namespaces left out of the pass's queue read: over their daily cost cap.
    capped_namespaces: int = 0
    errored: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _plain(stored: str) -> str:
    """A stored value as text: a JSON string is shown unquoted."""
    try:
        value = json.loads(stored)
    except (TypeError, ValueError):
        return stored
    return value if isinstance(value, str) else stored


def _client() -> Any:
    from server.llm.client import get_llm_client_without_fallback

    return get_llm_client_without_fallback(
        settings.conflict_resolver_model, reasoning_effort=REASONING_EFFORT
    )


def model_missing() -> str | None:
    """Why no call can be made, known before any read: no key for the model (the
    client is the fake stand-in). None when a call can be made."""
    from server.llm.fake import FakeLLMClient

    if isinstance(_client(), FakeLLMClient):
        return f"{NO_KEY} {settings.conflict_resolver_model}"
    return None


async def judge(new: cr.Note, peers: list[cr.Note]) -> tuple[dict[str, cr.PeerVerdict], str]:
    """One call over the cluster: ({peer memory_id: verdict}, the model that answered).
    Malformed output is {}. Never answered by the fallback model: the thresholds are
    this model's. Raises `ModelUnavailable` when the provider refuses the
    model or the key, `Unjudgeable` when it refuses this request, `CutOff` when the
    answer is cut at the output cap even with twice the room, and any other failure
    as it came."""
    import httpx

    from server.llm.fake import FakeLLMClient
    from server.observability.genai import llm_stage

    llm = _client()
    if isinstance(llm, FakeLLMClient):
        # The stand-in for a missing key: its echo is no judgement. The rows stay owed.
        raise ModelUnavailable(f"{NO_KEY} {settings.conflict_resolver_model}")
    cap = cr.max_output_tokens(len(peers))
    try:
        with llm_stage("conflict_resolver"):
            res = await llm.complete(
                cr.SYSTEM_PROMPT,
                cr.user_prompt(new, peers),
                max_tokens=cap,
                response_format=cr.RESPONSE_FORMAT,
            )
            if str(getattr(res, "stop_reason", "") or "") in _CUT:
                # Cut at the cap: a cut JSON answer parses to nothing. Once more with room.
                res = await llm.complete(
                    cr.SYSTEM_PROMPT,
                    cr.user_prompt(new, peers),
                    max_tokens=2 * cap,
                    response_format=cr.RESPONSE_FORMAT,
                )
    except httpx.HTTPStatusError as e:
        status, reason = e.response.status_code, str(e).splitlines()[0]
        if status in (401, 403, 404):
            raise ModelUnavailable(reason) from e
        if 400 <= status < 500 and status not in (408, 409, 429):
            raise Unjudgeable(reason) from e
        raise  # 408, 409, 429, 5xx: transient, the row stays owed
    if str(getattr(res, "stop_reason", "") or "") in _CUT:
        raise CutOff(f"the answer stopped at the output cap ({2 * cap} tokens) twice")
    answered = str(getattr(res, "model", "") or settings.conflict_resolver_model)
    return cr.parse_verdicts(str(getattr(res, "text", "") or ""), peers), answered


async def _head(org: str, memory_id: str) -> Any:
    return await memory_repo.get_by_id(
        memory_id, org=org, scope=WHOLE_NAMESPACE, fresh=True, strong=True
    )


def _eligible(row: Any) -> bool:
    """Whether a row may be judged or acted on: live, not deleted, not expired, its
    fact not ended, and not labelled 'high' (the rule `_PEER_ROWS_SQL` applies to
    peers). The content hash does not cover the label, so this is read every time."""
    now = naive_utc_now()

    def ended(t: Any) -> bool:
        return t is not None and to_naive_utc(t) <= now

    return (
        row is not None
        and str(row.status) in _LIVE
        and row.tombstoned_at is None
        and not ended(row.expires_at)
        and not ended(row.valid_to)
        and str(row.sensitivity or "") != "high"
    )


def _holds(row: Any, namespace: str, sha: str) -> bool:
    """Whether a re-read row is still the eligible write that was judged."""
    return (
        _eligible(row) and str(row.namespace) == namespace and str(row.content_sha256 or "") == sha
    )


def _conflict_peer(row: Any) -> ConflictPeer:
    return ConflictPeer(
        memory_id=str(row.memory_id),
        memory_key=str(row.memory_key),
        similarity=1.0,
        pinned=bool(row.pinned),
        canonical="",
        agent=str(row.agent),
        user_id=str(row.user_id),
        session_id=str(row.session_id),
        run_id=str(getattr(row, "run_id", "-") or "-"),
    )


def _field(r: Any, name: str) -> Any:
    return r.get(name) if isinstance(r, Mapping) else getattr(r, name, None)


async def _assertion_order(org: str, namespace: str, rows: list[Any]) -> dict[str, tuple[int, int]]:
    """{memory_id: (when its statement holds, when its value was first asserted)}, in
    wall-clock ns, for rows (`MemoryRow`s or peer dicts). Finer than the second, and
    a metadata write (touch, pin, tags) keeps the hash, so it does not move a row. An
    explicit valid_at earlier than the assertion moves it back: the fact held then."""
    keys = [(str(_field(r, "memory_id")), str(_field(r, "content_sha256") or "")) for r in rows]
    first = await queue.asserted_versions(org, namespace, keys)
    out: dict[str, tuple[int, int]] = {}
    for r in rows:
        mid = str(_field(r, "memory_id"))
        # A row the history does not hold: its current version.
        asserted = first.get(mid, int(_field(r, "version") or 0))
        holds = asserted
        valid = _field(r, "valid_at")
        if isinstance(valid, datetime):
            holds = min(holds, calendar.timegm(valid.utctimetuple()) * 1_000_000_000)
        out[mid] = (holds, asserted)
    return out


async def _retire(org: str, namespace: str, row: Any, peer: Any) -> Any:
    """Supersede `peer` behind the judged row `row`; the caller has checked that `peer`
    was stated first. Returns what `_after_retire` records and follows, or None.

    The record and the follow are queued for the projection repair before the
    supersede commits (FOLLOW_OWED, with the record's entry; the repair acts on a pair
    only while its loser is retired behind its winner, so an intent whose retirement
    never landed changes nothing), and resolved once both land: a crash or a failed
    emit in between leaves them for the repair. No intent, no retirement."""
    from server.memory.app.auto_resolution import CONFLICT_RESOLVER, retired_entry
    from server.memory.app.conflicts import apply_refines
    from server.memory.app.projection_failures import FOLLOW_OWED, record_projection_failure

    loser, winner = peer, row
    loser_sha = str(loser.content_sha256 or "")
    pair = (str(loser.memory_id), str(winner.memory_id))
    intent = await record_projection_failure(
        org=org,
        namespace=namespace,
        agent=str(winner.agent),
        projection=FOLLOW_OWED,
        source_operation=f"worker.{CONFLICT_RESOLVER}",
        source_resource_id=f"{CONFLICT_RESOLVER}:{pair[1]}",
        error="conflict_resolver: retirement to follow",
        payload={
            "pairs": [list(pair)],
            "retired": [
                retired_entry(
                    loser,
                    superseded_by=pair[1],
                    superseded_by_key=str(winner.memory_key),
                    label=cr.UPDATE,
                )
            ],
        },
    )
    into: dict[str, list[dict[str, Any]]] = {}
    await apply_refines(
        org=org,
        namespace=namespace,
        new_memory_id=str(winner.memory_id),
        new_memory_key=str(winner.memory_key),
        refined_peers=[_conflict_peer(loser)],
        record_into=into,
        expected_sha256=str(winner.content_sha256 or ""),
        skip_peer=lambda row: str(row.content_sha256 or "") != loser_sha,
        reason=CONFLICT_RESOLVER,
    )
    retired = [{**r, "label": cr.UPDATE} for r in into.get(CONFLICT_RESOLVER, [])]
    if not retired:
        await _done(org, intent)
        return None
    return SimpleNamespace(intent=intent, retired=retired, agent=str(winner.agent))


async def _after_retire(org: str, namespace: str, done: Any) -> None:
    """The retirement's record and follow, outside the keys' locks: neither writes a
    memory row."""
    from server.memory.app.auto_resolution import CONFLICT_RESOLVER, record_auto_supersessions

    if await queue.under_erasure(org):
        return  # the saga deletes the intent with the rest; nothing is written meanwhile
    recorded = await record_auto_supersessions(
        org=org, namespace=namespace, retired=done.retired, reason=CONFLICT_RESOLVER
    )
    pairs = [(str(r["memory_id"]), str(r["superseded_by"])) for r in done.retired]
    if await _follow(org, namespace, done.agent, pairs) and recorded:
        await _done(org, done.intent)


async def _done(org: str, intent: str) -> None:
    """The follow intent is no longer owed; left active, the repair replays it."""
    from server.memory.app.projection_failures import mark_projection_failures_resolved

    try:
        await mark_projection_failures_resolved(org=org, failure_ids=[intent])
    except Exception as e:  # noqa: BLE001 — a replay of a followed pair changes nothing
        log.warning("conflict_resolver: follow intent left for the repair: %s", e)


async def _follow(org: str, namespace: str, agent: str, pairs: list[tuple[str, str]]) -> bool:
    """The triple plane follows the retirement; True when every pair was followed.
    A failure is left to the repair, which holds the pairs as the follow intent."""
    from server.core.ch import get_client
    from server.memory.app.entity_projections import follow_row_supersessions

    scope = SimpleNamespace(org=org, namespace=namespace, agent=agent)
    try:
        left = (await follow_row_supersessions(scope, pairs, ch=get_client())).unfollowed
    except Exception as e:  # noqa: BLE001 — the rows are retired; the repair follows them later
        log.warning("conflict_resolver: triples follow failed (left for the repair): %s", e)
        return False
    return not left


async def _flag(org: str, row: Any, peer: Any) -> bool:
    from server.memory.app.conflicts import mark_peers_conflicted

    if (
        row.status == "conflicted"
        and peer.status == "conflicted"
        and str(peer.memory_id) in row.conflict_with
        and str(row.memory_id) in peer.conflict_with
    ):
        # Already on the review queue together. The peer keeps its own mark.
        return False
    landed = await mark_peers_conflicted(
        org=org,
        new_memory_id=str(row.memory_id),
        peer_ids=[str(peer.memory_id)],
        expected_sha256=str(row.content_sha256 or ""),
        peer_sha256={str(peer.memory_id): str(peer.content_sha256 or "")},
    )
    if landed != "conflict":
        return False
    # The pair is judged: the peer's flag is not an unjudged one to re-check, the other
    # way round, next pass. A peer still owed its own turn keeps its mark.
    await queue.stamp_judged(_queue_key(org, peer), keep_owed=True)
    return True


def _queue_key(org: str, row: Any) -> dict[str, Any]:
    """A memory row of `org` as the queue row `stamp_judged` takes."""
    cols = ("namespace", "agent", "user_id", "session_id", "memory_key", "version")
    return (
        {"org": org}
        | {c: getattr(row, c) for c in cols}
        | {"content_sha256": str(row.content_sha256 or "")}
    )


async def _unlink(org: str, row: Any, other_id: str) -> bool:
    """Remove `other_id` from `row`'s flags; a row left with none is active again."""
    from server.memory.app.conflicts import judged_write

    sha = str(row.content_sha256 or "")
    changed, version = judged_write(sha)

    def update(r: Any) -> dict[str, Any]:
        rest = [x for x in r.conflict_with if str(x) != other_id]
        cols: dict[str, Any] = {"conflict_with": rest, **version(r)}
        if r.status == "conflicted" and not rest:
            cols["status"] = "active"
        return cols

    def skip(r: Any) -> bool:
        return (
            r.pinned
            or other_id not in [str(x) for x in r.conflict_with]
            or (changed is not None and changed(r))
        )

    written: list[dict[str, Any]] = []
    await memory_repo.write_versions_by_ids(
        org,
        [str(row.memory_id)],
        update,
        skip=skip,
        strong=True,
        on_written=written.extend,
        judged=True,
    )
    # The write mirrors only a hiding status; search penalises on the vector row's.
    active = [r for r in written if r.get("status") == "active"]
    if active:
        await memory_repo.mirror_status_to_embeddings(active, judged=True)
    return bool(written)


async def _clear(org: str, row: Any, peer: Any) -> bool:
    a = await _unlink(org, row, str(peer.memory_id))
    b = await _unlink(org, peer, str(row.memory_id))
    return a or b


async def _act(action: str, org: str, namespace: str, row: Any, peer: Any) -> Any:
    if action == cr.RETIRE:
        return await _retire(org, namespace, row, peer)
    if action == cr.FLAG:
        return await _flag(org, row, peer)
    return await _clear(org, row, peer)


_COUNTED = {cr.RETIRE: "retired", cr.FLAG: "flagged", cr.CLEAR: "cleared"}


def _identity(r: Any) -> tuple[str, str, str, str]:
    return (
        str(_field(r, "agent")),
        str(_field(r, "user_id")),
        str(_field(r, "session_id")),
        str(_field(r, "memory_key")),
    )


@asynccontextmanager
async def _pair_locks(org: str, namespace: str, row: Any, peer: Any) -> AsyncIterator[None]:
    """Both rows' key write locks, taken in lock-name order: every resolver write to a
    row is made under its key's lock, from its re-read to its write, as a conditional
    revision holds it from its read to its insert. A writer takes one key, and two
    settles of a pair take the two in the same order, so none waits on another."""
    from server.memory.store.key_write_lock import key_write_locks, lock_name

    keys = sorted({_identity(row), _identity(peer)}, key=lambda k: lock_name(org, namespace, *k))
    async with AsyncExitStack() as stack:
        for k in keys:
            await stack.enter_async_context(key_write_locks.hold(org, namespace, *k))
        yield


async def _settle_pair(
    org: str,
    namespace: str,
    row: Any,
    peer: Mapping[str, Any],
    action: str,
    order: Mapping[str, tuple[int, int]],
    counts: Counts,
) -> dict[str, Any] | None:
    """Act on one pair after re-reading both rows; what was done ({"action": ...}, for
    the audit row) when something changed, else None. A key another write holds past
    the lock's wait leaves the pair alone: that write's own mark brings it back."""
    from server.core.errors import PreconditionFailedError

    try:
        async with _pair_locks(org, namespace, row, peer):
            taken, retired = await _settle_locked_pair(
                org, namespace, row, peer, action, order, counts
            )
    except PreconditionFailedError:
        counts.skipped_changed += 1
        return None
    if retired is not None:
        await _after_retire(org, namespace, retired)
    return taken


async def _settle_locked_pair(
    org: str,
    namespace: str,
    judged: Any,
    peer: Mapping[str, Any],
    action: str,
    order: Mapping[str, tuple[int, int]],
    counts: Counts,
) -> tuple[dict[str, Any] | None, Any]:
    row, head = await _head(org, str(judged.memory_id)), await _head(org, str(peer["memory_id"]))
    if not (
        _holds(row, namespace, str(judged.content_sha256 or ""))
        and _holds(head, namespace, str(peer["content_sha256"]))
    ):
        counts.skipped_changed += 1
        return None, None
    if row.pinned or head.pinned:
        counts.skipped_pinned += 1
        return None, None
    # Right before the write: an erasure requested meanwhile deletes what this writes.
    if await queue.under_erasure(org):
        return None, None
    if action == cr.CLEAR and not (
        str(head.memory_id) in row.conflict_with or str(row.memory_id) in head.conflict_with
    ):
        return None, None  # nothing flagged between them: nothing to clear
    if action == cr.RETIRE and not order[str(head.memory_id)] < order[str(row.memory_id)]:
        # "update" says the judged row is newer; the order does not show it. A person
        # decides.
        action = cr.FLAG
    done = await _act(action, org, namespace, row, head)
    if not done:
        return None, None
    setattr(counts, _COUNTED[action], getattr(counts, _COUNTED[action]) + 1)
    taken: dict[str, Any] = {"action": action}
    if action == cr.RETIRE and str(head.memory_id) in row.conflict_with:
        # The pair is settled: the winner's flag against the row it retired goes too,
        # or it stays on the review queue, stamped and never re-checked.
        taken["flag_cleared"] = await _unlink(org, row, str(head.memory_id))
    return taken, (done if action == cr.RETIRE else None)


async def _audit(
    org: str, namespace: str, row: Any, acted: list[dict[str, Any]], model: str
) -> None:
    """One hash-only audit row per judged row that changed memory: ids, hashes and
    the verdicts, never the text."""
    from server.observability import audit

    await audit.emit(
        AUDIT_EVENT,
        org=org,
        namespace=namespace,
        actor="process:loom",
        subject={
            "memory_id": str(row.memory_id),
            "content_sha256": str(row.content_sha256 or ""),
            "model": model,
            "pairs": acted,
        },
    )


async def _current(q: Mapping[str, Any]) -> Any:
    """The queue row's memory row, read strongly by its full identity."""
    key = (
        str(q["org"]),
        str(q["namespace"]),
        str(q["agent"]),
        str(q["user_id"]),
        str(q["session_id"]),
        str(q["memory_key"]),
    )
    rows = await memory_repo.memory_rows_by_identity_keys([key], sequential=True)
    return rows.get(key)


async def resolve_row(q: Mapping[str, Any], *, policy: str, counts: Counts) -> None:
    """Judge one queue row and act on its pairs, then stamp it judged. A row whose
    head is no longer the queued write is skipped unstamped: its new write carries
    its own mark. A provider failure raises, and the row stays owed."""
    org, namespace, sha = str(q["org"]), str(q["namespace"]), str(q.get("content_sha256") or "")
    row = await _current(q)
    if row is not None and sha and str(row.content_sha256 or "") != sha:
        counts.skipped_changed += 1
        return
    if not _eligible(row) or policy == "append_only":
        await queue.stamp_judged(q)
        return
    if row.pinned:
        counts.skipped_pinned += 1
        await queue.stamp_judged(q)
        return
    peers = await queue.cluster_peers(
        q,
        floor=float(settings.memory_conflict_resolver_min_similarity),
        limit=int(settings.memory_conflict_resolver_max_peers),
        prefer=frozenset(str(x) for x in row.conflict_with),
    )
    if peers:
        row, peers = await _still_unprotected(org, row, peers)
    if row is not None and peers:
        await _judge_and_act(org, namespace, row, peers, policy=policy, counts=counts)
    await queue.stamp_judged(q)  # under erasure it writes nothing: the row stays owed


async def _still_unprotected(
    org: str, row: Any, peers: list[dict[str, Any]]
) -> tuple[Any, list[dict[str, Any]]]:
    """The row and its peers re-read right before the prompt is built: a row relabelled
    'high' since it was read (same bytes, same hash) is never sent. (None, []) when the
    judged row is no longer eligible; a protected peer is dropped."""
    fresh = await _head(org, str(row.memory_id))
    if not _holds(fresh, str(row.namespace), str(row.content_sha256 or "")):
        return None, []
    high = await queue.protected_ids(org, str(row.namespace), [str(p["memory_id"]) for p in peers])
    return fresh, [p for p in peers if str(p["memory_id"]) not in high]


def _action(v: cr.PeerVerdict, policy: str) -> str:
    return cr.action_for(
        v,
        policy=policy,
        retire_at=float(settings.memory_conflict_resolver_retire_confidence),
        clear_at=float(settings.memory_conflict_resolver_clear_confidence),
    )


async def _judge_and_act(
    org: str,
    namespace: str,
    row: Any,
    peers: list[dict[str, Any]],
    *,
    policy: str,
    counts: Counts,
) -> None:
    """Judge the row and act on the answer. What the acting reads (the assertion
    order) is read before the call, so a failure there costs no call."""
    new = cr.Note(
        str(row.memory_id), str(row.memory_key), _plain(memory_repo.stored_value_text(row))
    )
    notes = [
        cr.Note(str(p["memory_id"]), str(p["memory_key"]), _plain(str(p["value"])), p["similarity"])
        for p in peers
    ]
    order = await _assertion_order(org, namespace, [row, *peers])
    verdicts, model = await judge(new, notes)
    counts.judged += 1
    if not verdicts:
        counts.errored += 1  # an answer with nothing usable in it: nothing changes
        return
    acting = asyncio.ensure_future(
        _act_on_verdicts(org, namespace, row, peers, verdicts, policy, order, counts, model)
    )
    try:
        await asyncio.shield(acting)
    except asyncio.CancelledError:
        # A cancellation (the pass deadline, a shutdown) lets the pairs finish with
        # their records, follows and audit row, and the pass ends only after them.
        await asyncio.wait([acting])
        raise


async def _act_on_verdicts(
    org: str,
    namespace: str,
    row: Any,
    peers: list[dict[str, Any]],
    verdicts: dict[str, cr.PeerVerdict],
    policy: str,
    order: Mapping[str, tuple[int, int]],
    counts: Counts,
    model: str,
) -> None:
    """Settle each pair the answer acts on, then one audit row for the row, written
    even when a later pair fails, for the pairs done."""
    acted: list[dict[str, Any]] = []
    try:
        await _settle_pairs(org, namespace, row, peers, verdicts, policy, order, counts, acted)
    finally:
        if acted and not await queue.under_erasure(org):
            await _audit(org, namespace, row, acted, model)


async def _settle_pairs(
    org: str,
    namespace: str,
    row: Any,
    peers: list[dict[str, Any]],
    verdicts: dict[str, cr.PeerVerdict],
    policy: str,
    order: Mapping[str, tuple[int, int]],
    counts: Counts,
    acted: list[dict[str, Any]],
) -> None:
    """Settle each pair the answer acts on, appending what was done to `acted`."""
    for peer in peers:
        v = verdicts.get(str(peer["memory_id"]))
        action = cr.NOTHING if v is None else _action(v, policy)
        if v is None or action == cr.NOTHING:
            # Unsure, or a peer the answer left out: the pair stays as it is.
            counts.skipped_low_confidence += 1
            continue
        taken = await _settle_pair(org, namespace, row, peer, action, order, counts)
        if taken is not None:
            acted.append(
                {
                    "peer_id": str(peer["memory_id"]),
                    **taken,
                    "verdict": v.verdict,
                    "confidence": v.confidence,
                }
            )
