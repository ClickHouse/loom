"""Unit tests for the procedural-graph domain logic (pure, no I/O)."""

from __future__ import annotations

import pytest

from server.memory.domain.procedure_graph import (
    GraphEdge,
    GraphNode,
    GraphValidationError,
    ProcedureGraph,
    RunStats,
    RunTrace,
    dominant_path,
    evolve_graph,
    is_template_ready,
    live_steps,
    merge_run,
    normalize_graph,
    steps_to_graph,
)


def _linear(n: int) -> ProcedureGraph:
    return steps_to_graph([f"step {i}" for i in range(1, n + 1)])


# --- normalize_graph -------------------------------------------------------


def test_normalize_none_is_empty():
    g = normalize_graph(None)
    assert g.nodes == [] and g.edges == []


def test_normalize_accepts_valid_dict():
    g = normalize_graph(
        {
            "nodes": [
                {"id": "a", "kind": "step", "text": "x"},
                {"id": "b", "kind": "memory", "text": "recall"},
            ],
            "edges": [{"src": "a", "dst": "b", "kind": "dataflow"}],
        }
    )
    assert len(g.nodes) == 2 and len(g.edges) == 1
    assert g.edges[0].kind == "dataflow"


def test_normalize_rejects_duplicate_node_id():
    with pytest.raises(GraphValidationError, match="duplicate node id"):
        normalize_graph({"nodes": [{"id": "a"}, {"id": "a"}], "edges": []})


def test_normalize_rejects_dangling_edge():
    with pytest.raises(GraphValidationError, match="not a node"):
        normalize_graph({"nodes": [{"id": "a"}], "edges": [{"src": "a", "dst": "z"}]})


def test_normalize_rejects_self_loop():
    with pytest.raises(GraphValidationError, match="self-loop"):
        normalize_graph({"nodes": [{"id": "a"}], "edges": [{"src": "a", "dst": "a"}]})


def test_normalize_rejects_edges_without_nodes():
    with pytest.raises(GraphValidationError, match="no nodes"):
        normalize_graph({"nodes": [], "edges": [{"src": "a", "dst": "b"}]})


# --- steps <-> graph -------------------------------------------------------


def test_steps_to_graph_is_linear_chain():
    g = steps_to_graph(["one", "two", "three"])
    assert [n.id for n in g.nodes] == ["n1", "n2", "n3"]
    assert [(e.src, e.dst) for e in g.edges] == [("n1", "n2"), ("n2", "n3")]


def test_live_steps_roundtrip():
    assert live_steps(steps_to_graph(["a", "b", "c"])) == ["a", "b", "c"]


def test_live_steps_omits_non_step_nodes():
    g = ProcedureGraph(
        nodes=[
            GraphNode(id="a", kind="step", text="do a"),
            GraphNode(id="m", kind="memory", text="recall"),
            GraphNode(id="b", kind="step", text="do b"),
        ],
        edges=[GraphEdge(src="a", dst="m"), GraphEdge(src="m", dst="b")],
    )
    assert live_steps(g) == ["do a", "do b"]


def test_topo_order_total_on_cycle():
    # a<->b cycle plus c: live_steps must still return every step node.
    g = ProcedureGraph(
        nodes=[
            GraphNode(id="a", kind="step", text="a"),
            GraphNode(id="b", kind="step", text="b"),
            GraphNode(id="c", kind="step", text="c"),
        ],
        edges=[
            GraphEdge(src="a", dst="b"),
            GraphEdge(src="b", dst="a"),
            GraphEdge(src="b", dst="c"),
        ],
    )
    assert sorted(live_steps(g)) == ["a", "b", "c"]


# --- merge_run -------------------------------------------------------------


def test_merge_run_increments_node_and_edge_stats():
    g = _linear(3)
    stats, runs = RunStats(), []
    g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2", "n3"], ok=True))
    by_id = {n.id: n for n in g.nodes}
    assert by_id["n1"].stats.runs == 1 and by_id["n1"].stats.ok == 1
    assert by_id["n2"].stats.fail == 0
    e = {(x.src, x.dst): x for x in g.edges}
    assert e[("n1", "n2")].weight == 1.0
    assert stats.total == 1 and stats.ok == 1
    assert len(runs) == 1


