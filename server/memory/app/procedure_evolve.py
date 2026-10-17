"""procedure.evolve, procedure.revise and procedure.fork: a procedure's next
version, gated on its graded runs, rewritten by hand, or copied into another
namespace."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from server.core.errors import NotFoundError
from server.memory.api.models_crud import MemorySetInput
from server.memory.api.models_procedure import (
    ProcedureEvolveInput,
    ProcedureEvolveOutput,
    ProcedureForkInput,
    ProcedureForkOutput,
    ProcedureReviseInput,
    ProcedureReviseOutput,
)
from server.memory.app.procedure import _load_procedure_value, _procedure_memory_key

MAX_REJECTED_KEPT = 8
MAX_REVISIONS_KEPT = 16


def _apply_gate(
    value: dict[str, Any],
    by_revision: dict[int, tuple[int, int]] | None,
    min_runs: int,
    margin: float,
    out: dict[str, Any],
    *,
    mode: str = "sprt",
    min_drop: float = 0.15,
    alpha: float = 0.05,
    beta: float = 0.20,
    max_runs: int = 50,
    cost_by_revision: dict[int, dict[str, float]] | None = None,
    cost_tolerance: float = 0.25,
    cost_min_samples: int = 3,
) -> str:
    """Judge the pending revision (if any) on ledger evidence and mutate
    `value` accordingly: accept locks it in, revert restores the prior graph
    (carrying the statistics accumulated meanwhile) and records the rejected
    topology so it is recognised if proposed again. Returns the verdict.

    A revision the success gate accepts is then asked what it cost: if its successful
    runs are more than `cost_tolerance` dearer than the prior revision's, with
    `cost_min_samples` costed successes on each side, it is reverted with
    `gate_reason="cost"`."""
    from server.memory.domain.procedure_graph import (
        ProcedureGraph,
        RevisionCost,
        RunStats,
        carry_stats,
        cost_verdict,
        gate_revision,
        gate_revision_sprt,
        graph_digest,
        live_steps,
    )

    pend = value.get("pending_revision")
    if not isinstance(pend, dict):
        return "none"
    if by_revision is None:
        return "undecided"  # ledger unavailable: no evidence, no verdict
    cur_rev = int(value.get("revision", 1) or 1)
    prior_rev = int(pend.get("prior_revision", cur_rev - 1))
    b_total, b_ok = by_revision.get(prior_rev, (0, 0))
    a_total, a_ok = by_revision.get(cur_rev, (0, 0))
    before, after = RunStats(total=b_total, ok=b_ok), RunStats(total=a_total, ok=a_ok)
    out["gate_before"] = before.model_dump()
    out["gate_after"] = after.model_dump()
    out["gate_mode"] = mode
    if mode == "margin":
        verdict = gate_revision(before, after, min_runs=min_runs, margin=margin)
        out["gate_llr"] = 0.0
    else:
        verdict, llr = gate_revision_sprt(
            before,
            after,
            min_runs=min_runs,
            min_drop=min_drop,
            alpha=alpha,
            beta=beta,
            max_runs=max_runs,
        )
        out["gate_llr"] = round(llr, 4)
    if verdict == "undecided":
        return verdict

    out["gate_reason"] = "success" if verdict == "revert" else ""
    if verdict == "accept" and cost_by_revision is not None and cost_tolerance > 0:
        c_before = RevisionCost.model_validate(cost_by_revision.get(prior_rev, {}))
        c_after = RevisionCost.model_validate(cost_by_revision.get(cur_rev, {}))
        out["gate_cost_before"] = c_before.model_dump()
        out["gate_cost_after"] = c_after.model_dump()
        if (
            cost_verdict(
                c_before,
                c_after,
                min_samples=cost_min_samples,
                tolerance=cost_tolerance,
            )
            == "costlier"
        ):
            verdict = "revert"
            out["gate_reason"] = "cost"

    revisions = [r for r in (value.get("revisions") or []) if isinstance(r, dict)]
    if verdict == "accept":
        for r in revisions:
            if int(r.get("revision", -1)) == cur_rev:
                r["outcome"] = "accepted"
                r["gate"] = {"before": before.model_dump(), "after": after.model_dump()}
        value.pop("pending_revision", None)
        value["revisions"] = revisions[-MAX_REVISIONS_KEPT:]
        return verdict

    # revert: the revision under test lost. Restore the prior graph, carrying
    # over the statistics the current graph accumulated on shared ids.
    current = ProcedureGraph.model_validate(value.get("graph") or {})
    prior = ProcedureGraph.model_validate(pend.get("graph") or {})
    restored = carry_stats(current, prior)
    rejected = [r for r in (value.get("rejected") or []) if isinstance(r, dict)]
    rejected.append(
        {
            "revision": cur_rev,
            "digest": graph_digest(current),
            "reason": str(pend.get("reason", "")),
            "by": str(pend.get("by", "")),
            "before": before.model_dump(),
            "after": after.model_dump(),
            "gate_reason": str(out.get("gate_reason", "success")),
            "cost_before": dict(out.get("gate_cost_before", {})),
            "cost_after": dict(out.get("gate_cost_after", {})),
            "steps": live_steps(current)[:16],
            "ts": str(pend.get("ts", "")),
        }
    )
    value["rejected"] = rejected[-MAX_REJECTED_KEPT:]
    for r in revisions:
        if int(r.get("revision", -1)) == cur_rev:
            r["outcome"] = "reverted"
            r["gate"] = {"before": before.model_dump(), "after": after.model_dump()}
    value["revisions"] = revisions[-MAX_REVISIONS_KEPT:]
    value["graph"] = restored.model_dump()
    value["steps"] = live_steps(restored) or list(pend.get("steps") or value.get("steps", []))
    value["template"] = False
    # A new revision number so runs after the revert are not attributed to
    # either judged side.
    value["revision"] = cur_rev + 1
    value.pop("pending_revision", None)
    return verdict


async def _rmw_procedure(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    attempts: int = 6,
) -> tuple[int, dict[str, Any]]:
    """Read-modify-write a procedure row under optimistic concurrency.

    loom.memory is a ReplacingMergeTree, so a just-written version is not
    always visible to the next read (read-your-writes lag) and two writers can
    race. We read the row + its content hash, apply `mutate`, and write back
    with `if_match_sha256`; a stale read makes the precondition fail, and we
    re-read and retry with backoff until it converges. Returns (version, value).
    """
    import asyncio

    from fastapi import HTTPException

    from server.core.errors import PreconditionFailedError
    from server.core.feature_modes import bitemporal_on
    from server.memory.app.write import handle_memory_set

    last: Exception | None = None
    for attempt in range(attempts):
        value, sha = await _load_procedure_value(
            org,
            namespace,
            agent,
            user_id,
            session_id,
            memory_key,
        )
        new_value = mutate(value)
        try:
            result = await handle_memory_set(
                MemorySetInput(
                    org=org,
                    namespace=namespace,
                    agent=agent,
                    user_id=user_id,
                    session_id=session_id,
                    memory_key=memory_key,
                    value=new_value,
                    memory_type="procedural",
                    tags=["procedure", *list(new_value.get("tags", []))],
                    if_match_sha256=sha,
                )
            )
            return int(result.version), new_value
        except PreconditionFailedError as e:  # the journal's: a newer value holds now
            if not bitemporal_on():
                raise
            last = e
            await asyncio.sleep(0.25 * (attempt + 1))
        except HTTPException as e:
            code = ""
            if isinstance(e.detail, dict):
                code = str(e.detail.get("code", ""))
            if code != "PRECONDITION_FAILED":
                raise
            last = e
            await asyncio.sleep(0.25 * (attempt + 1))
    if not bitemporal_on():
        raise RuntimeError(
            f"procedure {memory_key!r} write lost to contention after {attempts} attempts"
        ) from last
    raise PreconditionFailedError(
        f"procedure {memory_key!r} write lost to contention after {attempts} attempts; retry"
    ) from last


MAX_AGENTS_TRACKED = 32


def _fold_agent_stats(value: dict[str, Any], agents_ok: list[tuple[str, bool]]) -> None:
    """Per-agent {total, ok} on the row, bounded. A procedure proven by one
    agent may fail for another (procedures over-specialise to the agent that
    learned them); match and locate report this for the caller."""
    by_agent = value.get("run_stats_by_agent")
    if not isinstance(by_agent, dict):
        by_agent = {}
    for agent, ok in agents_ok:
        a = (agent or "").strip()
        if not a or a in ("*", "-"):
            continue
        if a not in by_agent and len(by_agent) >= MAX_AGENTS_TRACKED:
            continue
        cur = by_agent.get(a) or {"total": 0, "ok": 0}
        cur["total"] = int(cur.get("total", 0)) + 1
        cur["ok"] = int(cur.get("ok", 0)) + (1 if ok else 0)
        by_agent[a] = cur
    value["run_stats_by_agent"] = by_agent


def _fold_runs_into_value(value: dict[str, Any], traces: list[Any]) -> dict[str, int]:
    """Fold traces into the row's graph / run_stats / runs in place with the
    pure merge_run; returns the resulting run_stats."""
    from server.memory.domain.procedure_graph import (
        ProcedureGraph,
        RunStats,
        RunTrace,
        merge_run,
        steps_to_graph,
    )

    raw_graph = value.get("graph")
    graph = (
        ProcedureGraph.model_validate(raw_graph)
        if isinstance(raw_graph, dict)
        else steps_to_graph(list(value.get("steps", [])))
    )
    stats = RunStats.model_validate(value.get("run_stats") or {})
    runs = [RunTrace.model_validate(r) for r in (value.get("runs") or []) if isinstance(r, dict)]
    for t in traces:
        graph, stats, runs = merge_run(graph, stats, runs, t)
    value["graph"] = graph.model_dump()
    value["run_stats"] = stats.model_dump()
    value["runs"] = [r.model_dump() for r in runs]
    return stats.model_dump()


async def _evolve_ledger(
    input: ProcedureEvolveInput,
) -> tuple[
    list[dict[str, Any]], dict[int, tuple[int, int]] | None, dict[int, dict[str, float]] | None
]:
    """Ledger rows not yet folded into the row, and — when a revision is on
    trial — the per-revision outcome and cost stats the gate reads. Fetched
    once, outside the RMW; `mutate` re-filters against the watermark it
    actually reads, so a concurrent evolve that already folded some of them
    cannot double-count."""
    from server.memory.app import procedure_ledger

    by_revision: dict[int, tuple[int, int]] | None = None
    cost_by_revision: dict[int, dict[str, float]] | None = None
    probe, _ = await _load_procedure_value(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_key,
    )
    pending = await procedure_ledger.unfolded(
        org=input.org,
        namespace=input.namespace,
        memory_key=input.memory_key,
        since=str(probe.get("ledger_folded_through") or ""),
    )
    if probe.get("pending_revision"):
        by_revision = await procedure_ledger.stats_by_revision(
            org=input.org,
            namespace=input.namespace,
            memory_key=input.memory_key,
        )
        cost_by_revision = await procedure_ledger.cost_by_revision(
            org=input.org,
            namespace=input.namespace,
            memory_key=input.memory_key,
        )
    return pending, by_revision, cost_by_revision


def _fold_pending(value: dict[str, Any], pending: list[dict[str, Any]]) -> int:
    """Fold the ledger rows past the row's watermark into it; how many."""
    from server.memory.domain.procedure_graph import RunTrace

    since = str(value.get("ledger_folded_through") or "")
    fresh = [r for r in pending if r["ts"] > since]
    if fresh:
        _fold_runs_into_value(
            value,
            [
                RunTrace(
                    path=r["path"],
                    ok=r["ok"],
                    note=r["note"],
                    ts=r["ts"],
                    steps=int(r.get("steps", 0) or 0),
                    duration_ms=int(r.get("duration_ms", 0) or 0),
                    tokens=int(r.get("tokens", 0) or 0),
                )
                for r in fresh
            ],
        )
        _fold_agent_stats(value, [(str(r.get("agent", "")), bool(r["ok"])) for r in fresh])
        value["ledger_folded_through"] = max(r["ts"] for r in fresh)
    return len(fresh)


