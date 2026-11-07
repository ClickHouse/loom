"""The conflict resolver: one LLM judgement per marked row against its nearest live
peers, and the action on each pair (`domain.conflict_resolution.action_for`). Its
invariants are in docs/architecture/memory-architecture.md (the conflict resolver)."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, NamedTuple

import httpx

import server.memory.store as memory_repo
from server.core.datetimes import naive_utc_now, to_naive_utc
from server.core.errors import ModelCallRefused, PreconditionFailedError
from server.core.leases import LeaseUnavailable
from server.core.naming import is_anonymous_user, same_session
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.llm import customer
from server.llm.fake import FakeLLMClient
from server.memory.app import intent_marks
from server.memory.app.projection_errors import ProjectionScope
from server.memory.domain import conflict_resolution as cr
from server.memory.domain.conflicts import ConflictPeer
from server.memory.domain.sensitivity import reads_high, value_reads_high
from server.memory.domain.trust import subject_of
from server.memory.store import conflict_resolver as queue
from server.observability.genai import llm_stage
from server.settings import settings

log = logging.getLogger(__name__)

AUDIT_EVENT = "memory.conflict_resolver"
# The judge's thresholds were measured at this reasoning effort (OpenAI models).
REASONING_EFFORT = "low"
_LIVE = ("active", "conflicted")


class ModelUnavailable(RuntimeError):
    """The provider refused the resolver's model or key (401, 403, 404): the same
    answer for every row, so the pass stops and the rows stay owed."""


class Unanswered(RuntimeError):
    """Two answers with nothing usable (cut at the output cap, or malformed): asking
    again gets the same, so the row is stamped judged and counted errored."""


# The stop reasons of an answer cut at the output cap (OpenAI, Anthropic).
_CUT = ("length", "max_tokens")


class JudgedThenFailed(RuntimeError):
    """A failure after the judge answered (its cause the error): the call was paid, so
    the worker counts it as an attempt."""


class PeerBehind(RuntimeError):
    """A flagged peer's change is held by the Postgres authority but not yet by ClickHouse:
    the row waits, with no call and no attempt, and is judged with all its peers once
    ClickHouse holds it."""


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
    skipped_no_action: int = 0
    skipped_changed: int = 0
    skipped_pinned: int = 0
    skipped_budget: int = 0
    # Rows held back while a flagged peer's change is not yet in ClickHouse.
    skipped_behind: int = 0
    # Namespaces left out of the pass's queue read: over their daily cost cap.
    capped_namespaces: int = 0
    errored: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def add(self, other: Counts) -> None:
        """Add `other`'s counts to these: a row's own, into its pass's."""
        for name, value in asdict(other).items():
            setattr(self, name, getattr(self, name) + value)


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
    if isinstance(_client(), FakeLLMClient):
        return f"no key for {settings.conflict_resolver_model}"
    return None