def test_merge_run_failure_counts_fail_not_ok():
    g = _linear(2)
    g, stats, _ = merge_run(g, RunStats(), [], RunTrace(path=["n1", "n2"], ok=False))
    by_id = {n.id: n for n in g.nodes}
    assert by_id["n1"].stats.fail == 1 and by_id["n1"].stats.ok == 0
    assert stats.ok == 0 and stats.total == 1


def test_merge_run_creates_unmodelled_transition_edge():
    # Agent jumped n1 -> n3 directly; that transition isn't in the linear graph.
    g = _linear(3)
    g, _, _ = merge_run(g, RunStats(), [], RunTrace(path=["n1", "n3"], ok=True))
    e = {(x.src, x.dst): x for x in g.edges}
    assert ("n1", "n3") in e and e[("n1", "n3")].weight == 1.0


def test_merge_run_ignores_unknown_node_ids():
    g = _linear(2)
    g, stats, _ = merge_run(g, RunStats(), [], RunTrace(path=["n1", "ghost"], ok=True))
    by_id = {n.id: n for n in g.nodes}
    assert by_id["n1"].stats.runs == 1
    assert all(n.stats.runs <= 1 for n in g.nodes)
    assert stats.total == 1


def test_merge_run_bounds_trace_log():
    from server.memory.domain.procedure_graph import MAX_RUNS_KEPT

    g, stats, runs = _linear(2), RunStats(), []
    for _ in range(MAX_RUNS_KEPT + 10):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2"], ok=True))
    assert len(runs) == MAX_RUNS_KEPT
    assert stats.total == MAX_RUNS_KEPT + 10  # aggregate survives truncation


# --- dominant_path ---------------------------------------------------------


def test_dominant_path_follows_reinforcement():
    # Diamond: n1 -> {n2, n3} -> n4. Reinforce the n1->n3->n4 branch harder.
    g = ProcedureGraph(
        nodes=[GraphNode(id=f"n{i}", text=f"s{i}") for i in range(1, 5)],
        edges=[
            GraphEdge(src="n1", dst="n2"),
            GraphEdge(src="n1", dst="n3"),
            GraphEdge(src="n2", dst="n4"),
            GraphEdge(src="n3", dst="n4"),
        ],
    )
    stats, runs = RunStats(), []
    for _ in range(3):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n3", "n4"], ok=True))
    g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2", "n4"], ok=True))
    assert dominant_path(g) == ["n1", "n3", "n4"]


def test_dominant_path_terminates_on_cycle():
    g = ProcedureGraph(
        nodes=[GraphNode(id="a"), GraphNode(id="b")],
        edges=[GraphEdge(src="a", dst="b", weight=2.0), GraphEdge(src="b", dst="a", weight=2.0)],
    )
    assert dominant_path(g) == ["a", "b"]


# --- evolve_graph / live_steps ---------------------------------------------


def test_evolve_noop_below_min_runs():
    g = _linear(3)
    g, stats, _ = merge_run(g, RunStats(), [], RunTrace(path=["n1", "n2"], ok=True))
    evolved, changed = evolve_graph(g, stats, min_runs=3)
    assert changed is False
    assert all(not n.dead_end for n in evolved.nodes)


def test_evolve_flags_dead_end_step():
    # n3 is never traversed across 3 runs -> flagged dead_end.
    g = _linear(3)
    stats, runs = RunStats(), []
    for _ in range(3):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2"], ok=True))
    evolved, changed = evolve_graph(g, stats, min_runs=3)
    assert changed is True
    dead = {n.id for n in evolved.nodes if n.dead_end}
    assert dead == {"n3"}
    assert live_steps(evolved) == ["step 1", "step 2"]


def test_evolve_unflags_when_node_later_used():
    g = _linear(3)
    stats, runs = RunStats(), []
    for _ in range(3):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2"], ok=True))
    g, _ = evolve_graph(g, stats, min_runs=3)
    assert any(n.dead_end for n in g.nodes)
    # n3 now gets exercised; re-evolve should clear the flag.
    g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2", "n3"], ok=True))
    evolved, changed = evolve_graph(g, stats, min_runs=3)
    assert changed is True
    assert all(not n.dead_end for n in evolved.nodes)


# --- is_template_ready -----------------------------------------------------