def _strike_out(value: dict[str, Any], traces: list[Any], limit: int) -> tuple[int, bool]:
    """Strikes: K consecutive failed runs strike the procedure out; a success
    on record lifts it. Reads the row's own trace log only. Returns the
    strike count and whether the quarantine mark changed."""
    from server.memory.domain.procedure_graph import trailing_failures

    strikes = trailing_failures(traces)
    was_quarantined = isinstance(value.get("quarantined"), dict)
    if limit > 0 and strikes >= limit and not was_quarantined:
        value["quarantined"] = {
            "at": traces[-1].ts if traces and traces[-1].ts else "",
            "strikes": strikes,
            "reason": f"{strikes} consecutive failed runs (limit {limit})",
        }
        return strikes, True
    if was_quarantined and traces and traces[-1].ok:
        value.pop("quarantined", None)
        return strikes, True
    return strikes, False


def _record_evolve_metric(namespace: str, out: dict[str, Any]) -> None:
    try:
        from server.observability.metrics import record_procedure_evolve

        record_procedure_evolve(
            namespace=namespace,
            result=(
                "reverted"
                if out.get("gate") == "revert"
                else "quarantined"
                if out.get("quarantined")
                else "template"
                if out["template"]
                else "changed"
                if out["changed"]
                else "stable"
            ),
        )
    except Exception:  # noqa: BLE001 — telemetry never fails the op
        pass


