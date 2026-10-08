"""Procedural graphs — the executable-structure substrate for the procedure plane.

A procedure is a graph: nodes = steps, edges = dependency/dataflow, explicit
memory & consolidation nodes, and successful topologies archived as reusable
templates. loom's role is the DATA PLANE:
it stores, serves, evolves, and templatises the procedural knowledge an agent
executes — it never runs the graph itself. The agent executes and reports what
happened (`procedure.record_run`); loom folds those traces into edge/node
statistics and, on `procedure.evolve`, consolidates them into an updated graph
(reinforce the proven path, flag dead ends) and promotes stable, well-exercised
procedures to reusable templates.

Everything here is pure and I/O-free so it is exhaustively unit-testable; the
handlers (server/memory/app/procedure.py) own persistence via the
memory plane. A procedure graph is stored verbatim as JSON on the procedure
memory row's `value.graph` — no schema migration.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --- limits (defensive caps; a procedure graph is small by construction) ---
MAX_NODES = 256  # a real tool-catalog graph is 131 nodes
MAX_EDGES = 1024
MAX_PATH = 128
MAX_RUNS_KEPT = 50  # bounded trace log; aggregate run_stats survives truncation
MAX_ATTR = 512  # per edge attribute (condition / guidance / pitfalls)

# Outcome-contrast thresholds for evolve(). A node or transition is flagged
# fail-prone when it has enough evidence AND its failure share exceeds the
# procedure's base failure rate by the margin: it appears disproportionately
# on failed runs, not merely on runs that happened to fail downstream.
MIN_CONTRAST_RUNS = 3
CONTRAST_MARGIN = 0.25
# The margin shrinks as the procedure's own failure rate rises (a dynamic
# threshold τ·(1 − λ·T_global)): margin = CONTRAST_MARGIN · (1 − λ·base).
# λ = 1 would scale it with the room left above the base, λ = 0 is a fixed
# margin; at 0.5 a step that
# fails four of four on a half-failing procedure is flagged (bound 0.69 over
# a 0.69 bar) and three of three on a clean one is (0.64 over 0.25), while
# one of three on a clean one (0.20) is not.
CONTRAST_LAMBDA = 0.5

# `action` = a node that IS a tool call; its `tool` field (or id) names the
# tool so an agent's last call localises onto it by exact match, no LLM.
NodeKind = Literal["step", "action", "memory", "consolidation", "decision", "template_ref"]
# `converge` = a join: several branches lead here (the fourth relation in the
# procedural-graph vocabulary, next to leads-to / provides-input / triggers).
EdgeKind = Literal["dep", "dataflow", "branch", "converge"]


class NodeStats(BaseModel):
    runs: int = 0  # times this node appeared on a reported execution path
    ok: int = 0  # of those, how many were on a successful run
    fail: int = 0


class GraphNode(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    kind: NodeKind = "step"
    text: str = Field(default="", max_length=2048)
    # Tool / function name this node stands for (kind="action"). Lets
    # `localize()` match an agent's most recent tool call to a node exactly.
    tool: str = Field(default="", max_length=128)
    stats: NodeStats = Field(default_factory=NodeStats)
    # Set by evolve() when the node was never traversed across enough runs.
    dead_end: bool = False
    # Set by evolve() when the node's failure share exceeds the procedure's
    # base failure rate by CONTRAST_MARGIN over >= MIN_CONTRAST_RUNS runs.
    fail_prone: bool = False


class GraphEdge(BaseModel):
    src: str = Field(min_length=1, max_length=64)
    dst: str = Field(min_length=1, max_length=64)
    kind: EdgeKind = "dep"
    # Traversal count folded in from reported runs (ok + fail): the total;
    # the outcome split below is what ranking uses.
    weight: float = 0.0
    ok_weight: float = 0.0
    fail_weight: float = 0.0
    # Textual attributes an agent (or a guidance model) reads when this
    # transition is in its local neighbourhood: when it applies, how to take
    # it, what to avoid. Optional; a bare topology is still valid.
    condition: str = Field(default="", max_length=MAX_ATTR)
    guidance: str = Field(default="", max_length=MAX_ATTR)
    pitfalls: str = Field(default="", max_length=MAX_ATTR)
    # Set by evolve(): this transition is disproportionately on failed runs.
    fail_prone: bool = False


class ProcedureGraph(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class RunTrace(BaseModel):
    """One reported execution of the procedure by the agent (loom stores it; it
    does not produce it). `path` is the ordered node ids the agent actually
    visited; `ok` is whether the run achieved its goal."""

    path: list[str] = Field(default_factory=list, max_length=MAX_PATH)
    ok: bool = True
    note: str = Field(default="", max_length=512)
    ts: str = ""
    # What the run cost; 0 = not reported.
    steps: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)


class RunStats(BaseModel):
    total: int = 0
    ok: int = 0


class GraphValidationError(ValueError):
    """Raised when a caller-supplied graph is structurally invalid."""


class LocalTransition(BaseModel):
    """One outgoing transition in an agent's local neighbourhood, with the
    edge's attributes and outcome statistics flattened for direct reading."""

    src: str
    dst: str
    kind: EdgeKind = "dep"
    dst_kind: NodeKind = "step"
    dst_text: str = ""
    dst_tool: str = ""
    condition: str = ""
    guidance: str = ""
    pitfalls: str = ""
    ok: int = 0
    fail: int = 0
    fail_prone: bool = False
    # On the proven (dominant) path from the active node.
    proven: bool = False
    # The destination was already visited in this run (recent_path).
    visited: bool = False


