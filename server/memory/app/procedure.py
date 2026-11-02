"""procedure.* handlers: add, match, list, record_run, evolve, revise, fork, locate.

A procedure is a memory row under `procedure_<slug>` whose value holds the
situation, steps and graph (server/memory/api/models_procedure.py has the wire
models). Reads and writes go through the memory plane's handlers; the run
ledger and the evolve gate's statistics live in memory.app.procedure_ledger.
"""

from __future__ import annotations

import logging
from typing import Any

from server.core.ch import own_write_reads
from server.core.datetimes import naive_utc_now
from server.core.errors import NotFoundError
from server.identity.app.request_scope import request_scope
from server.memory.api.models_crud import MemoryGetInput, MemoryListInput, MemorySetInput
from server.memory.api.models_procedure import (
    ProcedureAddInput,
    ProcedureAddOutput,
    ProcedureListInput,
    ProcedureListOutput,
    ProcedureLocateInput,
    ProcedureLocateOutput,
    ProcedureMatchInput,
    ProcedureMatchOutput,
    ProcedureRecordRunInput,
    ProcedureRecordRunOutput,
    ProcedureStatus,
    _ProcedureMatch,
    _ProcedureSummary,
)
from server.memory.api.models_search import MemorySearchInput
from server.memory.domain.procedure_evidence import two_axis_score
from server.memory.domain.procedure_graph import utility_from_stats
from server.memory.domain.sensitivity import ATTRIBUTE_TAG_PREFIX, SENSITIVITY_TAG_PREFIX

log = logging.getLogger(__name__)
_REJECTED_PUBLIC_KEYS = ("revision", "digest", "reason", "before", "after", "steps")
_STATUS_RANK: dict[str, int] = {
    "template": 0,
    "exercised": 0,
    "authored": 0,
    "candidate": 1,
    "quarantined": 2,
}


def _procedure_status(value: dict[str, Any]) -> ProcedureStatus:
    """template > exercised (>=1 recorded run) > authored (registered by a
    caller, no runs yet) > candidate (machine-mined, no runs yet). Only the
    candidate tier is demoted: an unvalidated machine-written prior is the
    one thing the evidence says can make an agent worse than no procedure."""
    if isinstance(value.get("quarantined"), dict):
        # Struck out: its last runs all failed. Excluded from
        # match unless asked for; a success on record lifts it.
        return "quarantined"
    if bool(value.get("template", False)):
        return "template"
    total = int((value.get("run_stats") or {}).get("total", 0) or 0)
    if total > 0:
        return "exercised"
    if "mined" in (value.get("tags") or []):
        return "candidate"
    return "authored"


_PROCEDURE_KEY_PREFIX = "procedure_"
_PROCEDURE_TAG = "procedure"


def _procedure_memory_key(name: str) -> str:
    import hashlib
    import re

    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:96]
    if not slug:
        # A name with no ASCII letter or digit ("日本の手順") leaves no slug;
        # a digest of it keeps two such names off one shared key.
        slug = hashlib.sha256(name.strip().lower().encode()).hexdigest()[:16]
    return f"{_PROCEDURE_KEY_PREFIX}{slug}"


async def handle_procedure_add(input: ProcedureAddInput) -> ProcedureAddOutput:
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.procedure_graph import (
        GraphValidationError,
        normalize_graph,
        steps_to_graph,
    )

    memory_key = _procedure_memory_key(input.name)
    # Every procedure gets a graph substrate: use the caller's graph when given
    # (validated), else synthesise a linear graph from `steps`.
    try:
        graph = (
            normalize_graph(input.graph) if input.graph is not None else steps_to_graph(input.steps)
        )
    except GraphValidationError as e:
        raise ValueError(f"invalid procedure graph: {e}") from e
    # `situation` first so it dominates the dict's embedding signal.
    value = {
        "situation": input.situation,
        "name": input.name,
        "steps": input.steps,
        "tags": list(input.tags),
        "metadata": dict(input.metadata),
        "graph": graph.model_dump(),
        "run_stats": {"total": 0, "ok": 0},
        "runs": [],
        "template": False,
        "revision": 1,
    }
    set_in = MemorySetInput(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        memory_key=memory_key,
        value=value,
        memory_type="procedural",
        tags=[_PROCEDURE_TAG, *input.tags],
    )
    result = await handle_memory_set(set_in)
    return ProcedureAddOutput(
        memory_key=memory_key,
        memory_id=str(result.memory_id),
        version=int(result.version),
    )