def _evolve_output(
    input: ProcedureEvolveInput, version: int, out: dict[str, Any]
) -> ProcedureEvolveOutput:
    return ProcedureEvolveOutput(
        memory_key=input.memory_key,
        version=version,
        revision=int(out["revision"]),
        changed=bool(out["changed"]),
        template=bool(out["template"]),
        folded_runs=int(out.get("folded", 0)),
        gate=out.get("gate", "none"),
        gate_mode=str(out.get("gate_mode", "")),
        gate_llr=float(out.get("gate_llr", 0.0)),
        gate_before=dict(out.get("gate_before", {})),
        gate_after=dict(out.get("gate_after", {})),
        gate_cost_before=dict(out.get("gate_cost_before", {})),
        gate_cost_after=dict(out.get("gate_cost_after", {})),
        gate_reason=str(out.get("gate_reason", "")),
        dead_end_steps=list(out["dead_end_steps"]),
        fail_prone_steps=list(out["fail_prone_steps"]),
        fail_prone_transitions=list(out["fail_prone_transitions"]),
        run_stats=dict(out["run_stats"]),
        strikes=int(out.get("strikes", 0)),
        quarantined=bool(out.get("quarantined", False)),
        retention=dict(out.get("retention", {})),
    )


async def handle_procedure_evolve(input: ProcedureEvolveInput) -> ProcedureEvolveOutput:
    """Consolidate accumulated traces into an evolved graph: keep the proven
    path, flag dead-end steps, flag the steps and transitions that sit
    disproportionately on failed runs, re-derive the linear `steps`, and
    promote to a reusable template when the topology has stabilised and
    mostly succeeds."""
    from server.memory.domain.procedure_graph import (
        ProcedureGraph,
        RunStats,
        RunTrace,
        evolve_graph,
        is_template_ready,
        live_steps,
        steps_to_graph,
        success_retention,
    )
    from server.settings import settings as _settings

    out: dict[str, Any] = {}
    pending, by_revision, cost_by_revision = await _evolve_ledger(input)

    def mutate(value: dict[str, Any]) -> dict[str, Any]:
        out["folded"] = _fold_pending(value, pending)
        out["gate"] = _apply_gate(
            value,
            by_revision,
            input.gate_min_runs,
            input.gate_margin,
            out,
            mode=input.gate_mode,
            min_drop=input.gate_min_drop,
            alpha=input.gate_alpha,
            beta=input.gate_beta,
            max_runs=input.gate_max_runs,
            cost_by_revision=cost_by_revision,
            cost_tolerance=input.gate_cost_tolerance,
            cost_min_samples=(
                input.gate_cost_min_samples
                if input.gate_cost_min_samples is not None
                else input.gate_min_runs
            ),
        )
        raw_graph = value.get("graph")
        graph = (
            ProcedureGraph.model_validate(raw_graph)
            if isinstance(raw_graph, dict)
            else steps_to_graph(list(value.get("steps", [])))
        )
        stats = RunStats.model_validate(value.get("run_stats") or {})
        evolved, changed = evolve_graph(graph, stats, min_runs=input.min_runs)
        limit = (
            int(input.strike_limit)
            if input.strike_limit is not None
            else int(_settings.procedure_strike_limit)
        )
        traces = [
            RunTrace.model_validate(r) for r in (value.get("runs") or []) if isinstance(r, dict)
        ]
        strikes, struck = _strike_out(value, traces, limit)
        changed = changed or struck
        is_quarantined = isinstance(value.get("quarantined"), dict)
        template = is_template_ready(evolved, stats) and not is_quarantined
        out["strikes"] = strikes
        out["quarantined"] = is_quarantined
        out["retention"] = success_retention(traces)

        value["graph"] = evolved.model_dump()
        value["steps"] = live_steps(evolved) or list(value.get("steps", []))
        value["template"] = template
        # Flag changes are statistics, not topology edits: they do not open a
        # new revision (a revision is what the gate judges; see revise()).
        value["revision"] = int(value.get("revision", 1))
        if template and "template" not in value.get("tags", []):
            value["tags"] = [*list(value.get("tags", [])), "template"]

        out["changed"] = changed
        out["template"] = template
        out["revision"] = int(value["revision"])
        out["dead_end_steps"] = [n.text for n in evolved.nodes if n.dead_end and n.text]
        out["fail_prone_steps"] = [(n.text or n.id) for n in evolved.nodes if n.fail_prone]
        out["fail_prone_transitions"] = [
            f"{e.src} -> {e.dst}" for e in evolved.edges if e.fail_prone
        ]
        out["run_stats"] = stats.model_dump()
        return value

    version, _ = await _rmw_procedure(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_key,
        mutate,
    )
    _record_evolve_metric(input.namespace, out)
    return _evolve_output(input, version, out)