class LocalContext(BaseModel):
    """What an agent standing at one node of the procedure can see: the node,
    then its outgoing transitions hop by hop up to `hops` away. Serving this
    instead of the whole graph is what makes procedure graphs pay off: a
    localized subgraph beats the full graph at a fraction of the tokens."""

    matched: bool = False
    active: GraphNode | None = None
    # hops[0] = transitions one step ahead, hops[1] = two steps ahead, ...
    hops: list[list[LocalTransition]] = Field(default_factory=list)
    # When `at` matched nothing: where the procedure starts (in-degree 0).
    entry_nodes: list[str] = Field(default_factory=list)
    # Next node ids along the proven path from the active node.
    dominant_next: list[str] = Field(default_factory=list)
    # Trajectory window (recent_path): how many times each node was visited
    # in this run, whether the active node is being revisited, and the
    # untaken exits from it. An agent that repeats unproductive actions is the
    # failure this catches; this is where loom says so.
    revisits: dict[str, int] = Field(default_factory=dict)
    looping: bool = False
    exits: list[str] = Field(default_factory=list)


def normalize_graph(raw: dict[str, object] | ProcedureGraph | None) -> ProcedureGraph:
    """Validate and canonicalise a caller-supplied graph.

    Enforces: node-id uniqueness, every edge endpoint resolves to a node, kind
    enums, size caps, and that every node has a directed path to a terminal
    node (out-degree 0). Cycles are permitted — a retry/loop edge is a real
    execution structure — as long as the loop has an exit; a cycle no path
    leaves is a procedure that cannot finish and is rejected. Downstream code
    is still cycle-safe (live_steps and dominant_path never loop). Raises
    GraphValidationError on a structural problem so the operation layer can
    400 rather than persist a corrupt graph.
    """
    if raw is None:
        return ProcedureGraph()
    graph = raw if isinstance(raw, ProcedureGraph) else ProcedureGraph.model_validate(raw)

    if len(graph.nodes) > MAX_NODES:
        raise GraphValidationError(f"too many nodes ({len(graph.nodes)} > {MAX_NODES})")
    if len(graph.edges) > MAX_EDGES:
        raise GraphValidationError(f"too many edges ({len(graph.edges)} > {MAX_EDGES})")

    ids: set[str] = set()
    for n in graph.nodes:
        if n.id in ids:
            raise GraphValidationError(f"duplicate node id {n.id!r}")
        ids.add(n.id)
    if not ids and graph.edges:
        raise GraphValidationError("edges present but no nodes")
    for e in graph.edges:
        if e.src not in ids:
            raise GraphValidationError(f"edge src {e.src!r} is not a node")
        if e.dst not in ids:
            raise GraphValidationError(f"edge dst {e.dst!r} is not a node")
        if e.src == e.dst:
            raise GraphValidationError(f"self-loop edge on {e.src!r}")
    stranded = _nodes_without_terminal_path(graph)
    if stranded:
        raise GraphValidationError(f"no path to a terminal node from {sorted(stranded)[:5]!r}")
    return graph


def _nodes_without_terminal_path(graph: ProcedureGraph) -> set[str]:
    """Node ids that cannot reach any sink (out-degree 0). Reverse BFS from
    the sinks; whatever is unreached is stranded in an exit-less cycle."""
    ids = [n.id for n in graph.nodes]
    if not ids:
        return set()
    outdeg = dict.fromkeys(ids, 0)
    rev: dict[str, list[str]] = {i: [] for i in ids}
    for e in graph.edges:
        if e.src in outdeg and e.dst in rev:
            outdeg[e.src] += 1
            rev[e.dst].append(e.src)
    frontier = [i for i in ids if outdeg[i] == 0]
    reached = set(frontier)
    while frontier:
        cur = frontier.pop()
        for prev in rev[cur]:
            if prev not in reached:
                reached.add(prev)
                frontier.append(prev)
    return set(ids) - reached