def test_template_not_ready_below_min_ok():
    g = _linear(2)
    stats, runs = RunStats(), []
    for _ in range(3):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2"], ok=True))
    assert is_template_ready(g, stats, min_ok_runs=5) is False


def test_template_ready_when_stable_and_successful():
    g = _linear(2)
    stats, runs = RunStats(), []
    for _ in range(6):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2"], ok=True))
    assert is_template_ready(g, stats, min_ok_runs=5) is True


def test_template_not_ready_when_low_success_rate():
    g = _linear(2)
    stats, runs = RunStats(), []
    for i in range(10):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2"], ok=(i % 2 == 0)))
    # 5 ok / 10 total = 0.5 < 0.8 default
    assert is_template_ready(g, stats, min_ok_runs=5) is False


# --- outcome contrast ---------------------------------------


def test_merge_run_splits_edge_weight_by_outcome():
    g = _linear(2)
    stats, runs = RunStats(), []
    g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2"], ok=True))
    g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2"], ok=False))
    e = {(x.src, x.dst): x for x in g.edges}[("n1", "n2")]
    assert (e.weight, e.ok_weight, e.fail_weight) == (2.0, 1.0, 1.0)


def test_dominant_path_prefers_successful_over_most_traversed():
    # Diamond: n1 -> {n2, n3} -> n4. The n2 branch is walked 5x and fails 5x
    # (a retry loop); the n3 branch is walked once and succeeds. The spine is
    # the branch that WORKED, not the one that was hammered.
    g = ProcedureGraph(
        nodes=[GraphNode(id=f"n{i}", text=f"s{i}") for i in range(1, 5)],
        edges=[
            GraphEdge(src="n1", dst="n2"),
            GraphEdge(src="n1", dst="n3"),
            GraphEdge(src="n2", dst="n4"),
            GraphEdge(src="n3", dst="n4"),
        ],
    )
    stats, runs = RunStats(), []
    for _ in range(5):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2", "n4"], ok=False))
    g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n3", "n4"], ok=True))
    assert dominant_path(g) == ["n1", "n3", "n4"]


def test_dominant_path_falls_back_to_total_weight_for_legacy_graphs():
    # Graphs recorded before the outcome split carry weight only.
    g = ProcedureGraph(
        nodes=[GraphNode(id="a"), GraphNode(id="b"), GraphNode(id="c")],
        edges=[GraphEdge(src="a", dst="b", weight=3.0), GraphEdge(src="a", dst="c", weight=1.0)],
    )
    assert dominant_path(g) == ["a", "b"]


def test_evolve_flags_fail_prone_node_and_transition_by_contrast():
    # n1 -> n2 -> n4 (ok) vs n1 -> n3 -> n4 (always fails). Base fail rate is
    # 50%, so n1 and n4 (on every run) sit AT the base rate and are not
    # flagged; n3 and n1->n3 / n3->n4 are at 100% and are. The flag reads the
    # failure rate's lower confidence bound: four of four at
    # a 50% base bounds at 0.69 against a 0.69 bar.
    g = ProcedureGraph(
        nodes=[GraphNode(id=f"n{i}", text=f"s{i}") for i in range(1, 5)],
        edges=[
            GraphEdge(src="n1", dst="n2"),
            GraphEdge(src="n1", dst="n3"),
            GraphEdge(src="n2", dst="n4"),
            GraphEdge(src="n3", dst="n4"),
        ],
    )
    stats, runs = RunStats(), []
    for _ in range(4):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2", "n4"], ok=True))
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n3", "n4"], ok=False))
    evolved, changed = evolve_graph(g, stats, min_runs=3)
    assert changed is True
    prone_nodes = {n.id for n in evolved.nodes if n.fail_prone}
    assert prone_nodes == {"n3"}
    prone_edges = {(e.src, e.dst) for e in evolved.edges if e.fail_prone}
    assert prone_edges == {("n1", "n3"), ("n3", "n4")}
    # Correlation, not cause: fail-prone nodes stay in the linear view.
    assert "s3" in live_steps(evolved)


def test_evolve_does_not_flag_when_everything_fails_equally():
    # A procedure that fails every time has nothing to single out: every node
    # is at the base rate (100%), none exceeds it by the margin.
    g = _linear(3)
    stats, runs = RunStats(), []
    for _ in range(4):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["n1", "n2", "n3"], ok=False))
    evolved, _ = evolve_graph(g, stats, min_runs=3)
    assert not any(n.fail_prone for n in evolved.nodes)
    assert not any(e.fail_prone for e in evolved.edges)