async def _load_key_embeddings(
    org: str, keys_by_ns: dict[str, list[str]]
) -> dict[tuple[str, str], list[float]]:
    """Key-axis vectors of the candidate procedure rows (memory.set writes
    them). One read, bounded by top_k; rows whose key
    vector is absent are simply missing from the result."""
    from server.core.ch import get_client

    namespaces = sorted(keys_by_ns)
    keys = sorted({k for ks in keys_by_ns.values() for k in ks})
    rows = await get_client().query_all(
        """
        SELECT namespace, memory_key,
               argMax(memory_key_embedding, version)     AS key_embedding,
               argMax(memory_key_embedding_dim, version) AS key_dim
        FROM loom.memory_embeddings
        WHERE org = {org:String}
          AND namespace IN {nss:Array(String)}
          AND memory_key IN {keys:Array(String)}
          AND memory_type = 'procedural'
          AND tombstoned_at IS NULL
        GROUP BY namespace, memory_key
        """,
        {"org": org, "nss": namespaces, "keys": keys},
    )
    out: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        emb = list(r.get("key_embedding") or [])
        if emb and int(r.get("key_dim") or 0) == len(emb):
            out[(str(r.get("namespace")), str(r.get("memory_key")))] = [float(x) for x in emb]
    return out


async def _goal_similarities(
    org: str, goal: str, keys_by_ns: dict[str, list[str]]
) -> dict[tuple[str, str], float]:
    """Cosine of the goal against each candidate's key axis. A failure to
    read vectors or embed the goal degrades to single-axis matching rather
    than failing the match."""
    from server.embedding.embedder import get_embedder
    from server.memory.domain.procedure_evidence import cosine

    try:
        goal_vec = await get_embedder().embed(goal)
        key_vecs = await _load_key_embeddings(org, keys_by_ns)
    except Exception:
        log.warning(
            "procedure.match: goal axis unavailable, matching on situation only", exc_info=True
        )
        return {}
    sims: dict[tuple[str, str], float] = {}
    for ident, vec in key_vecs.items():
        c = cosine(goal_vec, vec)
        if c is not None:
            sims[ident] = round(c, 6)
    return sims


async def _procedure_rows(
    input: ProcedureMatchInput | ProcedureListInput, keys_by_ns: dict[str, list[str]]
) -> dict[tuple[str, str], Any]:
    """The live rows of the candidate keys whose value is a dict, by (namespace, key).

    A 'high' row is never a candidate: the locate leaves it out, so under the wildcard
    agent the plain row the search matched answers, and no key read is audited."""
    import server.memory.store as memory_repo

    rows: dict[tuple[str, str], Any] = {}
    for ns, keys in keys_by_ns.items():
        identity = memory_repo.Identity(
            org=input.org,
            namespace=ns,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
        )
        try:
            found = await memory_repo.get_many_by_keys(
                identity, keys, scope=request_scope(), exclude_high_sensitivity=True
            )
        except Exception:  # noqa: BLE001 — an unreadable namespace drops its candidates, not the match
            continue
        for row in found:
            if isinstance(row.value, dict):
                rows[(ns, row.memory_key)] = row
    return rows