def steps_to_graph(steps: list[str]) -> ProcedureGraph:
    """Build a linear graph (n1 -> n2 -> ...) from flat steps. Used to give a
    flat procedure a graph substrate so evolve/record_run apply uniformly."""
    nodes = [GraphNode(id=f"n{i + 1}", kind="step", text=str(s)) for i, s in enumerate(steps)]
    edges = [GraphEdge(src=f"n{i + 1}", dst=f"n{i + 2}", kind="dep") for i in range(len(steps) - 1)]
    return ProcedureGraph(nodes=nodes, edges=edges)


def _topo_order(graph: ProcedureGraph) -> list[str]:
    """Kahn topological order over all nodes; any node left by a cycle is
    appended in insertion order so the function is total (never drops a node)."""
    node_ids = [n.id for n in graph.nodes]
    indeg = {i: 0 for i in node_ids}
    adj: dict[str, list[str]] = {i: [] for i in node_ids}
    for e in graph.edges:
        if e.src in indeg and e.dst in indeg:
            adj[e.src].append(e.dst)
            indeg[e.dst] += 1
    # Preserve insertion order among ready nodes (stable, deterministic).
    ready = [i for i in node_ids if indeg[i] == 0]
    out: list[str] = []
    seen: set[str] = set()
    while ready:
        cur = ready.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        out.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    # Cycle remnants: append any unseen node in insertion order.
    for i in node_ids:
        if i not in seen:
            out.append(i)
    return out


def merge_run(
    graph: ProcedureGraph,
    stats: RunStats,
    runs: list[RunTrace],
    trace: RunTrace,
) -> tuple[ProcedureGraph, RunStats, list[RunTrace]]:
    """Fold one reported execution into the graph's node/edge statistics.

    - Each visited node: runs += 1, and ok/fail += 1 per the run outcome.
    - Each consecutive (a, b) on the path reinforces edge a->b: `weight` is
      the traversal total, and `ok_weight` / `fail_weight` split it by the
      run's outcome so ranking can prefer transitions that were on successful
      runs (an edge that doesn't yet exist is created as a `dep` edge — the
      agent demonstrated a transition the authored graph didn't have).
    - Aggregate run_stats advance; the bounded trace log keeps the last N.

    Node ids on the path that don't exist in the graph are ignored (an agent
    may report a transient step loom doesn't model). Pure: returns new objects.
    """
    nodes = {n.id: n.model_copy(deep=True) for n in graph.nodes}
    edges = {(e.src, e.dst): e.model_copy(deep=True) for e in graph.edges}

    visited = [nid for nid in trace.path if nid in nodes]
    for nid in visited:
        st = nodes[nid].stats
        st.runs += 1
        if trace.ok:
            st.ok += 1
        else:
            st.fail += 1
    for a, b in zip(trace.path, trace.path[1:], strict=False):
        if a not in nodes or b not in nodes or a == b:
            continue
        edge = edges.get((a, b))
        if edge is None:
            edge = GraphEdge(src=a, dst=b, kind="dep", weight=0.0)
            edges[(a, b)] = edge
        edge.weight += 1.0
        if trace.ok:
            edge.ok_weight += 1.0
        else:
            edge.fail_weight += 1.0

    new_graph = ProcedureGraph(
        nodes=[nodes[n.id] for n in graph.nodes],
        edges=list(edges.values()),
    )
    new_stats = RunStats(total=stats.total + 1, ok=stats.ok + (1 if trace.ok else 0))
    new_runs = [*runs, trace][-MAX_RUNS_KEPT:]
    return new_graph, new_stats, new_runs


def find_node(graph: ProcedureGraph, at: str) -> GraphNode | None:
    """Locate a node from an agent's last step: exact id, then exact `tool`
    name (an action node stands for a tool call), then either
    case-insensitively. None when nothing matches."""
    key = (at or "").strip()
    if not key:
        return None
    for n in graph.nodes:
        if n.id == key:
            return n
    for n in graph.nodes:
        if n.tool and n.tool == key:
            return n
    low = key.lower()
    for n in graph.nodes:
        if n.id.lower() == low or (n.tool and n.tool.lower() == low):
            return n
    return None