async def judge(new: cr.Note, peers: list[cr.Note]) -> tuple[dict[str, cr.PeerVerdict], str]:
    """One call over the cluster: ({peer memory_id: verdict}, the model that answered).
    Never answered by the fallback model: the thresholds are this model's. Raises
    `ModelUnavailable` when the provider refuses the model or the key, `Unjudgeable`
    when it refuses this request, `Unanswered` when the answer has nothing usable even
    with twice the room, and any other failure as it came."""
    llm = _client()
    if isinstance(llm, FakeLLMClient):
        # The stand-in for a missing key: its echo is no judgement. The rows stay owed.
        raise ModelUnavailable(f"no key for {settings.conflict_resolver_model}")
    cap = cr.max_output_tokens(len(peers))

    async def ask(max_tokens: int) -> tuple[Any, dict[str, cr.PeerVerdict]]:
        res = await llm.complete(
            cr.SYSTEM_PROMPT,
            cr.user_prompt(new, peers),
            max_tokens=max_tokens,
            response_format=cr.RESPONSE_FORMAT,
        )
        if str(getattr(res, "stop_reason", "") or "") in _CUT:
            return res, {}  # a cut JSON answer parses to nothing
        return res, cr.parse_verdicts(str(getattr(res, "text", "") or ""), peers)

    try:
        with llm_stage("conflict_resolver"):
            res, verdicts = await ask(cap)
            if len(verdicts) < len(peers):
                # Cut at the cap, malformed, or a peer left out: once more, with room.
                try:
                    again, more = await ask(2 * cap)
                except (httpx.HTTPError, TimeoutError, ModelCallRefused):
                    if not verdicts:
                        raise
                    again, more = res, {}  # the first answer stands; what it left out waits
                if len(more) > len(verdicts):
                    res, verdicts = again, more
    except httpx.HTTPStatusError as e:
        status, reason = e.response.status_code, str(e).splitlines()[0]
        if status in (401, 403, 404):
            raise ModelUnavailable(reason) from e
        if 400 <= status < 500 and status not in (408, 409, 429):
            raise Unjudgeable(reason) from e
        raise  # 408, 409, 429, 5xx: transient, the row stays owed
    if not verdicts:
        raise Unanswered(f"nothing usable in two answers (output cap {2 * cap} tokens)")
    return verdicts, str(getattr(res, "model", "") or settings.conflict_resolver_model)