async def handle_procedure_match(input: ProcedureMatchInput) -> ProcedureMatchOutput:
    """Top-K procedures by situation similarity (and goal similarity when a
    goal is given), each with its full body."""
    from server.memory.app import search as _search

    search_in = MemorySearchInput(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        query=input.situation,
        memory_type="procedural",
        tags=list(input.tags),
        top_k=input.top_k,
        namespace_scope=input.namespace_scope,
        search_mode="semantic",
        recipe="fast",
        hyde=False,
        auto_touch=False,
        # With a goal, the situation must score against the situation axis
        # alone — the key axis carries the name and is scored by the goal.
        key_axis=False if input.goal else None,
    )
    search_out = await _search.handle_memory_search(search_in)

    u_weight = float(input.utility_weight or 0.0)

    keys_by_ns: dict[str, list[str]] = {}
    for hit in search_out.results:
        keys_by_ns.setdefault(hit.namespace or input.namespace, []).append(hit.memory_key)

    goal_sims: dict[tuple[str, str], float] = {}
    if input.goal and keys_by_ns:
        goal_sims = await _goal_similarities(input.org, input.goal, keys_by_ns)

    rows_by_key = await _procedure_rows(input, keys_by_ns)

    matches: list[_ProcedureMatch] = []
    seen: set[tuple[str, str]] = set()
    for hit in search_out.results:
        ns_key = (hit.namespace or input.namespace, hit.memory_key)
        row = rows_by_key.get(ns_key)
        value = row.value if row is not None else {}
        if row is None or ns_key in seen or not isinstance(value, dict) or "steps" not in value:
            continue  # the key's rows read as one procedure, at its best hit
        seen.add(ns_key)
        is_template = bool(value.get("template", False))
        if input.templates_only and not is_template:
            continue
        status = _procedure_status(value)
        if status == "candidate" and not input.include_candidates:
            continue
        if status == "quarantined" and not input.include_quarantined:
            continue
        raw_stats = value.get("run_stats") or {}
        run_stats = {
            "total": int(raw_stats.get("total", 0)),
            "ok": int(raw_stats.get("ok", 0)),
        }
        graph = value.get("graph")
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        fail_prone_steps = [
            str(n.get("text") or n.get("id") or "")
            for n in nodes
            if isinstance(n, dict) and n.get("fail_prone")
        ]
        overlap = 0.0
        if input.recent_tools:
            tools = {
                str(n.get("tool") or n.get("id") or "").lower()
                for n in nodes
                if isinstance(n, dict)
            }
            wanted = {t.lower() for t in input.recent_tools if t}
            overlap = len(wanted & tools) / len(wanted) if wanted else 0.0
        # Ledger-proven utility: pure function of the folded
        # run_stats, so an unrun procedure scores exactly its similarity.
        utility = utility_from_stats(run_stats["total"], run_stats["ok"])
        goal_sim = goal_sims.get((hit.namespace or input.namespace, hit.memory_key))
        axes = two_axis_score(float(hit.similarity), goal_sim)
        by_agent_raw = value.get("run_stats_by_agent")
        by_agent = (
            {
                str(k): {"total": int(v.get("total", 0)), "ok": int(v.get("ok", 0))}
                for k, v in by_agent_raw.items()
                if isinstance(v, dict)
            }
            if isinstance(by_agent_raw, dict)
            else {}
        )
        matches.append(
            _ProcedureMatch(
                name=str(value.get("name", hit.memory_key.replace(_PROCEDURE_KEY_PREFIX, ""))),
                situation=str(value.get("situation", "")),
                steps=list(value.get("steps", [])),
                tags=list(value.get("tags", [])),
                metadata=dict(value.get("metadata", {})),
                similarity=hit.similarity,
                memory_key=hit.memory_key,
                namespace=hit.namespace or input.namespace,
                graph=graph if isinstance(graph, dict) else None,
                template=is_template,
                run_stats=run_stats,
                status=status,
                quarantined=(
                    value.get("quarantined") if isinstance(value.get("quarantined"), dict) else None
                ),
                fail_prone_steps=fail_prone_steps,
                revision=int(value.get("revision", 1) or 1),
                tool_overlap=round(overlap, 4),
                utility=utility,
                goal_similarity=goal_sim,
                score=round(axes + input.tool_boost * overlap + u_weight * utility, 6),
                for_agent=by_agent.get(input.agent, {}) if input.agent not in ("*", "-") else {},
                actor=str(getattr(row, "actor", "") or ""),
                run_stats_by_agent=by_agent,
                rejected_revisions=[
                    {k: r.get(k) for k in _REJECTED_PUBLIC_KEYS}
                    for r in (value.get("rejected") or [])
                    if isinstance(r, dict)
                ],
            )
        )
    # Fused score within a tier (situation×goal axes + tool overlap boost +
    # weighted ledger utility; equals similarity when no goal and no
    # recent_tools were given and the utility weight is 0); candidates
    # after everything else.
    matches.sort(key=lambda m: (_STATUS_RANK.get(m.status, 1), -m.score))
    _top = matches[0] if matches else None
    return ProcedureMatchOutput(
        matches=matches,
        matched=len(matches),
        procedure_key=str(_top.memory_key) if _top else "",
        score=float(_top.score) if _top else 0.0,
    )