def localize(
    graph: ProcedureGraph,
    at: str | None,
    hops: int = 2,
    recent_path: list[str] | None = None,
) -> LocalContext:
    """The k-hop outgoing neighbourhood of the node `at` (id or tool name).
    Dead-end nodes are not expanded into and not listed: evolve demoted them.
    When `at` matches nothing the context is unmatched and carries the
    graph's entry nodes so the caller can start (loom's callers already hold
    the full graph).

    `recent_path` is the agent's trajectory window for this run (node ids or
    tool names). It marks transitions whose destination was already visited,
    counts revisits, and flags `looping` when the active node itself has been
    visited before, listing the untaken `exits` from it."""
    hops = max(1, min(int(hops), 4))
    nodes = {n.id: n for n in graph.nodes}
    active = find_node(graph, at or "")
    # Resolve the window onto node ids (tool names are accepted like `at`).
    window: list[str] = []
    for step in recent_path or []:
        n = find_node(graph, str(step))
        if n is not None:
            window.append(n.id)
    revisits: dict[str, int] = {}
    for nid in window:
        revisits[nid] = revisits.get(nid, 0) + 1
    if active is None:
        indeg = dict.fromkeys(nodes, 0)
        for e in graph.edges:
            if e.dst in indeg:
                indeg[e.dst] += 1
        entry = [i for i, d in indeg.items() if d == 0 and not nodes[i].dead_end]
        return LocalContext(matched=False, entry_nodes=entry, revisits=revisits)

    spine = _greedy_from(graph, active.id)
    # The active node is in the window at least once when the caller passed
    # it as the last step; a second occurrence is a revisit.
    prior_visits = revisits.get(active.id, 0)
    if window and window[-1] == active.id:
        prior_visits -= 1
    looping = prior_visits >= 1
    visited_set = set(window)
    on_spine = {(a, b) for a, b in zip(spine, spine[1:], strict=False)}
    out_edges: dict[str, list[GraphEdge]] = {}
    for e in graph.edges:
        out_edges.setdefault(e.src, []).append(e)

    layers: list[list[LocalTransition]] = []
    frontier = [active.id]
    seen = {active.id}
    for _ in range(hops):
        layer: list[LocalTransition] = []
        nxt: list[str] = []
        for src in frontier:
            for e in sorted(out_edges.get(src, []), key=lambda e: (-e.ok_weight, -e.weight, e.dst)):
                dst = nodes.get(e.dst)
                if dst is None or dst.dead_end:
                    continue
                layer.append(
                    LocalTransition(
                        src=e.src,
                        dst=e.dst,
                        kind=e.kind,
                        dst_kind=dst.kind,
                        dst_text=dst.text,
                        dst_tool=dst.tool,
                        condition=e.condition,
                        guidance=e.guidance,
                        pitfalls=e.pitfalls,
                        ok=int(e.ok_weight),
                        fail=int(e.fail_weight),
                        fail_prone=e.fail_prone or dst.fail_prone,
                        proven=(e.src, e.dst) in on_spine,
                        visited=e.dst in visited_set,
                    )
                )
                if e.dst not in seen:
                    seen.add(e.dst)
                    nxt.append(e.dst)
        if not layer:
            break
        layers.append(layer)
        frontier = nxt
        if not frontier:
            break
    exits: list[str] = []
    if looping and layers:
        # Untaken ways out of the active node, proven first, then by success.
        first = [t for t in layers[0] if t.src == active.id and not t.visited]
        first.sort(key=lambda t: (not t.proven, -t.ok, t.dst))
        exits = [t.dst for t in first]
    return LocalContext(
        matched=True,
        active=active,
        hops=layers,
        dominant_next=spine[1:],
        revisits=revisits,
        looping=looping,
        exits=exits,
    )


def render_local_context(ctx: LocalContext, *, name: str = "") -> str:
    """Serialise a LocalContext as the text an agent reads: active node
    header, then each hop's transitions with condition / guidance / pitfalls
    and outcome counts, plus loom's outcome statistics and contrast flags.
    Deterministic."""
    lines: list[str] = []
    title = f" for {name}" if name else ""
    if not ctx.matched or ctx.active is None:
        lines.append(f"No active node matched{title}.")
        if ctx.entry_nodes:
            lines.append("Entry points: " + ", ".join(f"[{i}]" for i in ctx.entry_nodes))
        return "\n".join(lines)
    a = ctx.active
    head = f"Active node{title}: [{a.id}]"
    if a.kind != "step":
        head += f" ({a.kind}{': ' + a.tool if a.tool else ''})"
    if a.text:
        head += f" — {a.text}"
    lines.append(head)
    if a.fail_prone:
        lines.append("  note: this step sits disproportionately on failed runs")
    if ctx.looping:
        times = ctx.revisits.get(a.id, 0)
        warn = f"  WARNING: you have been at [{a.id}] {times} times in this run"
        if ctx.exits:
            warn += "; untaken exits: " + ", ".join(f"[{x}]" for x in ctx.exits)
        else:
            warn += "; every exit from here has already been taken — stop and reassess"
        lines.append(warn)
    for depth, layer in enumerate(ctx.hops, start=1):
        label = "Next transitions (hop 1)" if depth == 1 else f"Horizon (hop {depth})"
        lines.append(f"{label}:")
        for t in layer:
            tags = []
            if t.proven:
                tags.append("proven")
            if t.visited:
                tags.append("already visited this run")
            if t.fail_prone:
                tags.append(f"fail-prone: failed {t.fail} of {t.ok + t.fail}")
            elif t.ok or t.fail:
                tags.append(f"ok {t.ok} / fail {t.fail}")
            tag = f" [{'; '.join(tags)}]" if tags else ""
            target = f"[{t.dst}]"
            if t.dst_tool:
                target += f" ({t.dst_tool})"
            if t.dst_text:
                target += f" — {t.dst_text}"
            lines.append(f"- [{t.src}] -> {target} ({t.kind}){tag}")
            if t.condition:
                lines.append(f"    condition: {t.condition}")
            if t.guidance:
                lines.append(f"    guidance: {t.guidance}")
            if t.pitfalls:
                lines.append(f"    pitfalls: {t.pitfalls}")
    if ctx.dominant_next:
        lines.append("Proven path from here: " + " -> ".join(f"[{i}]" for i in ctx.dominant_next))
    return "\n".join(lines)


