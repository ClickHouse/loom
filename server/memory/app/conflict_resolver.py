"""The conflict resolver: one LLM judgement per marked row, against its nearest live
peers, and what it does about each pair.

A write marks its row when a live peer of its memory_type sits at cosine >= the
resolver floor, or when it was flagged `conflicted` (`store.conflict_resolver`).
The leader's worker (`memory.workers.conflict_resolver`) hands each marked row
here. The verdicts are folded by `domain.conflict_resolution.action_for`:

- retire: the peer is superseded behind the judged row through `apply_refines`, only
  when the peer was stated strictly first (else the pair is flagged), with its
  `memory.auto_supersede` record (reason `conflict_resolver`, so `memory.restore`
  undoes it) and the triple plane following the retirement;
- flag: both rows go to the review queue (`conflicted`, `conflict_with`);
- clear: a flag between the pair is removed, both rows stay live;
- nothing: below the confidence floor, a peer the answer did not name, or an answer
  that could not be parsed.

Before it acts on a pair it re-reads both rows strongly and acts only while both
still hold the judged content; a row changed since is left to its new mark. A
pinned row is never retired, flagged or cleared. The row is then stamped judged
at its content hash, so no write is judged twice. Acting again is a no-op: an
already-retired row is not live, an existing flag is left alone, and a removed
flag has nothing left to remove."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any

import server.memory.store as memory_repo
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain import conflict_resolution as cr
from server.memory.domain.conflicts import ConflictPeer
from server.memory.store import conflict_resolver as queue
from server.settings import settings

log = logging.getLogger(__name__)

AUDIT_EVENT = "memory.conflict_resolver"
_LIVE = ("active", "conflicted")


class ModelUnavailable(RuntimeError):
    """The provider answered that the resolver's model does not exist."""


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
    skipped_erasure: int = 0
    skipped_budget: int = 0
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


def _row_dict(row: Any) -> dict[str, Any]:
    """The fields `statement_rank` orders a `MemoryRow` by."""
    return {f: getattr(row, f, None) for f in ("memory_id", "valid_at", "created_at", "updated_at")}


async def judge(new: cr.Note, peers: list[cr.Note]) -> dict[str, cr.PeerVerdict]:
    """One call over the cluster; {peer memory_id: verdict}. Malformed output is {}.
    Raises `ModelUnavailable` when the provider does not know the model, and any
    other provider failure as it came."""
    import httpx

    from server.llm.client import get_llm_client_for_model
    from server.observability.genai import llm_stage

    llm = get_llm_client_for_model(settings.conflict_resolver_model)
    try:
        with llm_stage("conflict_resolver"):
            res = await llm.complete(
                cr.SYSTEM_PROMPT,
                cr.user_prompt(new, peers),
                max_tokens=cr.max_output_tokens(len(peers)),
                response_format=cr.RESPONSE_FORMAT,
            )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ModelUnavailable(str(e).splitlines()[0]) from e
        raise
    return cr.parse_verdicts(str(getattr(res, "text", "") or ""), peers)


async def _head(org: str, memory_id: str) -> Any:
    return await memory_repo.get_by_id(
        memory_id, org=org, scope=WHOLE_NAMESPACE, fresh=True, strong=True
    )