async def handle_procedure_revise(input: ProcedureReviseInput) -> ProcedureReviseOutput:
    """Open a revision of a procedure's topology. The agent is
    the refiner: it contrasts failed and successful runs (procedure.match
    returns `fail_prone_steps`; the ledger has every run) and proposes a new
    graph. loom validates it structurally, refuses a topology the gate has
    already reverted (unless `force`), carries over statistics for unchanged
    nodes and edges, snapshots the prior graph, and opens a new revision.
    Runs recorded from now on are attributed to it; the next procedure.evolve
    with enough runs on both sides accepts or reverts it."""
    from server.memory.domain.procedure_graph import (
        GraphValidationError,
        ProcedureGraph,
        carry_stats,
        graph_digest,
        live_steps,
        normalize_graph,
    )

    try:
        proposed = normalize_graph(input.graph)
    except GraphValidationError as e:
        raise ValueError(f"invalid procedure graph: {e}") from e
    digest = graph_digest(proposed)
    out: dict[str, Any] = {}

    def mutate(value: dict[str, Any]) -> dict[str, Any]:
        if value.get("pending_revision"):
            raise ValueError(
                "a revision is already pending the gate; record runs and call "
                "procedure.evolve before proposing another"
            )
        for r in value.get("rejected") or []:
            if isinstance(r, dict) and r.get("digest") == digest and not input.force:
                raise ValueError(
                    f"this topology was rejected at revision {r.get('revision')} "
                    f"(success {r.get('before')} -> {r.get('after')}); pass force=true to retry"
                )
        current = ProcedureGraph.model_validate(value.get("graph") or {})
        if graph_digest(current) == digest:
            raise ValueError("proposed graph is identical to the current topology")
        cur_rev = int(value.get("revision", 1) or 1)
        new_graph = carry_stats(current, proposed)
        value["pending_revision"] = {
            "prior_revision": cur_rev,
            "graph": current.model_dump(),
            "steps": list(value.get("steps", [])),
            "reason": input.reason,
            "by": input.agent,
            "ts": _now_iso(),
        }
        revisions = [r for r in (value.get("revisions") or []) if isinstance(r, dict)]
        revisions.append(
            {
                "revision": cur_rev + 1,
                "digest": digest,
                "reason": input.reason,
                "by": input.agent,
                "ts": _now_iso(),
                "outcome": "pending",
            }
        )
        value["revisions"] = revisions[-MAX_REVISIONS_KEPT:]
        value["graph"] = new_graph.model_dump()
        value["steps"] = live_steps(new_graph) or list(value.get("steps", []))
        value["template"] = False  # must re-prove itself
        value["revision"] = cur_rev + 1
        out["revision"] = cur_rev + 1
        out["prior_revision"] = cur_rev
        out["steps"] = list(value["steps"])
        return value

    version, _ = await _rmw_procedure(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_key,
        mutate,
    )
    return ProcedureReviseOutput(
        memory_key=input.memory_key,
        version=version,
        revision=int(out["revision"]),
        prior_revision=int(out["prior_revision"]),
        digest=digest,
        steps=list(out["steps"]),
    )