def _greedy_from(graph: ProcedureGraph, start: str) -> list[str]:
    """Greedy proven path from `start`: follow the outgoing edge with the most
    successful traversals (total as tie-break), visited-guarded."""
    out_edges: dict[str, list[GraphEdge]] = {}
    for e in graph.edges:
        out_edges.setdefault(e.src, []).append(e)
    path = [start]
    seen = {start}
    cur = start
    while True:
        outs = sorted(
            (e for e in out_edges.get(cur, []) if e.dst not in seen and e.weight > 0),
            key=lambda e: (-e.ok_weight, -e.weight, e.dst),
        )
        if not outs:
            break
        cur = outs[0].dst
        path.append(cur)
        seen.add(cur)
    return path


def dominant_path(graph: ProcedureGraph) -> list[str]:
    """The proven acyclic path through the graph: greedy from the outgoing edge
    with the most SUCCESSFUL traversals (`ok_weight`), total traversals as the
    tie-break, visited-guarded so it terminates on cyclic graphs. Ranking on
    ok_weight rather than weight is what keeps a heavily-retried failure loop
    from being reported as the spine. A graph with ok_weight 0 everywhere
    falls back to total weight."""
    if not graph.nodes:
        return []
    indeg: dict[str, float] = {n.id: 0.0 for n in graph.nodes}
    for e in graph.edges:
        if e.dst in indeg:
            indeg[e.dst] += e.weight
    # Start at the reinforced source with no inbound weight; else first node.
    starts = [n.id for n in graph.nodes if indeg.get(n.id, 0.0) == 0.0]
    start = starts[0] if starts else graph.nodes[0].id
    return _greedy_from(graph, start)


def contrast_threshold(base_fail_rate: float) -> float:
    """The failure rate a node or transition must have *established* to be
    flagged: the procedure's base rate plus a margin that tightens with it."""
    base = min(1.0, max(0.0, float(base_fail_rate)))
    return base + CONTRAST_MARGIN * (1.0 - CONTRAST_LAMBDA * base)


def _fail_prone(runs: float, fails: float, base_fail_rate: float) -> bool:
    """Outcome contrast: enough evidence AND an *established* failure share
    that beats the procedure's base failure rate by the margin. Comparing
    against the base rate (not zero) matters: every node on a failed path
    counts one failure, so on a procedure that fails half the time every node
    has a 50% share and nothing is singled out; only the node or transition
    that fails MORE than the procedure does carries signal.

    "Established" is the lower confidence bound of a Beta posterior on the
    failure rate (mean − sd), not the point estimate: one
    failure in three runs is a 0.33 share but a 0.20 bound, and does not
    flag a step on a procedure that otherwise succeeds. Three of three
    (bound 0.64) does; at a 50% base it takes four of four (0.69 over a
    0.69 bar)."""
    from server.memory.domain.procedure_evidence import failure_lcb_counts

    if runs < MIN_CONTRAST_RUNS:
        return False
    threshold = contrast_threshold(base_fail_rate)
    if threshold > 1.0:
        # The procedure itself fails so often that nothing can stand out
        # above it by the margin; there is no contrast to report.
        return False
    return failure_lcb_counts(runs, fails) >= threshold