def test_evolve_requires_min_contrast_runs_before_flagging():
    from server.memory.domain.procedure_graph import MIN_CONTRAST_RUNS

    g = ProcedureGraph(
        nodes=[
            GraphNode(id="a", text="a"),
            GraphNode(id="b", text="b"),
            GraphNode(id="c", text="c"),
        ],
        edges=[GraphEdge(src="a", dst="b"), GraphEdge(src="a", dst="c")],
    )
    stats, runs = RunStats(), []
    for _ in range(10):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["a", "b"], ok=True))
    # c fails, but on fewer than MIN_CONTRAST_RUNS traversals -> no flag yet.
    for _ in range(MIN_CONTRAST_RUNS - 1):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["a", "c"], ok=False))
    evolved, _ = evolve_graph(g, stats, min_runs=3)
    assert not any(n.fail_prone for n in evolved.nodes)
    g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["a", "c"], ok=False))
    evolved, _ = evolve_graph(g, stats, min_runs=3)
    assert {n.id for n in evolved.nodes if n.fail_prone} == {"c"}


def test_evolve_unflags_fail_prone_when_node_recovers():
    g = ProcedureGraph(
        nodes=[
            GraphNode(id="a", text="a"),
            GraphNode(id="b", text="b"),
            GraphNode(id="c", text="c"),
        ],
        edges=[GraphEdge(src="a", dst="b"), GraphEdge(src="a", dst="c")],
    )
    stats, runs = RunStats(), []
    for _ in range(4):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["a", "b"], ok=True))
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["a", "c"], ok=False))
    g, _ = evolve_graph(g, stats, min_runs=3)
    assert any(n.fail_prone for n in g.nodes)
    for _ in range(12):
        g, stats, runs = merge_run(g, stats, runs, RunTrace(path=["a", "c"], ok=True))
    evolved, changed = evolve_graph(g, stats, min_runs=3)
    assert changed is True
    assert not any(n.fail_prone for n in evolved.nodes)


# --- edge attributes / action nodes / structural checks ---------------------


def test_edge_attributes_and_action_tool_roundtrip():
    g = normalize_graph(
        {
            "nodes": [
                {"id": "forecast", "kind": "action", "tool": "cash_flow_forecast", "text": "x"},
                {"id": "raise", "kind": "action", "tool": "fund_raising_request", "text": "y"},
            ],
            "edges": [
                {
                    "src": "forecast",
                    "dst": "raise",
                    "kind": "dep",
                    "condition": "runway below buffer",
                    "guidance": "request early",
                    "pitfalls": "do not stack requests",
                }
            ],
        }
    )
    e = g.edges[0]
    assert (e.condition, e.guidance, e.pitfalls) == (
        "runway below buffer",
        "request early",
        "do not stack requests",
    )
    assert g.nodes[0].tool == "cash_flow_forecast"
    # action nodes are steps in the linear view
    assert live_steps(g) == ["x", "y"]


def test_normalize_rejects_cycle_with_no_exit():
    with pytest.raises(GraphValidationError, match="terminal"):
        normalize_graph(
            {
                "nodes": [{"id": "a"}, {"id": "b"}],
                "edges": [{"src": "a", "dst": "b"}, {"src": "b", "dst": "a"}],
            }
        )


def test_normalize_accepts_retry_loop_with_exit():
    g = normalize_graph(
        {
            "nodes": [{"id": "try"}, {"id": "check"}, {"id": "done"}],
            "edges": [
                {"src": "try", "dst": "check"},
                {"src": "check", "dst": "try"},
                {"src": "check", "dst": "done"},
            ],
        }
    )
    assert len(g.edges) == 3


def test_node_cap_admits_a_tool_catalog_sized_graph():
    from server.memory.domain.procedure_graph import MAX_NODES

    assert MAX_NODES >= 131  # a large published tool-calling procedure graph has 131 nodes
    g = normalize_graph({"nodes": [{"id": f"t{i}"} for i in range(131)], "edges": []})
    assert len(g.nodes) == 131


# --- localize / render --------------------------------------