def _holds(row: Any, namespace: str, sha: str) -> bool:
    """Whether a re-read row is still the live write that was judged."""
    return (
        row is not None
        and str(row.namespace) == namespace
        and str(row.status) in _LIVE
        and str(row.content_sha256 or "") == sha
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


def _stated_at(r: Any) -> tuple[Any, ...]:
    """When a row (a `MemoryRow` or a peer dict) was stated, by valid and write time.
    The rank's last tie-break, the memory_id, is arbitrary and is left out."""
    from server.memory.app.key_supersession import statement_rank

    return statement_rank(r if isinstance(r, Mapping) else _row_dict(r))[:2]


def _stated_before(a: Any, b: Any) -> bool:
    return _stated_at(a) < _stated_at(b)


async def _retire(org: str, namespace: str, row: Any, peer: Any) -> bool:
    """Supersede `peer` behind the judged row `row`, recorded and followed. The
    caller has checked that `peer` was stated first."""
    from server.memory.app.auto_resolution import CONFLICT_RESOLVER, record_auto_supersessions
    from server.memory.app.conflicts import apply_refines

    loser, winner = peer, row
    loser_sha = str(loser.content_sha256 or "")
    into: dict[str, list[dict[str, Any]]] = {}
    await apply_refines(
        org=org,
        namespace=namespace,
        new_memory_id=str(winner.memory_id),
        new_memory_key=str(winner.memory_key),
        refined_peers=[_conflict_peer(loser)],
        run_id=str(getattr(winner, "run_id", "-") or "-"),
        record_into=into,
        expected_sha256=str(winner.content_sha256 or ""),
        skip_peer=lambda row: str(row.content_sha256 or "") != loser_sha,
        reason=CONFLICT_RESOLVER,
    )
    retired = [{**r, "label": cr.UPDATE} for r in into.get(CONFLICT_RESOLVER, [])]
    if not retired:
        return False
    await record_auto_supersessions(
        org=org, namespace=namespace, retired=retired, reason=CONFLICT_RESOLVER
    )
    pairs = [(str(r["memory_id"]), str(r["superseded_by"])) for r in retired]
    await _follow(org, namespace, str(winner.agent), pairs)
    return True


async def _follow(org: str, namespace: str, agent: str, pairs: list[tuple[str, str]]) -> None:
    """The triple plane follows the retirement; a failure is left for the repair."""
    from server.core.ch import get_client
    from server.memory.app.entity_projections import follow_row_supersessions
    from server.memory.app.projection_errors import record_input_projection_failure
    from server.memory.app.projection_failures import FOLLOW_ROWS

    scope = SimpleNamespace(org=org, namespace=namespace, agent=agent)
    try:
        left = (await follow_row_supersessions(scope, pairs, ch=get_client())).unfollowed
    except Exception as e:  # noqa: BLE001 — the rows are retired; the repair follows them later
        log.warning("conflict_resolver: triples follow failed (recorded for repair): %s", e)
        left = pairs
    if left:
        await record_input_projection_failure(
            scope,
            projection=FOLLOW_ROWS,
            error=f"supersession pairs left for the repair to follow: {len(left)}",
            payload={"pairs": [list(p) for p in left]},
            source_operation="worker.conflict_resolver",
            source_resource_id=f"conflict_resolver:{pairs[0][1]}",
        )


async def _flag(org: str, row: Any, peer: Any) -> bool:
    from server.memory.app.conflicts import mark_peers_conflicted

    if (
        row.status == "conflicted"
        and peer.status == "conflicted"
        and str(peer.memory_id) in row.conflict_with
        and str(row.memory_id) in peer.conflict_with
    ):
        return False  # already on the review queue together
    landed = await mark_peers_conflicted(
        org=org,
        new_memory_id=str(row.memory_id),
        peer_ids=[str(peer.memory_id)],
        expected_sha256=str(row.content_sha256 or ""),
    )
    return landed == "conflict"


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


async def _act(action: str, org: str, namespace: str, row: Any, peer: Any) -> bool:
    if action == cr.RETIRE:
        return await _retire(org, namespace, row, peer)
    if action == cr.FLAG:
        return await _flag(org, row, peer)
    return await _clear(org, row, peer)


_COUNTED = {cr.RETIRE: "retired", cr.FLAG: "flagged", cr.CLEAR: "cleared"}


async def _settle_pair(
    org: str,
    namespace: str,
    judged: tuple[str, str],
    peer: Mapping[str, Any],
    action: str,
    counts: Counts,
) -> str | None:
    """Act on one pair after re-reading both rows; the action taken when something
    changed, else None."""
    row_id, row_sha = judged
    row, head = await _head(org, row_id), await _head(org, str(peer["memory_id"]))
    if not (
        _holds(row, namespace, row_sha) and _holds(head, namespace, str(peer["content_sha256"]))
    ):
        counts.skipped_changed += 1
        return None
    if row.pinned or head.pinned:
        counts.skipped_pinned += 1
        return None
    if action == cr.CLEAR and not (
        str(head.memory_id) in row.conflict_with or str(row.memory_id) in head.conflict_with
    ):
        return None  # nothing flagged between them: nothing to clear
    if action == cr.RETIRE and not _stated_before(head, row):
        # "update" says the judged row is newer; the times do not show it. A person decides.
        action = cr.FLAG
    if not await _act(action, org, namespace, row, head):
        return None
    setattr(counts, _COUNTED[action], getattr(counts, _COUNTED[action]) + 1)
    return action


async def _audit(org: str, namespace: str, row: Any, acted: list[dict[str, Any]]) -> None:
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
            "model": settings.conflict_resolver_model,
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
    q: Mapping[str, Any], *, policy: str, counts: Counts, acting: asyncio.Lock | None = None
) -> None:
    """Judge one queue row and act on its pairs, then stamp it judged. A row whose
    head is no longer the queued write is skipped unstamped: its new write carries
    its own mark. A provider failure raises, and the row stays owed.

    Rows judged at the same time share `acting`: it is held from the re-read of a
    pair to its write, so two rows that judged the same peer act on it once."""
    org, namespace, sha = str(q["org"]), str(q["namespace"]), str(q.get("content_sha256") or "")
    row = await _current(q)
    if row is not None and sha and str(row.content_sha256 or "") != sha:
        counts.skipped_changed += 1
        return
    if row is None or row.status not in _LIVE or policy == "append_only":
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
        row, peers = await _newest_as_new(org, row, peers)
        await _judge_and_act(
            org,
            namespace,
            row,
            peers,
            policy=policy,
            counts=counts,
            acting=acting or asyncio.Lock(),
        )
    await queue.stamp_judged(q)