def success_retention(runs: list[RunTrace]) -> dict[str, float]:
    """Success retention: P(ok at t+1 | ok at t)
    over the trace log in order — does a success reproduce, or was it luck?
    Returns {pairs, ok_after_ok, rate}; rate is 0 with no ok→next pair."""
    pairs = 0
    both = 0
    for prev, cur in zip(runs, runs[1:], strict=False):
        if prev.ok:
            pairs += 1
            both += int(cur.ok)
    return {
        "pairs": float(pairs),
        "ok_after_ok": float(both),
        "rate": round(both / pairs, 4) if pairs else 0.0,
    }


def trailing_failures(runs: list[RunTrace]) -> int:
    """Consecutive failed runs at the end of the trace log — the strike count.
    A success resets it to zero."""
    n = 0
    for t in reversed(runs):
        if t.ok:
            break
        n += 1
    return n


def evolve_graph(
    graph: ProcedureGraph,
    stats: RunStats,
    min_runs: int = 3,
) -> tuple[ProcedureGraph, bool]:
    """Consolidate accumulated traces into an evolved graph (the procedure-aware
    consolidation primitive: keep the proven path, flag the dead ends, flag
    what fails).

    Only acts once at least `min_runs` runs have been folded in — below that the
    evidence is too thin to prune. Two flags, both reversible on new evidence:

    - `dead_end`: a step/action node never traversed across all runs (kept for
      provenance, demoted from the linear view).
    - `fail_prone`: a node or transition whose failure share exceeds the
      procedure's base failure rate by CONTRAST_MARGIN over at least
      MIN_CONTRAST_RUNS traversals — the statistical form of contrasting failed
      trajectories with successful ones. Kept in the linear view (presence on
      failed runs is correlation, not cause) but surfaced to the agent as a
      pitfall in the localized view and to evolve's caller.

    Returns (evolved_graph, changed) — `changed=False` means the topology and
    flags are stable, one of the template-readiness signals.
    """
    if stats.total < min_runs:
        return graph, False
    base_fail = (stats.total - stats.ok) / stats.total if stats.total else 0.0
    changed = False
    nodes: list[GraphNode] = []
    for n in graph.nodes:
        nn = n.model_copy(deep=True)
        should_flag = nn.kind in ("step", "action") and nn.stats.runs == 0
        if should_flag != nn.dead_end:
            nn.dead_end = should_flag
            changed = True
        prone = _fail_prone(nn.stats.runs, nn.stats.fail, base_fail)
        if prone != nn.fail_prone:
            nn.fail_prone = prone
            changed = True
        nodes.append(nn)
    edges: list[GraphEdge] = []
    for e in graph.edges:
        ne = e.model_copy(deep=True)
        prone = _fail_prone(ne.weight, ne.fail_weight, base_fail)
        if prone != ne.fail_prone:
            ne.fail_prone = prone
            changed = True
        edges.append(ne)
    return ProcedureGraph(nodes=nodes, edges=edges), changed


def live_steps(graph: ProcedureGraph) -> list[str]:
    """Steps for the linear view after evolution: topological order minus the
    dead-end nodes evolve() flagged."""
    dead = {n.id for n in graph.nodes if n.dead_end}
    text_by_id = {n.id: n.text for n in graph.nodes if n.kind in ("step", "action")}
    return [
        text_by_id[i]
        for i in _topo_order(graph)
        if i in text_by_id and text_by_id[i] and i not in dead
    ]


GateVerdict = Literal["accept", "revert", "undecided"]

# Acceptance gate for a revised topology. A revision is judged
# on the runs that executed against it versus the runs against the revision
# it replaced; both sides need MIN_GATE_RUNS before a verdict, and a drop of
# more than GATE_MARGIN in success rate reverts. Ties and small drops accept:
# with tens of runs a single episode moves the rate by several points, and
# a tie is no evidence against the revision.
MIN_GATE_RUNS = 5
GATE_MARGIN = 0.05


def gate_revision(
    before: RunStats,
    after: RunStats,
    *,
    min_runs: int = MIN_GATE_RUNS,
    margin: float = GATE_MARGIN,
) -> GateVerdict:
    """Compare success against the previous revision with success against the
    current one. `undecided` until `after` has `min_runs` runs; with no
    baseline (`before` empty) the revision is accepted once it has evidence."""
    if after.total < min_runs:
        return "undecided"
    if before.total < min_runs:
        return "accept"
    before_rate = before.ok / before.total
    after_rate = after.ok / after.total
    return "revert" if after_rate < before_rate - margin else "accept"