def _cfo_graph() -> ProcedureGraph:
    g = normalize_graph(
        {
            "nodes": [
                {"id": "start", "kind": "step", "text": "month start"},
                {
                    "id": "cash",
                    "kind": "action",
                    "tool": "check_cash_in_bank",
                    "text": "audit cash",
                },
                {
                    "id": "forecast",
                    "kind": "action",
                    "tool": "cash_flow_forecast",
                    "text": "project runway",
                },
                {
                    "id": "raise",
                    "kind": "action",
                    "tool": "fund_raising_request",
                    "text": "raise capital",
                },
                {
                    "id": "close",
                    "kind": "action",
                    "tool": "book_closing",
                    "text": "close the month",
                },
                {"id": "pass", "kind": "step", "text": "do nothing"},
            ],
            "edges": [
                {"src": "start", "dst": "cash"},
                {
                    "src": "cash",
                    "dst": "forecast",
                    "kind": "dataflow",
                    "condition": "always",
                    "guidance": "forecast before deciding",
                    "pitfalls": "do not skip the forecast",
                },
                {
                    "src": "forecast",
                    "dst": "raise",
                    "kind": "branch",
                    "condition": "runway below buffer",
                    "guidance": "request early",
                    "pitfalls": "do not stack a second request",
                },
                {
                    "src": "forecast",
                    "dst": "close",
                    "kind": "branch",
                    "condition": "runway healthy",
                },
                {"src": "forecast", "dst": "pass"},
                {"src": "raise", "dst": "close"},
            ],
        }
    )
    stats, runs = RunStats(), []
    for _ in range(4):
        g, stats, runs = merge_run(
            g, stats, runs, RunTrace(path=["start", "cash", "forecast", "raise", "close"], ok=True)
        )
    for _ in range(3):
        g, stats, runs = merge_run(
            g, stats, runs, RunTrace(path=["start", "cash", "forecast", "pass"], ok=False)
        )
    g, _ = evolve_graph(g, stats, min_runs=3)
    return g


def test_localize_matches_by_id_and_by_tool_name():
    from server.memory.domain.procedure_graph import localize

    g = _cfo_graph()
    by_id = localize(g, "forecast", hops=1)
    by_tool = localize(g, "cash_flow_forecast", hops=1)
    assert by_id.matched and by_tool.matched
    assert by_id.active is not None and by_id.active.id == "forecast"
    assert by_tool.active is not None and by_tool.active.id == "forecast"
    assert localize(g, "Cash_Flow_Forecast", hops=1).matched  # case-insensitive fallback


def test_localize_exposes_only_the_neighbourhood_with_attributes_and_outcomes():
    from server.memory.domain.procedure_graph import localize

    ctx = localize(_cfo_graph(), "cash", hops=2)
    assert [t.dst for t in ctx.hops[0]] == ["forecast"]
    hop2 = {t.dst: t for t in ctx.hops[1]}
    # forecast -> raise (ok 4), forecast -> close (0), forecast -> pass (fail 3)
    assert set(hop2) == {"raise", "close", "pass"}
    assert hop2["raise"].guidance == "request early"
    assert hop2["raise"].pitfalls == "do not stack a second request"
    assert (hop2["raise"].ok, hop2["raise"].fail) == (4, 0)
    assert hop2["raise"].proven is True
    assert hop2["pass"].fail_prone is True and hop2["pass"].proven is False
    # nothing beyond two hops
    assert len(ctx.hops) == 2
    assert ctx.dominant_next == ["forecast", "raise", "close"]


def test_localize_orders_transitions_by_success_and_skips_dead_ends():
    from server.memory.domain.procedure_graph import localize

    g = _cfo_graph()
    # Mark `close` dead-end by hand: it must vanish from the neighbourhood.
    g = ProcedureGraph(
        nodes=[n.model_copy(update={"dead_end": True}) if n.id == "close" else n for n in g.nodes],
        edges=g.edges,
    )
    ctx = localize(g, "forecast", hops=1)
    assert [t.dst for t in ctx.hops[0]] == ["raise", "pass"]  # ok-weight first; close omitted


def test_localize_unmatched_returns_entry_nodes():
    from server.memory.domain.procedure_graph import localize

    ctx = localize(_cfo_graph(), "no_such_tool", hops=2)
    assert ctx.matched is False and ctx.active is None
    assert ctx.entry_nodes == ["start"]
    assert ctx.hops == []