def _procedure_owner(m: _ProcedureMatch) -> str:
    """The subject who wrote a procedure: its actor without the `human:` / `process:` kind.
    A procedure with no recorded actor (one the worker mined) is owned by nobody: its
    user_id is a client-supplied label, not a subject."""
    return m.actor.removeprefix("human:").removeprefix("process:")


def own_procedures(matches: list[_ProcedureMatch]) -> list[_ProcedureMatch]:
    """The matches the caller wrote. A pack injects procedures as steps to follow, so a
    teammate's procedure in a shared namespace is never auto-injected; procedure.match
    still finds it on request. Ownership is by principal: the caller's subject or, for a
    delegated token, its creator. Without an authenticated caller (auth off) all of them."""
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is None:
        return matches
    me = {s for s in (auth.subject, auth.creator) if s}
    return [m for m in matches if _procedure_owner(m) in me]


async def handle_procedure_list(input: ProcedureListInput) -> ProcedureListOutput:
    """Enumerate procedures — memory.list scoped by the procedure_ key prefix."""
    from server.memory.app.query import handle_memory_list

    list_in = MemoryListInput(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        prefix=_PROCEDURE_KEY_PREFIX + input.prefix,
        memory_type="procedural",
        limit=input.limit,
    )
    list_out = await handle_memory_list(list_in)
    # One read of the listed bodies. A list never reads a 'high' body: such a procedure is
    # listed by its key and the tags on its row.
    readable = [row.memory_key for row in list_out.entries if row.sensitivity != "high"]
    with own_write_reads():  # the body a procedure.add just revised, as memory.get read it
        bodies = await _procedure_rows(input, {input.namespace: readable})
    now = naive_utc_now()
    summaries: list[_ProcedureSummary] = []
    for row in list_out.entries:
        body = bodies.get((input.namespace, row.memory_key))
        expired = body is not None and body.expires_at is not None and body.expires_at <= now
        value: dict[str, Any] = body.value if body is not None and not expired else {}
        # The caller's tags, without the ones procedure.add and memory.set add themselves.
        row_tags = [
            t
            for t in row.tags
            if t != _PROCEDURE_TAG
            and not t.startswith((SENSITIVITY_TAG_PREFIX, ATTRIBUTE_TAG_PREFIX))
        ]
        summaries.append(
            _ProcedureSummary(
                name=str(value.get("name", row.memory_key.replace(_PROCEDURE_KEY_PREFIX, ""))),
                situation=str(value.get("situation", "")),
                tags=list(value.get("tags", row_tags)),
                memory_key=row.memory_key,
                created_at=row.created_at,
            )
        )
        if input.tags and not set(input.tags) & set(summaries[-1].tags):
            summaries.pop()  # caller-tag filter; drop if no overlap
    return ProcedureListOutput(procedures=summaries)


async def _load_procedure_value(
    org: str, namespace: str, agent: str, user_id: str, session_id: str, memory_key: str
) -> tuple[dict[str, Any], str]:
    """Return (value, content_sha256) for a procedure row. The hash is the
    optimistic-concurrency token for the write-back."""
    from server.memory.app.handlers import handle_memory_get

    get_input = MemoryGetInput(
        org=org,
        namespace=namespace,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        memory_key=memory_key,
    )
    # This read feeds a write-back under if_match_sha256. A lagging replica
    # can return the PREVIOUS version as a hit; folding against it would
    # re-apply runs the newer version already holds. Read the latest version,
    # always.
    get_input._strong_read = True
    get_out = await handle_memory_get(get_input)
    value = get_out.value if isinstance(get_out.value, dict) else None
    if not isinstance(value, dict) or "steps" not in value:
        # A read for a key that holds no procedure is 404
        # RESOURCE_NOT_FOUND on the wire, never an INTERNAL_ERROR 500.
        raise NotFoundError(f"no procedure at memory_key {memory_key!r}")
    return value, get_out.content_sha256