async def _head(org: str, namespace: str, memory_id: str) -> Any:
    return await memory_repo.get_by_id(
        memory_id, org=org, namespace=namespace, scope=WHOLE_NAMESPACE, fresh=True, strong=True
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


class AssertionOrder(NamedTuple):
    """`_assertion_order`'s answer: {memory_id: (when its statement holds, when its value
    was first asserted)}, in wall-clock ns, and the rows' Undos."""

    when: dict[str, tuple[int, int]]
    undos: list[queue.Undo]


async def _assertion_order(org: str, namespace: str, rows: list[Any]) -> AssertionOrder:
    """The `AssertionOrder` of rows (`MemoryRow`s or peer dicts). Finer than the
    second, and a metadata write (touch, pin, tags) keeps the hash, so it does not move a
    row. An explicit valid_at earlier than the assertion moves it back: the fact held
    then."""
    keys = [(str(_field(r, "memory_id")), str(_field(r, "content_sha256") or "")) for r in rows]
    found = await queue.asserted_versions(org, namespace, keys)
    out: dict[str, tuple[int, int]] = {}
    for r in rows:
        mid = str(_field(r, "memory_id"))
        # A row the history does not hold: its current version.
        asserted = found.asserted.get(mid, int(_field(r, "version") or 0))
        holds = asserted
        valid = _field(r, "valid_at")
        if isinstance(valid, datetime):
            holds = min(holds, calendar.timegm(valid.utctimetuple()) * 1_000_000_000)
        out[mid] = (holds, asserted)
    return AssertionOrder(out, found.undos)


@dataclass
class Retired:
    """A retirement that landed: its follow intent, its record's entries, its agent."""

    intent: str
    retired: list[dict[str, Any]]
    agent: str


async def _retire(
    org: str, namespace: str, row: Any, peer: Any, *, why: Mapping[str, str]
) -> Retired | None:
    """Supersede `peer` (stated first) behind the judged row `row`; what `_after_retire`
    records and follows, or None. The record and the follow are owed to the repair
    before the supersede commits (FOLLOW_OWED, acted on only while the loser is retired
    behind the winner) and resolved once both land. No intent, no retirement. `why`: the
    judge's verdict, confidence, model and prompt version, kept on each retired entry for
    the audit record."""
    from server.memory.app.auto_resolution import CONFLICT_RESOLVER, retired_entry
    from server.memory.app.conflicts import apply_refines
    from server.memory.app.projection_failures import FOLLOW_OWED, record_projection_failure

    loser, winner = peer, row
    loser_sha = str(loser.content_sha256 or "")
    pair = (str(loser.memory_id), str(winner.memory_id))
    entry = retired_entry(
        loser,
        superseded_by=pair[1],
        superseded_by_key=str(winner.memory_key),
        label=cr.UPDATE,
        **why,
    )
    intent = await record_projection_failure(
        org=org,
        namespace=namespace,
        agent=str(winner.agent),
        projection=FOLLOW_OWED,
        source_operation=f"worker.{CONFLICT_RESOLVER}",
        source_resource_id=f"{CONFLICT_RESOLVER}:{pair[1]}",
        error="conflict_resolver: retirement to follow",
        payload={"pairs": [list(pair)], "retired": [entry], "reason": CONFLICT_RESOLVER},
        user_id=str(loser.user_id),
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
    retired = [{**r, "label": cr.UPDATE, **why} for r in into.get(CONFLICT_RESOLVER, [])]
    if not retired:
        # Nothing tracked does not mean nothing landed (a lost response): the head says.
        head = await _head(org, namespace, pair[0])
        if head is None or (head.status, str(head.superseded_by)) != ("superseded", pair[1]):
            if _holds(head, namespace, loser_sha):
                intent_marks.resolve_soon(org, [intent])  # still live as judged: none landed
            return None  # otherwise the repair decides
        retired = [entry]
    return Retired(intent=intent, retired=retired, agent=str(winner.agent))


async def _after_retire(org: str, namespace: str, done: Retired) -> None:
    """The retirement's record and follow, outside the keys' locks: neither writes a
    memory row."""
    from server.memory.app.auto_resolution import (
        CONFLICT_RESOLVER,
        follow_pairs,
        record_auto_supersessions,
    )

    try:
        erasing = await queue.under_erasure(org)
    except Exception as e:  # noqa: BLE001 — the intent stays for the repair
        log.warning("conflict_resolver: erasure check failed, follow left for the repair: %s", e)
        return
    if erasing:
        return  # the intent stays; the repair replays it once the erasure is over
    recorded = await record_auto_supersessions(  # the intent's id: the repair finds it
        org=org,
        namespace=namespace,
        retired=done.retired,
        reason=CONFLICT_RESOLVER,
        request_id=done.intent,
    )
    pairs = [(str(r["memory_id"]), str(r["superseded_by"])) for r in done.retired]
    # The retired row's user goes on any failure the follow records; what it leaves is
    # the repair's, which holds the pairs as the follow intent.
    user_id = str(done.retired[0].get("user_id") or "") if done.retired else ""
    scope = ProjectionScope(org=org, namespace=namespace, agent=done.agent, user_id=user_id)
    if not await follow_pairs(scope, pairs, "conflict_resolver") and recorded:
        # No longer owed: resolved in the next batched mark with every other writer's.
        intent_marks.resolve_soon(org, [done.intent])


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
        namespace=row.namespace,
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


async def _unlink(org: str, row: Any, *other_ids: str) -> bool:
    """Remove `other_ids` from `row`'s flags; a row left with none is active again."""
    from server.memory.app.conflicts import judged_write

    sha = str(row.content_sha256 or "")
    changed, version = judged_write(sha)
    gone = set(other_ids)

    def update(r: Any) -> dict[str, Any]:
        rest = [x for x in r.conflict_with if str(x) not in gone]
        cols: dict[str, Any] = {"conflict_with": rest, **version(r)}
        if r.status == "conflicted" and not rest:
            cols["status"] = "active"
        return cols

    def skip(r: Any) -> bool:
        return (
            r.pinned
            or not gone & {str(x) for x in r.conflict_with}
            or (changed is not None and changed(r))
        )

    written: list[dict[str, Any]] = []
    await memory_repo.write_versions_by_ids(
        org,
        [str(row.memory_id)],
        update,
        namespace=row.namespace,
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


async def clear_retired_flag(org: str, namespace: str, winner_id: str, loser_id: str) -> None:
    """The winner's flag against the row it retired, for the replay of a retirement
    whose clear failed. Under the winner's key lock, as every resolver write."""
    from server.memory.store.key_write_lock import key_write_locks

    winner = await _head(org, namespace, winner_id)
    if winner is not None and loser_id in winner.conflict_with:
        async with key_write_locks.hold(org, namespace, *queue.key_identity(winner)):
            await _unlink(org, winner, loser_id)


async def _clear(org: str, row: Any, peer: Any) -> bool:
    a = await _unlink(org, row, str(peer.memory_id))
    b = await _unlink(org, peer, str(row.memory_id))
    return a or b


@asynccontextmanager
async def _pair_locks(org: str, namespace: str, row: Any, peer: Any) -> AsyncIterator[None]:
    """Both rows' key write locks, in lock-name order, so two settles of one pair never
    wait on each other."""
    from server.memory.store.key_write_lock import key_write_locks, lock_name

    keys = sorted(
        {queue.key_identity(row), queue.key_identity(peer)},
        key=lambda k: lock_name(org, namespace, *k),
    )
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
    counts: Counts,
    *,
    why: Mapping[str, str],
) -> dict[str, Any] | None:
    """Act on one pair after re-reading both rows; what was done ({"action": ...}, for
    the audit row) when something changed, else None. A key another write holds past
    the lock's wait raises PreconditionFailedError."""
    async with _pair_locks(org, namespace, row, peer):
        taken, retired = await _settle_locked_pair(
            org, namespace, row, peer, action, counts, why=why
        )
    if retired is not None:
        await _after_retire(org, namespace, retired)
    return taken


async def _settle_locked_pair(
    org: str,
    namespace: str,
    judged: Any,
    peer: Mapping[str, Any],
    action: str,
    counts: Counts,
    *,
    why: Mapping[str, str],
) -> tuple[dict[str, Any] | None, Retired | None]:
    """Act on one pair's verdict as both rows stand under their key locks."""
    # memory.set takes no key lock, so a set can still land on the winner after this
    # re-read: the supersede (`apply_refines` with the judged hash) reads the winner again
    # and retires nothing once it holds another value.
    row, head = await asyncio.gather(
        _head(org, namespace, str(judged.memory_id)),
        _head(org, namespace, str(peer["memory_id"])),
    )
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
    if action in (cr.RETIRE, cr.FLAG):
        # Read under the locks: a valid_at corrected during the call moves the order.
        # If this read fails the row stays unstamped and the next pass judges it again
        # (another LLM call).
        found = await _assertion_order(org, namespace, [row, head])
        if _undone(row, head, found.when, found.undos):
            counts.skipped_no_action += 1
            return None, None  # a person undid a retirement between them: that stands
        if action == cr.RETIRE and cr.same_fact_day(
            _dated_note(row, found.when), _dated_note(head, found.when)
        ):
            action = cr.FLAG  # a date corrected during the call put both on one day
        head_at, row_at = found.when[str(head.memory_id)], found.when[str(row.memory_id)]
        if action == cr.RETIRE and not head_at < row_at:
            action = cr.FLAG  # the order does not show the judged row is newer: a person decides
    retired = None
    if action == cr.RETIRE:
        retired = await _retire(org, namespace, row, head, why=why)
        done = retired is not None
    elif action == cr.FLAG:
        done = await _flag(org, row, head)
    else:
        done = await _clear(org, row, head)
    if not done:
        return None, None
    if action == cr.RETIRE:
        counts.retired += 1
    elif action == cr.FLAG:
        counts.flagged += 1
    else:
        counts.cleared += 1
    taken: dict[str, Any] = {"action": action}
    if action == cr.RETIRE and str(head.memory_id) in row.conflict_with:
        # The pair is settled: the winner's flag against the row it retired goes too,
        # or it stays on the review queue, stamped and never re-checked.
        taken["flag_cleared"] = await _unlink(org, row, str(head.memory_id))
    return taken, retired


def _dated_note(r: Any, when: Mapping[str, tuple[int, int]]) -> cr.Note:
    """`r` as the prompt dates it (its value aside), from the order read under the locks."""
    mid = str(r.memory_id)
    written = cr.written_at(when.get(mid, (0, 0))[0], r.valid_at)
    return cr.Note(mid, str(r.memory_key), "", written=written, dated=r.valid_at is not None)


def _undone(
    row: Any, head: Any, order: Mapping[str, tuple[int, int]], undos: list[queue.Undo]
) -> bool:
    """Whether a person's Undo of a retirement between the two rows (`MemoryRow`s or peer
    dicts) still stands: a person's decision outranks the model's."""
    for loser, winner in ((head, row), (row, head)):
        lost, won = str(_field(loser, "memory_id")), str(_field(winner, "memory_id"))
        undone = (lost, won, str(_field(loser, "content_sha256") or ""))
        if any(
            (u.memory_id, u.superseded_by, u.content_sha256) == undone
            and order[won][1] < u.version
            and order[lost][1] < u.version
            for u in undos
        ):
            return True
    return False


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
            "prompt_version": cr.PROMPT_VERSION,
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


async def resolve_row(
    q: Mapping[str, Any],
    *,
    policy: str,
    counts: Counts,
    shown: frozenset[str] = frozenset(),
) -> frozenset[str] | None:
    """Judge one queue row, act on its pairs and stamp it judged; a provider failure
    raises and the row stays owed. Returns None once stamped (or skipped), else the ids
    it was judged against while it stays owed. `shown`: the peers its earlier attempts
    were judged against, which give their places to the others."""
    org, namespace, sha = str(q["org"]), str(q["namespace"]), str(q.get("content_sha256") or "")
    row = await _current(q)
    if row is not None and sha and str(row.content_sha256 or "") != sha:
        counts.skipped_changed += 1
        return None
    if not _eligible(row) or policy == "append_only":
        await queue.stamp_judged(q)
        return None
    if row.pinned:
        counts.skipped_pinned += 1
        await queue.stamp_judged(q)
        return None
    if row.conflict_with:
        try:
            await _release_dead_flags(org, namespace, row, counts)
        except PreconditionFailedError:
            counts.skipped_changed += 1  # the key is busy: the row stays owed
            return None
    flagged = frozenset(str(x) for x in row.conflict_with)
    cluster = await queue.cluster_peers(
        q,
        floor=float(settings.memory_conflict_resolver_min_similarity),
        limit=int(settings.memory_conflict_resolver_max_peers),
        prefer=flagged,
        shown=shown,
    )
    # A peer the Postgres authority no longer serves at the version read is never sent; an
    # unflagged one is left out of this judgement, a flagged one holds the row back.
    peers = await memory_repo.served_rows(cluster.peers)
    behind = {str(p["memory_id"]) for p in cluster.peers} - {str(p["memory_id"]) for p in peers}
    if behind & flagged:
        raise PeerBehind()
    if peers:
        row, peers = await _still_unprotected(org, row, peers)
    if row is not None and peers:
        unanswered = await _judge_and_act(org, namespace, row, peers, policy=policy, counts=counts)
        judged_ids = frozenset(str(p["memory_id"]) for p in peers)
        if unanswered & {str(x) for x in row.conflict_with} or cluster.flags_left:
            # A flagged peer the answer left out, or one past the cluster's places: stamped,
            # its flag would never be judged.
            return judged_ids
    await queue.stamp_judged(q)  # under erasure it writes nothing: the row stays owed
    return None


async def _release_dead_flags(org: str, namespace: str, row: Any, counts: Counts) -> None:
    """Clear the row's flags against rows that are no longer live (retired, rejected,
    deleted, expired). Such a peer never joins a cluster, so no judgement clears its flag
    and the row would stay on the review queue. Under the row's key lock, with the audit
    row a clear writes."""
    from server.memory.store.key_write_lock import key_write_locks

    dead = await queue.dead_flags(org, namespace, queue.key_identity(row))
    if not dead:
        return
    async with key_write_locks.hold(org, namespace, *queue.key_identity(row)):
        head = await _head(org, namespace, str(row.memory_id))
        if not _holds(head, namespace, str(row.content_sha256 or "")) or head.pinned:
            return
        if await queue.under_erasure(org) or not await _unlink(org, head, *dead):
            return
    counts.cleared += len(dead)
    acted = [{"peer_id": x, "action": cr.CLEAR, "reason": "peer_not_live"} for x in dead]
    if not await queue.under_erasure(org):
        await _audit(org, namespace, head, acted, "")


async def _still_unprotected(
    org: str, row: Any, peers: list[dict[str, Any]]
) -> tuple[Any, list[dict[str, Any]]]:
    """The row and its peers re-read right before the prompt is built: a row relabelled
    'high' since it was read (same bytes, same hash) is never sent, nor one whose value
    or key holds a high shape the sweep has not labelled yet. (None, []) when the
    judged row is no longer eligible or holds one (it is stamped judged, with no model
    call); such a peer is dropped."""
    fresh = await _head(org, str(row.namespace), str(row.memory_id))
    if not _holds(fresh, str(row.namespace), str(row.content_sha256 or "")):
        return None, []
    if _reads_high(fresh.value, fresh.memory_key):
        return None, []
    high = await queue.protected_ids(org, str(row.namespace), [str(p["memory_id"]) for p in peers])
    return fresh, [
        p
        for p in peers
        if str(p["memory_id"]) not in high and not _reads_high(p["value"], p["memory_key"])
    ]


def _reads_high(value: Any, memory_key: Any) -> bool:
    """A high shape in the value or the key: the prompt shows both."""
    return value_reads_high(value) or reads_high(str(memory_key))


def _action(v: cr.PeerVerdict, policy: str, *, cross_session: bool, same_written: bool) -> str:
    return cr.action_for(
        v,
        policy=policy,
        retire_at=float(settings.memory_conflict_resolver_retire_confidence),
        clear_at=float(settings.memory_conflict_resolver_clear_confidence),
        flag_at=float(settings.memory_conflict_resolver_flag_confidence),
        cross_session=cross_session,
        same_written=same_written,
    )


def _cross_session(row: Any, peer: Mapping[str, Any]) -> bool:
    """A peer from another session of a subject with no real user: the anonymous user is
    one subject per agent (the cluster reads only its agent's rows), and an agent's
    sessions are separate tasks."""
    return is_anonymous_user(str(row.user_id)) and not same_session(
        str(peer["session_id"]), str(row.session_id)
    )


def notes_for(
    row: Any, peers: list[dict[str, Any]], holds: Mapping[str, int]
) -> tuple[cr.Note, list[cr.Note]]:
    """The judged row and its peers as the prompt shows them; `holds`: {memory_id: when its
    statement holds, in ns}, the order a retirement checks (`_assertion_order`)."""
    new = cr.Note(
        str(row.memory_id),
        str(row.memory_key),
        _plain(memory_repo.stored_value_text(row)),
        written=cr.written_at(holds.get(str(row.memory_id)), row.valid_at),
        dated=row.valid_at is not None,
    )
    notes = [
        cr.Note(
            str(p["memory_id"]),
            str(p["memory_key"]),
            _plain(str(p["value"])),
            p["similarity"],
            written=cr.written_at(holds.get(str(p["memory_id"])), p.get("valid_at")),
            dated=p.get("valid_at") is not None,
            session=cr.session_relation(
                str(row.agent), str(row.session_id), str(p["agent"]), str(p["session_id"])
            ),
        )
        for p in peers
    ]
    return new, notes


async def _judge_and_act(
    org: str,
    namespace: str,
    row: Any,
    peers: list[dict[str, Any]],
    *,
    policy: str,
    counts: Counts,
) -> set[str]:
    """Judge the row and act on the answer; the ids of the peers it gave no verdict."""
    found = await _assertion_order(org, namespace, [row, *peers])
    # A pair a person's Undo settled skips the judge call; for the others the re-read
    # under the locks decides.
    settled = {str(p["memory_id"]) for p in peers if _undone(row, p, found.when, found.undos)}
    counts.skipped_no_action += len(settled)
    peers = [p for p in peers if str(p["memory_id"]) not in settled]
    if not peers:
        return set()
    new, notes = notes_for(row, peers, {mid: when for mid, (when, _) in found.when.items()})
    with customer.for_org(
        org,
        namespace=namespace,
        subject=subject_of(str(row.actor or "")),
        user_id=str(row.user_id),
    ):
        verdicts, model = await judge(new, notes)
    counts.judged += 1
    acting = asyncio.ensure_future(
        _act_on_verdicts(org, namespace, row, peers, verdicts, policy, counts, model, new, notes)
    )
    try:
        await asyncio.shield(acting)
    except asyncio.CancelledError:
        # A cancellation (the pass deadline, a shutdown) lets the pairs finish with
        # their records, follows and audit row, and the pass ends only after them.
        await asyncio.wait([acting])
        if not acting.cancelled() and acting.exception() is not None:
            # Nobody awaits it after the raise: logged here, or lost.
            log.warning("conflict_resolver: acting on a verdict failed: %r", acting.exception())
        raise
    except LeaseUnavailable:
        raise  # no lease store: the row stays owed and the pass stops
    except Exception as e:  # re-raised: a failure after the paid call is an attempt
        # A busy key included: the row stays owed until its attempts run out.
        raise JudgedThenFailed(f"{type(e).__name__}: {e}") from e
    return {str(p["memory_id"]) for p in peers} - set(verdicts)


async def _act_on_verdicts(
    org: str,
    namespace: str,
    row: Any,
    peers: list[dict[str, Any]],
    verdicts: dict[str, cr.PeerVerdict],
    policy: str,
    counts: Counts,
    model: str,
    new: cr.Note,
    notes: list[cr.Note],
) -> None:
    """Settle each pair the answer acts on, then one audit row for the row, written
    even when a later pair fails, for the pairs done. `new` and `notes`: the row and its
    peers as the prompt showed them."""
    shown = {n.memory_id: n for n in notes}
    acted: list[dict[str, Any]] = []
    try:
        for peer in peers:
            v = verdicts.get(str(peer["memory_id"]))
            cross = _cross_session(row, peer)
            note = shown.get(str(peer["memory_id"]))
            same = note is not None and cr.same_fact_day(new, note)
            action = (
                cr.NOTHING
                if v is None
                else _action(v, policy, cross_session=cross, same_written=same)
            )
            if v is None or action == cr.NOTHING:
                # Unsure, self-contradicting, or a peer the answer left out: the pair
                # stays as it is.
                counts.skipped_no_action += 1
                continue
            why = {
                "judge_verdict": v.verdict,
                "judge_confidence": str(v.confidence),
                "judge_model": model,
                "judge_prompt_version": cr.PROMPT_VERSION,
            }
            taken = await _settle_pair(org, namespace, row, peer, action, counts, why=why)
            if taken is not None:
                acted.append(
                    {"peer_id": str(peer["memory_id"]), **taken}
                    | {"verdict": v.verdict, "confidence": v.confidence}
                )
    except BaseException:
        # The pairs done still get their audit row, and the error raised is the one that
        # stopped them, not one from the audit.
        try:
            await _audit_unless_erasing(org, namespace, row, acted, model)
        except Exception as e:  # noqa: BLE001 — the pairs' own failure is raised below
            log.warning("conflict_resolver: audit after a failed pair not written: %s", e)
        raise
    await _audit_unless_erasing(org, namespace, row, acted, model)


async def _audit_unless_erasing(
    org: str, namespace: str, row: Any, acted: list[dict[str, Any]], model: str
) -> None:
    if acted and not await queue.under_erasure(org):
        await _audit(org, namespace, row, acted, model)