def test_render_local_context_is_deterministic_and_carries_attributes():
    from server.memory.domain.procedure_graph import localize, render_local_context

    g = _cfo_graph()
    ctx = localize(g, "forecast", hops=1)
    text = render_local_context(ctx, name="monthly-close")
    assert text == render_local_context(localize(g, "forecast", hops=1), name="monthly-close")
    assert text.startswith("Active node for monthly-close: [forecast] (action: cash_flow_forecast)")
    assert (
        "- [forecast] -> [raise] (fund_raising_request) — raise capital (branch) [proven; ok 4 / fail 0]"
        in text
    )
    assert "    condition: runway below buffer" in text
    assert "    guidance: request early" in text
    assert "    pitfalls: do not stack a second request" in text
    assert "[fail-prone: failed 3 of 3]" in text
    assert text.rstrip().endswith("Proven path from here: [raise] -> [close]")


def test_render_unmatched_lists_entry_points():
    from server.memory.domain.procedure_graph import localize, render_local_context

    text = render_local_context(localize(_cfo_graph(), "zzz"), name="p")
    assert text == "No active node matched for p.\nEntry points: [start]"


# --- acceptance gate / digest / carry -----------------------


def test_gate_revision_verdicts():
    from server.memory.domain.procedure_graph import gate_revision

    ok = lambda t, o: RunStats(total=t, ok=o)  # noqa: E731
    assert gate_revision(ok(10, 8), ok(3, 3), min_runs=5) == "undecided"  # too few after
    assert gate_revision(ok(2, 2), ok(6, 3), min_runs=5) == "accept"  # no baseline
    assert gate_revision(ok(10, 8), ok(10, 8), min_runs=5) == "accept"  # tie
    assert gate_revision(ok(10, 8), ok(10, 7), min_runs=5, margin=0.05) == "revert"  # 0.8 -> 0.7
    assert gate_revision(ok(10, 8), ok(20, 16), min_runs=5, margin=0.05) == "accept"
    assert gate_revision(ok(10, 8), ok(10, 9), min_runs=5) == "accept"  # improved


def test_graph_digest_ignores_statistics_and_flags_but_not_topology_or_attributes():
    from server.memory.domain.procedure_graph import graph_digest

    a = steps_to_graph(["x", "y"])
    b, _, _ = merge_run(a, RunStats(), [], RunTrace(path=["n1", "n2"], ok=False))
    b, _ = evolve_graph(b, RunStats(total=3, ok=0), min_runs=1)
    assert graph_digest(a) == graph_digest(b)  # stats + flags differ, topology same
    c = ProcedureGraph(nodes=a.nodes, edges=[a.edges[0].model_copy(update={"guidance": "hurry"})])
    assert graph_digest(a) != graph_digest(c)  # attribute text is part of the topology
    d = steps_to_graph(["x", "y", "z"])
    assert graph_digest(a) != graph_digest(d)


def test_carry_stats_moves_evidence_onto_unchanged_ids_only():
    from server.memory.domain.procedure_graph import carry_stats

    old = steps_to_graph(["a", "b", "c"])
    stats, runs = RunStats(), []
    for _ in range(3):
        old, stats, runs = merge_run(old, stats, runs, RunTrace(path=["n1", "n2", "n3"], ok=True))
    old, _ = evolve_graph(old, RunStats(total=3, ok=0), min_runs=1)  # sets flags we must drop
    new = ProcedureGraph(
        nodes=[
            GraphNode(id="n1", text="a"),
            GraphNode(id="n2", text="b"),
            GraphNode(id="n9", text="new"),
        ],
        edges=[GraphEdge(src="n1", dst="n2"), GraphEdge(src="n2", dst="n9")],
    )
    carried = carry_stats(old, new)
    by_id = {n.id: n for n in carried.nodes}
    assert by_id["n1"].stats.runs == 3 and by_id["n9"].stats.runs == 0
    e = {(x.src, x.dst): x for x in carried.edges}
    assert e[("n1", "n2")].ok_weight == 3.0 and e[("n2", "n9")].weight == 0.0
    assert not any(n.dead_end or n.fail_prone for n in carried.nodes)