# Sequential gate. Runs against a revision arrive one at a
# time and evolve may be called after every one of them; a fixed-horizon
# comparison peeked at repeatedly inflates the chance of a wrong verdict, and
# a fixed margin at n=5 is a coin flip. Wald's sequential probability ratio
# test is the classical answer: accumulate the log-likelihood ratio of "the
# success rate dropped by at least MIN_DROP" against "it did not", stop as
# soon as the evidence crosses either boundary, and keep waiting otherwise.
SPRT_MIN_DROP = 0.15  # the drop worth reverting over
SPRT_ALPHA = 0.05  # P(revert | no drop)
SPRT_BETA = 0.20  # P(accept | drop of MIN_DROP)
SPRT_MAX_RUNS = 50  # force a verdict by point estimate past this many runs


def sprt_llr(before: RunStats, after: RunStats, *, min_drop: float = SPRT_MIN_DROP) -> float:
    """Log-likelihood ratio of H1 (after-rate = p0 - min_drop) against H0
    (after-rate = p0), where p0 is the Laplace-smoothed prior success rate.
    Positive favours the drop. Closed form from counts: the SPRT statistic for
    Bernoulli outcomes is a sum over runs, and the sum depends only on how
    many succeeded and how many failed."""
    import math

    p0 = (before.ok + 1.0) / (before.total + 2.0)
    p0 = min(0.98, max(0.02, p0))
    p1 = max(0.01, p0 - min_drop)
    ok = float(after.ok)
    fail = float(after.total - after.ok)
    return ok * math.log(p1 / p0) + fail * math.log((1.0 - p1) / (1.0 - p0))


def gate_revision_sprt(
    before: RunStats,
    after: RunStats,
    *,
    min_runs: int = 3,
    min_drop: float = SPRT_MIN_DROP,
    alpha: float = SPRT_ALPHA,
    beta: float = SPRT_BETA,
    max_runs: int = SPRT_MAX_RUNS,
) -> tuple[GateVerdict, float]:
    """Sequential verdict on a pending revision. Returns (verdict, llr).

    - fewer than `min_runs` on the new side: undecided (no evidence yet);
    - no baseline (`before` below `min_runs`): accept once evidenced, as the
      margin gate does;
    - llr >= ln((1-beta)/alpha): revert (the drop is established);
    - llr <= ln(beta/(1-alpha)): accept (a drop of `min_drop` is ruled out);
    - otherwise undecided, until `max_runs`, where the point estimate decides
      (revert if the rate fell by more than half of `min_drop`)."""
    import math

    if after.total < min_runs:
        return "undecided", 0.0
    if before.total < min_runs:
        return "accept", 0.0
    llr = sprt_llr(before, after, min_drop=min_drop)
    upper = math.log((1.0 - beta) / alpha)
    lower = math.log(beta / (1.0 - alpha))
    if llr >= upper:
        return "revert", llr
    if llr <= lower:
        return "accept", llr
    if after.total >= max_runs:
        before_rate = before.ok / before.total
        after_rate = after.ok / after.total
        return ("revert" if after_rate < before_rate - min_drop / 2 else "accept"), llr
    return "undecided", llr


class RevisionCost(BaseModel):
    """Median cost of the *successful* runs against one revision, from the
    ledger. `samples` counts the ok runs that reported any
    cost; a revision with none has nothing to compare."""

    samples: int = 0
    p50_steps: float = 0.0
    p50_duration_ms: float = 0.0
    p50_tokens: float = 0.0


COST_TOLERANCE = 0.25  # a revision may cost this much more before it loses a tie


def cost_verdict(
    before: RevisionCost,
    after: RevisionCost,
    *,
    min_samples: int = 3,
    tolerance: float = COST_TOLERANCE,
) -> str | None:
    """When the success gate accepts a revision, ask what it cost. Returns
    "costlier" when the revision's successful runs are more than
    `tolerance` more expensive than the prior's on the first dimension both
    sides reported (duration, then steps, then tokens), "cheaper" when they
    are that much less, None when there is nothing to say — too few costed
    runs on either side, or within tolerance. A revision should replace its
    predecessor only if it succeeds AND is shorter."""
    if before.samples < min_samples or after.samples < min_samples:
        return None
    for dim in ("p50_duration_ms", "p50_steps", "p50_tokens"):
        b = float(getattr(before, dim))
        a = float(getattr(after, dim))
        if b <= 0.0 or a <= 0.0:
            continue
        if a > b * (1.0 + tolerance):
            return "costlier"
        if a < b * (1.0 - tolerance):
            return "cheaper"
        return None
    return None


def graph_digest(graph: ProcedureGraph) -> str:
    """Stable hash of the TOPOLOGY and attributes (ids, kinds, texts, tools,
    edges and their condition/guidance/pitfalls), excluding statistics and
    evolve flags. Two proposals of the same graph have the same digest, which
    is how a rejected topology is recognised when it is proposed again."""
    import hashlib
    import json

    canon = {
        "nodes": sorted((n.id, n.kind, n.text, n.tool) for n in graph.nodes),
        "edges": sorted(
            (e.src, e.dst, e.kind, e.condition, e.guidance, e.pitfalls) for e in graph.edges
        ),
    }
    return hashlib.sha256(json.dumps(canon, ensure_ascii=False).encode()).hexdigest()[:16]