def _now_iso() -> str:
    from datetime import UTC

    return datetime.now(UTC).replace(microsecond=0).isoformat()


async def _assert_can_read_namespace(org: str, namespace: str) -> str:
    """VIEWER-or-better on (org, namespace) for the current caller, by the
    canonical check (a scoped token's per-namespace role is its ceiling; an
    unscoped token needs the namespace in its claim plus a role, else a grant,
    with the service-account creator fallback). Returns the caller subject."""
    from server.auth.authz import row_in_scope
    from server.auth.context import current_auth
    from server.core.errors import AuthInsufficientError, ValidationError
    from server.operations.base import Role

    auth = current_auth()
    if auth is None or not auth.subject:
        raise ValidationError(
            "procedure.fork requires an authenticated caller; enable LOOM_AUTH_REQUIRED=true"
        )
    if not await row_in_scope(org, namespace, Role.VIEWER):
        raise AuthInsufficientError(
            f"{auth.subject!r} cannot read {org}/{namespace}; cannot fork from it"
        )
    return auth.subject


async def handle_procedure_fork(input: ProcedureForkInput) -> ProcedureForkOutput:
    """Adopt a procedure from another namespace. The topology,
    attributes, situation and steps are copied; every statistic, flag,
    revision, pending edit and rejection is reset, because a procedure proven
    by other agents in another namespace is unproven here — exactly what the
    per-agent record and the `authored` status will then say. Lineage is kept
    in `forked_from`."""
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.procedure_graph import (
        ProcedureGraph,
        graph_digest,
        reset_evidence,
        steps_to_graph,
    )

    from_org = input.from_org or input.org
    caller = await _assert_can_read_namespace(from_org, input.from_namespace)

    source, _ = await _load_procedure_value(
        from_org,
        input.from_namespace,
        "*",
        "-",
        "-",
        input.memory_key,
    )
    name = (input.new_name or str(source.get("name") or "")).strip()
    if not name:
        raise ValueError("source procedure has no name; pass new_name")
    target_key = _procedure_memory_key(name)

    # Refuse to overwrite a procedure the target already has under that key.
    try:
        await _load_procedure_value(
            input.org,
            input.namespace,
            "*",
            "-",
            "-",
            target_key,
        )
    except NotFoundError:
        pass
    else:
        raise ValueError(
            f"procedure {target_key!r} already exists in {input.namespace}; pass new_name"
        )

    raw_graph = source.get("graph")
    src_graph = (
        ProcedureGraph.model_validate(raw_graph)
        if isinstance(raw_graph, dict)
        else steps_to_graph(list(source.get("steps", [])))
    )
    graph = reset_evidence(src_graph)
    tags = [t for t in (source.get("tags") or []) if t not in ("template", "mined", "forked")]
    forked_from = {
        "org": from_org,
        "namespace": input.from_namespace,
        "memory_key": input.memory_key,
        "revision": int(source.get("revision", 1) or 1),
        "digest": graph_digest(src_graph),
        "template": bool(source.get("template", False)),
        "run_stats": dict(source.get("run_stats") or {}),
        "by": caller,
        "reason": input.reason,
        "ts": _now_iso(),
    }
    value: dict[str, Any] = {
        "situation": str(source.get("situation") or ""),
        "name": name,
        "steps": list(source.get("steps") or []),
        "tags": [*tags, "forked"],
        "metadata": dict(source.get("metadata") or {}),
        "graph": graph.model_dump(),
        "run_stats": {"total": 0, "ok": 0},
        "runs": [],
        "template": False,
        "revision": 1,
        "forked_from": forked_from,
    }
    result = await handle_memory_set(
        MemorySetInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            memory_key=target_key,
            value=value,
            memory_type="procedural",
            tags=["procedure", *value["tags"]],
        )
    )
    return ProcedureForkOutput(
        memory_key=target_key,
        version=int(result.version),
        name=name,
        steps=list(value["steps"]),
        forked_from=forked_from,
    )