# --- trajectory window --------------------------------------


def test_localize_marks_visited_transitions_and_detects_loops():
    from server.memory.domain.procedure_graph import localize

    g = _cfo_graph()
    # First time at forecast: no loop.
    ctx = localize(g, "forecast", hops=1, recent_path=["start", "cash", "forecast"])
    assert ctx.looping is False and ctx.exits == []
    assert ctx.revisits == {"start": 1, "cash": 1, "forecast": 1}
    # Second time at forecast after trying `pass` (by tool name or id): loop.
    ctx = localize(
        g,
        "cash_flow_forecast",
        hops=1,
        recent_path=["start", "check_cash_in_bank", "forecast", "pass", "forecast"],
    )
    assert ctx.looping is True
    assert ctx.revisits["forecast"] == 2 and ctx.revisits["cash"] == 1
    by_dst = {t.dst: t for t in ctx.hops[0]}
    assert by_dst["pass"].visited is True and by_dst["raise"].visited is False
    # Untaken exits, proven first: raise (proven) then close.
    assert ctx.exits == ["raise", "close"]


def test_localize_reports_no_exits_when_every_exit_was_taken():
    from server.memory.domain.procedure_graph import localize

    g = _cfo_graph()
    ctx = localize(
        g,
        "forecast",
        hops=1,
        recent_path=["forecast", "raise", "forecast", "close", "forecast", "pass", "forecast"],
    )
    assert ctx.looping is True and ctx.exits == []


def test_render_local_context_warns_on_loop_and_marks_visited():
    from server.memory.domain.procedure_graph import localize, render_local_context

    g = _cfo_graph()
    text = render_local_context(
        localize(
            g, "forecast", hops=1, recent_path=["start", "cash", "forecast", "pass", "forecast"]
        ),
        name="monthly-close",
    )
    assert (
        "WARNING: you have been at [forecast] 2 times in this run; untaken exits: [raise], [close]"
        in text
    )
    assert "[already visited this run" in text
    # no window -> no warning
    assert "WARNING" not in render_local_context(localize(g, "forecast", hops=1), name="x")


# --- sequential gate ----------------------------------------


def test_sprt_gate_waits_then_decides_with_bounded_error():
    import math

    from server.memory.domain.procedure_graph import (
        SPRT_ALPHA,
        SPRT_BETA,
        gate_revision_sprt,
        sprt_llr,
    )

    ok = lambda t, o: RunStats(total=t, ok=o)  # noqa: E731
    before = ok(20, 18)  # 0.9 prior, smoothed to ~0.86
    assert gate_revision_sprt(before, ok(2, 2))[0] == "undecided"  # below min_runs
    assert gate_revision_sprt(ok(1, 1), ok(3, 3))[0] == "accept"  # no baseline
    # Boundaries are Wald's: ln((1-beta)/alpha) and ln(beta/(1-alpha)).
    upper, lower = math.log((1 - SPRT_BETA) / SPRT_ALPHA), math.log(SPRT_BETA / (1 - SPRT_ALPHA))
    assert lower < sprt_llr(before, ok(5, 5)) < upper  # not yet
    assert gate_revision_sprt(before, ok(5, 5))[0] == "undecided"
    assert gate_revision_sprt(before, ok(14, 14))[0] == "accept"  # drop ruled out
    assert gate_revision_sprt(before, ok(6, 1))[0] == "revert"  # drop established
    # every additional failure moves the statistic toward revert, every
    # success toward accept: the statistic is monotone in the counts
    assert sprt_llr(before, ok(6, 1)) > sprt_llr(before, ok(6, 2)) > sprt_llr(before, ok(6, 3))


def test_sprt_gate_forces_a_verdict_at_max_runs_by_point_estimate():
    from server.memory.domain.procedure_graph import gate_revision_sprt

    before = RunStats(total=40, ok=28)  # 0.70
    # Hovering just under the prior: never crosses either boundary within the
    # cap, so the cap decides — rate fell 0.05, less than half of min_drop.
    verdict, _ = gate_revision_sprt(before, RunStats(total=50, ok=32), max_runs=50)  # 0.64
    assert verdict == "accept"
    verdict, _ = gate_revision_sprt(before, RunStats(total=50, ok=28), max_runs=50)  # 0.56
    assert verdict == "revert"