async def handle_procedure_record_run(
    input: ProcedureRecordRunInput,
) -> ProcedureRecordRunOutput:
    """Record one agent-reported execution. loom stores what the agent did —
    it never executes the graph.

    One INSERT into loom.procedure_runs against the procedure's current
    revision; the graph's counters catch up on the next procedure.evolve.
    The reply's run_stats = the row's folded stats + the ledger rows not yet
    folded, so callers see the run they just made.
    """
    from server.memory.app import procedure_ledger

    steps_taken = int(input.steps_taken) if input.steps_taken is not None else len(input.path)
    value, _ = await _load_procedure_value(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_key,
    )
    revision = int(value.get("revision", 1) or 1)
    await procedure_ledger.record(
        org=input.org,
        namespace=input.namespace,
        memory_key=input.memory_key,
        agent=input.agent,
        session_id=input.session_id,
        path=list(input.path),
        ok=input.ok,
        note=input.note,
        revision=revision,
        steps_taken=steps_taken,
        duration_ms=int(input.duration_ms),
        tokens=int(input.tokens),
    )
    since = str(value.get("ledger_folded_through") or "")
    pending_total, pending_ok = await procedure_ledger.count_since(
        org=input.org,
        namespace=input.namespace,
        memory_key=input.memory_key,
        since=since,
    )
    folded = value.get("run_stats") or {}
    try:
        from server.observability.metrics import record_procedure_run

        record_procedure_run(namespace=input.namespace, ok=input.ok)
    except Exception:  # noqa: BLE001 — telemetry never fails the op
        pass
    return ProcedureRecordRunOutput(
        memory_key=input.memory_key,
        version=0,
        run_stats={
            "total": int(folded.get("total", 0) or 0) + pending_total,
            "ok": int(folded.get("ok", 0) or 0) + pending_ok,
            "unfolded": pending_total,
        },
    )


async def handle_procedure_locate(input: ProcedureLocateInput) -> ProcedureLocateOutput:
    """Serve the agent's local neighbourhood in a procedure graph: the node it
    is at (matched by id or tool name), the transitions `hops` steps ahead
    with their condition / guidance / pitfalls and outcome counts, and the
    proven path from here. Read-only; nothing is executed or synthesised."""
    from server.memory.domain.procedure_graph import (
        ProcedureGraph,
        localize,
        render_local_context,
        steps_to_graph,
    )

    value, _ = await _load_procedure_value(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_key,
    )
    raw_graph = value.get("graph")
    graph = (
        ProcedureGraph.model_validate(raw_graph)
        if isinstance(raw_graph, dict)
        else steps_to_graph(list(value.get("steps", [])))
    )
    at = input.at or (input.recent_path[-1] if input.recent_path else "")
    ctx = localize(graph, at, hops=input.hops, recent_path=list(input.recent_path))
    name = str(value.get("name", ""))
    raw_stats = value.get("run_stats") or {}

    notes: list[dict[str, Any]] = []
    if input.include_notes and input.max_notes and ctx.matched:
        ahead = {(t.src, t.dst) for layer in ctx.hops for t in layer}
        try:
            from server.memory.app import procedure_ledger

            for n in await procedure_ledger.failure_notes(
                org=input.org,
                namespace=input.namespace,
                memory_key=input.memory_key,
                limit=max(20, input.max_notes * 4),
            ):
                here = ctx.active.id if ctx.active else ""
                if (n["src"], n["dst"]) in ahead or n["dst"] == here:
                    notes.append(n)
                if len(notes) >= input.max_notes:
                    break
        except Exception as e:  # noqa: BLE001 — notes are evidence, not the answer
            log.debug("procedure.locate: failure notes unavailable: %s", e)

    rendered = render_local_context(ctx, name=name)
    if notes:
        rendered += "\nRecent failure notes on the transitions ahead:\n" + "\n".join(
            f"- [{n['src']}] -> [{n['dst']}]: {n['note']}" for n in notes
        )
    by_agent = value.get("run_stats_by_agent")
    for_agent = (
        dict(by_agent.get(input.agent) or {})
        if isinstance(by_agent, dict) and input.agent not in ("*", "-")
        else {}
    )
    return ProcedureLocateOutput(
        memory_key=input.memory_key,
        name=name,
        status=_procedure_status(value),
        matched=ctx.matched,
        active=ctx.active.model_dump() if ctx.active is not None else None,
        hops=[[t.model_dump() for t in layer] for layer in ctx.hops],
        entry_nodes=list(ctx.entry_nodes),
        dominant_next=list(ctx.dominant_next),
        revisits=dict(ctx.revisits),
        looping=ctx.looping,
        exits=list(ctx.exits),
        failure_notes=notes,
        rendered=rendered,
        run_stats={
            "total": int(raw_stats.get("total", 0) or 0),
            "ok": int(raw_stats.get("ok", 0) or 0),
        },
        for_agent={k: int(v) for k, v in for_agent.items()} if for_agent else {},
    )
