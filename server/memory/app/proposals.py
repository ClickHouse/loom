"""Fact proposals: written for trial, decided on graded outcomes.

A proposal is a `loom.memory` row with `status='proposed'`. The served set
leaves it out (`temporal_sql.SERVED_STATUSES`); a read that passes
`include_proposed` gets it, and a run that relies on it names it in
`run.complete(memories_used=…)`. When a run is graded (`run.grade`), every
proposal among its `memories_used` gains a pass or a fail. The counts are
derived, never stored: `graded_counts` reads them from `loom.agent_runs`,
where a regrade replaces the run's verdict, so a count can never drift from
the grades it was made of.

Loom never decides acceptance on its own: a person
or an agent decides with `memory.proposal_decide`, unless the namespace owner
set `proposal_policy.mode = "auto"`. Then `apply_policy` accepts a proposal
at `min_passes` passes with at most `max_fails` fails, and rejects it at
`reject_after_fails` fails (0 = never). A decision is a new version of the
row (`build_memory_version_row`) mirrored onto `memory_embeddings`: no
lightweight UPDATE, so the vector table gets no patch part.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from server.core.hashing import sha256_value
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import webhooks
from server.memory.domain.proposal_policy import ProposalPolicy, parse_policy
from server.memory.store.inserts import insert_memory_versions
from server.settings import settings

log = logging.getLogger(__name__)

PROPOSED = "proposed"
Decision = Literal["accept", "reject"]


_POLICY_CACHE: dict[tuple[str, str], tuple[float, ProposalPolicy]] = {}


async def policy_for(
    org: str, namespace: str, *, ch: Any = None, fresh: bool = False
) -> ProposalPolicy:
    """The namespace's policy, cached a minute per process (`fresh` skips the
    cache: a decision path reads the owner's current rule)."""
    key = (org, namespace)
    hit = _POLICY_CACHE.get(key)
    now = time.monotonic()
    if hit is not None and not fresh and now - hit[0] < settings.memory_proposals_policy_ttl_s:
        return hit[1]
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    try:
        row = await ch.query_one(
            """
            SELECT proposal_policy FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace},
        )
    except Exception as e:  # noqa: BLE001 — unreadable reads as manual
        log.warning("proposals: policy read failed for %s/%s: %s", org, namespace, e)
        return ProposalPolicy()
    policy = parse_policy((row or {}).get("proposal_policy"))
    _POLICY_CACHE[key] = (now, policy)
    return policy


def invalidate_policy(org: str, namespace: str) -> None:
    _POLICY_CACHE.pop((org, namespace), None)


@dataclass(frozen=True)
class GradedCounts:
    passes: int = 0
    fails: int = 0
    last_graded_at: str = ""


_GRADED_COUNTS_SQL = """
    SELECT key,
           countIf(grade = 'pass') AS passes,
           countIf(grade = 'fail') AS fails,
           toString(max(graded_at)) AS last_graded_at
    FROM loom.agent_runs FINAL
    ARRAY JOIN memories_used AS key
    WHERE org = {org:String} AND namespace = {ns:String}
      AND grade IN ('pass', 'fail')
      AND key IN {keys:Array(String)}
    GROUP BY key
"""


async def graded_counts(
    ch: Any, org: str, namespace: str, keys: list[str]
) -> dict[str, GradedCounts]:
    """Graded runs that relied on each key: `memories_used` of every run with
    a pass / fail grade. One statement; a run counts once per key."""
    wanted = sorted({k for k in keys if k})
    if not wanted:
        return {}
    rows = await ch.query_all(
        _GRADED_COUNTS_SQL, params={"org": org, "ns": namespace, "keys": wanted}
    )
    return {
        str(r["key"]): GradedCounts(
            passes=int(r.get("passes") or 0),
            fails=int(r.get("fails") or 0),
            last_graded_at=str(r.get("last_graded_at") or ""),
        )
        for r in rows
    }


def policy_verdict(policy: ProposalPolicy, counts: GradedCounts) -> Decision | None:
    """What the policy's thresholds say about one proposal (whatever the
    mode — `memory.proposals` shows it as `ready`). Rejection is checked
    first: a proposal that crossed the fail limit is never accepted."""
    if policy.reject_after_fails and counts.fails >= policy.reject_after_fails:
        return "reject"
    if counts.passes >= policy.min_passes and counts.fails <= policy.max_fails:
        return "accept"
    return None


@dataclass
class DecideResult:
    memory_key: str
    decided: bool
    status: str  # the row's status after the call ('' when not found)
    reason: str = ""  # why nothing changed


async def decide(
    org: str,
    namespace: str,
    keys: list[str],
    decision: Decision,
    *,
    actor: str,
    reason: str = "",
    ch: Any = None,
) -> list[DecideResult]:
    """Accept (→ `active`) or reject (→ `rejected`) the proposals under `keys`.

    Only rows still `proposed` change; anything else is reported as it is.
    Each decided row is a new version carrying every column
    (`build_memory_version_row` over a whole-row read),
    mirrored onto `memory_embeddings` in the same call, with one audit row
    per decision. An accepted proposal that recorded a contradiction while on
    trial enters the ordinary conflict flow now (`mark_peers_conflicted`)."""
    import server.memory.store as memory_repo

    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    wanted = list(dict.fromkeys(k for k in keys if k))
    ident = memory_repo.Identity(
        org=org, namespace=namespace, agent="*", user_id="-", session_id="-"
    )
    rows = await memory_repo.get_many_by_keys(
        ident, wanted, ch=ch, strong=True, scope=WHOLE_NAMESPACE
    )
    by_key = {r.memory_key: r for r in rows}
    to_write: list[dict[str, Any]] = []
    decided: list[Any] = []
    out: list[DecideResult] = []
    for key in wanted:
        row = by_key.get(key)
        if row is None:
            out.append(DecideResult(key, False, "", "not found"))
            continue
        status = str(getattr(row, "status", "") or "")
        if status != PROPOSED:
            out.append(DecideResult(key, False, status, f"not a proposal (status {status})"))
            continue
        # An accepted proposal that met a contradiction on trial lands as a
        # conflicted fact in the same version (no re-read that a lagging
        # replica could answer with the proposal); its peers are flagged below.
        contradicted = bool(getattr(row, "conflict_with", None))
        accepted_status = "conflicted" if contradicted else "active"
        overrides: dict[str, Any] = {
            "version": memory_repo.next_version(),
            "status": accepted_status if decision == "accept" else "rejected",
        }
        if decision == "reject":
            overrides["rejection_reason"] = (reason or "proposal rejected")[:512]
        to_write.append(memory_repo.build_memory_version_row(row, **overrides))
        decided.append(row)
        out.append(DecideResult(key, True, str(overrides["status"])))
    if not to_write:
        return out
    await insert_memory_versions(to_write, ch=ch)
    if decision == "accept":
        # The accepted row is the key's first served one: a proposal may not land on a
        # key that holds a fact. A rejected proposal was never served or announced.
        await webhooks.announce([("memory.written", r) for r in to_write])
    # Every decided row, `active` included: write_versions_by_ids mirrors only
    # non-active flips, and an accepted proposal's mirror still says proposed.
    await memory_repo.mirror_status_to_embeddings(to_write, ch=ch)
    await _audit_decisions(org, namespace, decided, decision, actor=actor, reason=reason)
    if decision == "accept":
        await _surface_conflicts(org, decided)
    return out


async def _audit_decisions(
    org: str, namespace: str, rows: list[Any], decision: Decision, *, actor: str, reason: str
) -> None:
    from server.observability import audit as _audit

    for row in rows:
        await _audit.emit(
            event_type=f"memory.proposal_{decision}",
            org=org,
            namespace=namespace,
            actor=actor,
            subject={
                "memory_key": row.memory_key,
                "memory_id": str(row.memory_id),
                "content_sha256": str(getattr(row, "content_sha256", "") or ""),
                # The reason stays on the rejected row, which erasure reaches; the audit
                # row keeps its hash and length.
                "reason_sha256": sha256_value(reason),
                "reason_chars": len(reason),
            },
        )


async def _surface_conflicts(org: str, rows: list[Any]) -> None:
    """An accepted proposal that met a contradiction on trial is now a fact
    that contradicts others: its row landed `conflicted` (see `decide`); the
    peers are flagged and cross-linked the way `mark_peers_conflicted` does."""
    import server.memory.store as memory_repo

    def _flag(new_id: str) -> Any:
        def _update(peer: Any) -> dict[str, Any]:
            links = [str(x) for x in (peer.conflict_with or []) if x]
            return {
                "status": PROPOSED
                if str(getattr(peer, "status", "")) == PROPOSED
                else "conflicted",
                "conflict_with": links if new_id in links else [*links, new_id],
            }

        return _update

    for row in rows:
        peers = [str(p) for p in (getattr(row, "conflict_with", None) or []) if p]
        if not peers:
            continue
        try:
            await memory_repo.write_versions_by_ids(org, peers, _flag(str(row.memory_id)))
        except Exception as e:  # noqa: BLE001 — the decision already landed
            log.warning("proposals: conflict surfacing failed for %s: %s", row.memory_key, e)


async def apply_policy(
    org: str, namespace: str, keys: list[str], *, ch: Any = None
) -> list[tuple[str, Decision, GradedCounts]]:
    """After a grade: under an owner-set `auto` mode, decide the proposals
    among `keys` whose graded counts cross the owner's thresholds. Returns
    what was decided; nothing under `manual`."""
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    policy = await policy_for(org, namespace, ch=ch, fresh=True)
    if policy.mode != "auto" or not keys:
        return []
    proposed = await proposal_keys_among(ch, org, namespace, keys)
    if not proposed:
        return []
    counts = await graded_counts(ch, org, namespace, proposed)
    plan: dict[Decision, list[str]] = {"accept": [], "reject": []}
    for key in proposed:
        verdict = policy_verdict(policy, counts.get(key, GradedCounts()))
        if verdict:
            plan[verdict].append(key)
    decided: list[tuple[str, Decision, GradedCounts]] = []
    for verdict, batch in plan.items():
        if not batch:
            continue
        results = await decide(
            org,
            namespace,
            batch,
            verdict,
            actor="process:proposal_policy",
            reason=f"owner policy: {policy.model_dump_json()}",
            ch=ch,
        )
        decided.extend(
            (r.memory_key, verdict, counts.get(r.memory_key, GradedCounts()))
            for r in results
            if r.decided
        )
    return decided


async def proposal_keys_among(ch: Any, org: str, namespace: str, keys: list[str]) -> list[str]:
    """The keys that are live proposals now (sequentially consistent: the
    answer gates a write)."""
    import server.memory.store as memory_repo

    wanted = sorted({k for k in keys if k})
    if not wanted:
        return []
    rows = await ch.query_all(
        """
        SELECT DISTINCT memory_key FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)}
          AND tombstoned_at IS NULL AND status = 'proposed'
        """
        + (memory_repo._SEQUENTIAL if memory_repo.read_your_writes_enabled() else ""),
        params={"org": org, "ns": namespace, "keys": wanted},
    )
    return [str(r["memory_key"]) for r in rows]