async def _newest_as_new(
    org: str, row: Any, peers: list[dict[str, Any]]
) -> tuple[Any, list[dict[str, Any]]]:
    """The judge reads NEW as the later statement. A re-checked row is often the older
    side of its flag, so when a flagged peer was stated later, the latest such peer
    is NEW and the row is judged as one of its peers."""
    flagged = {str(x) for x in row.conflict_with}
    later = [p for p in peers if str(p["memory_id"]) in flagged and _stated_before(row, p)]
    if not later:
        return row, peers
    top = max(later, key=_stated_at)
    new = await _head(org, str(top["memory_id"]))
    if not _holds(new, str(row.namespace), str(top["content_sha256"])):
        return row, peers  # changed since the cluster read: judged as queued
    as_peer = {
        "memory_id": str(row.memory_id),
        "memory_key": str(row.memory_key),
        "value": memory_repo.stored_value_text(row),
        "content_sha256": str(row.content_sha256 or ""),
        "similarity": top["similarity"],
    }
    return new, [as_peer, *(p for p in peers if p is not top)]


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
    acting: asyncio.Lock,
) -> None:
    new = cr.Note(
        str(row.memory_id), str(row.memory_key), _plain(memory_repo.stored_value_text(row))
    )
    notes = [
        cr.Note(str(p["memory_id"]), str(p["memory_key"]), _plain(str(p["value"])), p["similarity"])
        for p in peers
    ]
    verdicts = await judge(new, notes)
    counts.judged += 1
    if not verdicts:
        counts.errored += 1  # an answer with nothing usable in it: nothing changes
        return
    acted: list[dict[str, Any]] = []
    judged = (str(row.memory_id), str(row.content_sha256 or ""))
    async with acting:
        for peer in peers:
            v = verdicts.get(str(peer["memory_id"]))
            action = cr.NOTHING if v is None else _action(v, policy)
            if v is None or action == cr.NOTHING:
                # Unsure, or a peer the answer left out: the pair stays as it is.
                counts.skipped_low_confidence += 1
                continue
            taken = await _settle_pair(org, namespace, judged, peer, action, counts)
            if taken is not None:
                acted.append(
                    {
                        "peer_id": str(peer["memory_id"]),
                        "action": taken,
                        "verdict": v.verdict,
                        "confidence": v.confidence,
                    }
                )
        if acted:
            await _audit(org, namespace, row, acted)