def carry_stats(old: ProcedureGraph, new: ProcedureGraph) -> ProcedureGraph:
    """Copy node statistics and edge weights from `old` onto the nodes and
    edges of `new` that share ids / endpoints, so a revision keeps the
    evidence for the parts it did not change. Flags are recomputed by the
    next evolve, so they are not carried."""
    old_nodes = {n.id: n for n in old.nodes}
    old_edges = {(e.src, e.dst): e for e in old.edges}
    nodes = []
    for n in new.nodes:
        nn = n.model_copy(deep=True)
        prev = old_nodes.get(n.id)
        if prev is not None:
            nn.stats = prev.stats.model_copy(deep=True)
        nn.dead_end = False
        nn.fail_prone = False
        nodes.append(nn)
    edges = []
    for e in new.edges:
        ne = e.model_copy(deep=True)
        prev_edge = old_edges.get((e.src, e.dst))
        if prev_edge is not None:
            ne.weight = prev_edge.weight
            ne.ok_weight = prev_edge.ok_weight
            ne.fail_weight = prev_edge.fail_weight
        ne.fail_prone = False
        edges.append(ne)
    return ProcedureGraph(nodes=nodes, edges=edges)


def reset_evidence(graph: ProcedureGraph) -> ProcedureGraph:
    """The topology and attributes of `graph` with every statistic and flag
    cleared: node runs/ok/fail, edge weights, dead_end and fail_prone. What a
    fork copies: the shape of a proven procedure travels; the
    evidence that proved it stays where it was earned."""
    nodes = [
        n.model_copy(update={"stats": NodeStats(), "dead_end": False, "fail_prone": False})
        for n in graph.nodes
    ]
    edges = [
        e.model_copy(
            update={"weight": 0.0, "ok_weight": 0.0, "fail_weight": 0.0, "fail_prone": False}
        )
        for e in graph.edges
    ]
    return ProcedureGraph(nodes=nodes, edges=edges)


def utility_from_stats(total: int, ok: int, *, prior: float = 0.5) -> float:
    """What a procedure's runs proved, as a signed number in (-0.5, 0.5):
    the Laplace-smoothed success rate minus `prior`.

    (ok + 1) / (total + 2) keeps an unrun procedure at exactly 0 and lets one
    run move it by at most a sixth, so a single lucky or unlucky report never
    outranks similarity; the sign is what matters (uniform weights lose most
    of the gain). `prior` is the success
    rate a caller treats as neutral; 0.5 is the uninformative default and a
    namespace's own base rate is the better one when it is known.
    """
    t = max(0, int(total or 0))
    k = min(max(0, int(ok or 0)), t)
    return round((k + 1.0) / (t + 2.0) - float(prior), 6)


def is_template_ready(
    graph: ProcedureGraph,
    stats: RunStats,
    min_ok_runs: int = 5,
    min_success_rate: float = 0.8,
) -> bool:
    """A procedure is template-ready when it has been exercised enough, mostly
    succeeds, and its topology has stabilised (evolve makes no further change).
    A template is loom's analogue of an archived successful execution topology —
    a 'workflow context pack' retrievable and reusable for recurring tasks."""
    if stats.ok < min_ok_runs or stats.total == 0:
        return False
    if stats.ok / stats.total < min_success_rate:
        return False
    _, changed = evolve_graph(graph, stats, min_runs=1)
    return not changed


__all__ = [
    "ProcedureGraph",
    "GraphNode",
    "GraphEdge",
    "NodeStats",
    "RunTrace",
    "RunStats",
    "GraphValidationError",
    "LocalTransition",
    "LocalContext",
    "find_node",
    "localize",
    "render_local_context",
    "normalize_graph",
    "steps_to_graph",
    "merge_run",
    "dominant_path",
    "evolve_graph",
    "live_steps",
    "is_template_ready",
    "MAX_NODES",
    "MAX_EDGES",
    "MAX_RUNS_KEPT",
    "MAX_ATTR",
    "MIN_CONTRAST_RUNS",
    "CONTRAST_MARGIN",
    "GateVerdict",
    "MIN_GATE_RUNS",
    "GATE_MARGIN",
    "gate_revision",
    "SPRT_MIN_DROP",
    "SPRT_ALPHA",
    "SPRT_BETA",
    "SPRT_MAX_RUNS",
    "sprt_llr",
    "gate_revision_sprt",
    "graph_digest",
    "carry_stats",
    "reset_evidence",
]
