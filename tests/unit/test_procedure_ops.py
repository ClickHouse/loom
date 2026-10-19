"""Operation-layer tests for the procedure plane: probation ordering in
procedure.match and the procedure.locate read path. Storage is stubbed; the
graph logic itself is covered in test_procedure_graph.py."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.core.errors import NotFoundError


def _value(
    name: str,
    *,
    tags: list[str] | None = None,
    total: int = 0,
    ok: int = 0,
    template: bool = False,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "situation": f"when {name}",
        "name": name,
        "steps": ["a", "b"],
        "tags": tags or [],
        "metadata": {},
        "graph": graph,
        "run_stats": {"total": total, "ok": ok},
        "runs": [],
        "template": template,
        "revision": 1,
    }


def _stub_search_and_bulk(
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, dict[str, Any]],
    order: list[str],
    high: frozenset[str] = frozenset(),
) -> None:
    async def _search(inp: Any) -> Any:
        return NS(
            results=[
                NS(namespace="ns", memory_key=k, similarity=1.0 - i * 0.01)
                for i, k in enumerate(order)
            ]
        )

    async def _rows(_ident: Any, keys: list[str], *, exclude_high_sensitivity: bool, **_k: Any):
        hidden = high if exclude_high_sensitivity else frozenset()
        return [NS(memory_key=k, value=rows[k]) for k in keys if k in rows and k not in hidden]

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    monkeypatch.setattr("server.memory.store.get_many_by_keys", _rows)


def test_match_labels_status_and_lists_candidates_last(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    rows = {
        "p:mined": _value("mined", tags=["mined"]),  # candidate
        "p:auth": _value("auth"),  # authored
        "p:run": _value("run", total=3, ok=2),  # exercised
        "p:tpl": _value("tpl", total=9, ok=9, template=True),  # template
    }
    # Similarity order puts the mined candidate FIRST; it must come out last.
    _stub_search_and_bulk(monkeypatch, rows, ["p:mined", "p:auth", "p:run", "p:tpl"])
    out = asyncio.run(
        handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    assert [m.memory_key for m in out.matches] == ["p:auth", "p:run", "p:tpl", "p:mined"]
    assert [m.status for m in out.matches] == ["authored", "exercised", "template", "candidate"]

    out = asyncio.run(
        handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", situation="x", include_candidates=False)
        )
    )
    assert "p:mined" not in {m.memory_key for m in out.matches}


def test_match_never_returns_a_high_procedure(monkeypatch: pytest.MonkeyPatch) -> None:
    # The wildcard read can answer another agent's row of a hit key, labelled high.
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    rows = {"p:auth": _value("auth"), "p:vault": _value("vault")}
    _stub_search_and_bulk(monkeypatch, rows, ["p:vault", "p:auth"], high=frozenset({"p:vault"}))
    out = asyncio.run(
        handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    assert [m.memory_key for m in out.matches] == ["p:auth"]


def test_match_surfaces_fail_prone_steps_as_pitfalls(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    graph = {
        "nodes": [
            {"id": "n1", "text": "a"},
            {"id": "n2", "text": "retry blindly", "fail_prone": True},
        ],
        "edges": [{"src": "n1", "dst": "n2"}],
    }
    _stub_search_and_bulk(monkeypatch, {"p:x": _value("x", total=5, ok=2, graph=graph)}, ["p:x"])
    out = asyncio.run(
        handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    assert out.matches[0].fail_prone_steps == ["retry blindly"]


def test_locate_reads_graph_and_renders_neighbourhood(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureLocateInput
    from server.memory.app.procedure import handle_procedure_locate

    graph = {
        "nodes": [
            {"id": "s", "text": "start"},
            {"id": "t", "kind": "action", "tool": "run_tests", "text": "run the tests"},
            {"id": "d", "text": "done"},
        ],
        "edges": [
            {"src": "s", "dst": "t", "guidance": "always test first"},
            {"src": "t", "dst": "d", "condition": "tests green", "ok_weight": 2, "weight": 2},
        ],
    }

    async def _get(inp: Any) -> Any:
        assert inp.memory_key == "p:ship"
        return NS(value=_value("ship", total=2, ok=2, graph=graph), content_sha256="h")

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)

    out = asyncio.run(
        handle_procedure_locate(
            ProcedureLocateInput(org="o", namespace="ns", memory_key="p:ship", at="run_tests")
        )
    )
    assert out.matched is True and out.active is not None and out.active["id"] == "t"
    assert [t["dst"] for t in out.hops[0]] == ["d"]
    assert out.hops[0][0]["condition"] == "tests green"
    assert out.dominant_next == ["d"]
    assert out.status == "exercised"
    assert "Active node for ship: [t] (action: run_tests)" in out.rendered

    # recent_path fallback + unmatched → entry nodes
    out = asyncio.run(
        handle_procedure_locate(
            ProcedureLocateInput(org="o", namespace="ns", memory_key="p:ship", recent_path=["nope"])
        )
    )
    assert out.matched is False and out.entry_nodes == ["s"]


# --- run ledger ---------------------------------------------


def _ledger_fixture(monkeypatch: pytest.MonkeyPatch, row_value: dict[str, Any]) -> dict[str, Any]:
    """Stub the row read/write and the ledger; return the recorder dict."""
    from server.memory.app import procedure_ledger

    rec: dict[str, Any] = {"inserted": [], "sets": [], "unfolded": []}

    async def _get(inp: Any) -> Any:
        return NS(value=row_value, content_sha256="h")

    async def _set(inp: Any) -> Any:
        rec["sets"].append(inp.value)
        row_value.clear()
        row_value.update(inp.value)
        return NS(memory_id="m", version=len(rec["sets"]) + 1)

    async def _record(**kw: Any) -> Any:
        rec["inserted"].append(kw)
        return None

    async def _count(**kw: Any) -> tuple[int, int]:
        rows = [r for r in rec["inserted"]]
        return len(rows), sum(1 for r in rows if r["ok"])

    async def _unfolded(**kw: Any) -> list[dict[str, Any]]:
        return list(rec["unfolded"])

    async def _cost(**kw: Any) -> dict[int, dict[str, float]]:
        return {}

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set)
    monkeypatch.setattr(procedure_ledger, "record", _record)
    monkeypatch.setattr(procedure_ledger, "count_since", _count)
    monkeypatch.setattr(procedure_ledger, "unfolded", _unfolded)
    monkeypatch.setattr(procedure_ledger, "cost_by_revision", _cost)
    return rec


def test_record_run_appends_to_ledger_and_does_not_rewrite_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureRecordRunInput
    from server.memory.app.procedure import handle_procedure_record_run

    row = _value("ship", total=2, ok=2)
    row["revision"] = 3
    rec = _ledger_fixture(monkeypatch, row)
    out = asyncio.run(
        handle_procedure_record_run(
            ProcedureRecordRunInput(
                org="o", namespace="ns", memory_key="p:ship", path=["n1", "n2"], ok=False
            )
        )
    )
    assert len(rec["inserted"]) == 1
    assert rec["inserted"][0]["revision"] == 3 and rec["inserted"][0]["ok"] is False
    assert rec["sets"] == []  # no read-modify-write of the row
    # folded (2/2) + unfolded (1 total, 0 ok)
    assert out.run_stats == {"total": 3, "ok": 2, "unfolded": 1}


def test_record_run_unknown_procedure_is_a_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureRecordRunInput
    from server.memory.app.procedure import handle_procedure_record_run

    async def _get(inp: Any) -> Any:
        return NS(value={"not": "a procedure"}, content_sha256="h")

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    # The client error is a 404 RESOURCE_NOT_FOUND on the wire.
    with pytest.raises(NotFoundError, match="no procedure"):
        asyncio.run(
            handle_procedure_record_run(
                ProcedureRecordRunInput(
                    org="o", namespace="ns", memory_key="p:x", path=["n1"], ok=True
                )
            )
        )


def test_evolve_folds_unfolded_ledger_rows_and_advances_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _value("ship")
    row["steps"] = ["a", "b", "c"]
    rec = _ledger_fixture(monkeypatch, row)
    rec["unfolded"] = [
        {
            "path": ["n1", "n2"],
            "ok": True,
            "note": "",
            "revision": 1,
            "ts": "2026-09-11 10:00:00.000",
        },
        {
            "path": ["n1", "n2"],
            "ok": True,
            "note": "",
            "revision": 1,
            "ts": "2026-09-11 10:00:01.000",
        },
        {
            "path": ["n1", "n2"],
            "ok": True,
            "note": "",
            "revision": 1,
            "ts": "2026-09-11 10:00:02.000",
        },
    ]
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", min_runs=3)
        )
    )
    assert out.folded_runs == 3
    assert out.run_stats == {"total": 3, "ok": 3}
    assert out.dead_end_steps == ["c"]  # n3 never traversed across 3 runs
    assert row["ledger_folded_through"] == "2026-09-11 10:00:02.000"

    # Second evolve: nothing newer than the watermark -> nothing re-folded.
    out2 = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship", min_runs=3)
        )
    )
    assert out2.folded_runs == 0 and out2.run_stats == {"total": 3, "ok": 3}


# --- gated revisions ----------------------------------------


def _graph_ab(extra: bool = False) -> dict[str, Any]:
    nodes = [{"id": "n1", "text": "a"}, {"id": "n2", "text": "b"}]
    edges = [{"src": "n1", "dst": "n2"}]
    if extra:
        nodes.append({"id": "n3", "text": "verify"})
        edges.append({"src": "n2", "dst": "n3"})
    return {"nodes": nodes, "edges": edges}


def test_revise_opens_a_gated_revision_and_carries_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureReviseInput
    from server.memory.app.procedure_evolve import handle_procedure_revise

    row = _value("ship", total=4, ok=2, graph=_graph_ab())
    row["graph"]["edges"][0].update({"weight": 4, "ok_weight": 2, "fail_weight": 2})
    _ledger_fixture(monkeypatch, row)
    out = asyncio.run(
        handle_procedure_revise(
            ProcedureReviseInput(
                org="o",
                namespace="ns",
                agent="refiner",
                memory_key="p:ship",
                graph=_graph_ab(extra=True),
                reason="failed runs skipped verification",
            )
        )
    )
    assert (out.prior_revision, out.revision) == (1, 2)
    assert out.steps == ["a", "b", "verify"]
    assert row["revision"] == 2 and row["template"] is False
    assert row["pending_revision"]["prior_revision"] == 1
    assert row["pending_revision"]["graph"]["nodes"][0]["id"] == "n1"
    assert row["graph"]["edges"][0]["ok_weight"] == 2.0  # carried onto the unchanged edge
    assert row["revisions"][-1]["outcome"] == "pending"

    # A second proposal while one is pending is refused.
    with pytest.raises(ValueError, match="already pending"):
        asyncio.run(
            handle_procedure_revise(
                ProcedureReviseInput(
                    org="o", namespace="ns", memory_key="p:ship", graph=_graph_ab()
                )
            )
        )


def test_revise_refuses_identical_and_rejected_topologies(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureReviseInput
    from server.memory.app.procedure_evolve import handle_procedure_revise
    from server.memory.domain.procedure_graph import ProcedureGraph, graph_digest

    row = _value("ship", graph=_graph_ab())
    _ledger_fixture(monkeypatch, row)
    with pytest.raises(ValueError, match="identical"):
        asyncio.run(
            handle_procedure_revise(
                ProcedureReviseInput(
                    org="o", namespace="ns", memory_key="p:ship", graph=_graph_ab()
                )
            )
        )

    bad = _graph_ab(extra=True)
    row["rejected"] = [
        {
            "revision": 2,
            "digest": graph_digest(ProcedureGraph.model_validate(bad)),
            "before": {"total": 10, "ok": 9},
            "after": {"total": 10, "ok": 4},
        }
    ]
    with pytest.raises(ValueError, match="rejected at revision 2"):
        asyncio.run(
            handle_procedure_revise(
                ProcedureReviseInput(org="o", namespace="ns", memory_key="p:ship", graph=bad)
            )
        )
    out = asyncio.run(
        handle_procedure_revise(
            ProcedureReviseInput(
                org="o", namespace="ns", memory_key="p:ship", graph=bad, force=True
            )
        )
    )
    assert out.revision == 2


def _pending_row() -> dict[str, Any]:
    row = _value("ship", total=10, ok=9, graph=_graph_ab(extra=True))
    row["revision"] = 2
    row["steps"] = ["a", "b", "verify"]
    row["pending_revision"] = {
        "prior_revision": 1,
        "graph": _graph_ab(),
        "steps": ["a", "b"],
        "reason": "add verify",
        "by": "refiner",
        "ts": "2026-09-11T10:00:00+00:00",
    }
    row["revisions"] = [
        {"revision": 2, "digest": "x", "reason": "add verify", "outcome": "pending"}
    ]
    return row


def _stub_by_revision(monkeypatch: pytest.MonkeyPatch, table: dict[int, tuple[int, int]]) -> None:
    from server.memory.app import procedure_ledger

    async def _by_rev(**kw: Any) -> dict[int, tuple[int, int]]:
        return dict(table)

    monkeypatch.setattr(procedure_ledger, "stats_by_revision", _by_rev)


def test_evolve_gate_is_undecided_until_the_revision_has_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _pending_row()
    _ledger_fixture(monkeypatch, row)
    _stub_by_revision(monkeypatch, {1: (10, 9), 2: (2, 2)})
    out = asyncio.run(
        handle_procedure_evolve(ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship"))
    )
    assert out.gate == "undecided" and out.gate_after == {"total": 2, "ok": 2}
    assert row.get("pending_revision") is not None and row["revision"] == 2


def test_evolve_gate_accepts_a_revision_that_holds_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _pending_row()
    _ledger_fixture(monkeypatch, row)
    _stub_by_revision(monkeypatch, {1: (10, 9), 2: (6, 6)})
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(
                org="o", namespace="ns", memory_key="p:ship", gate_mode="margin", gate_min_runs=5
            )
        )
    )
    assert out.gate == "accept" and out.gate_mode == "margin" and out.revision == 2
    assert "pending_revision" not in row
    assert row["revisions"][-1]["outcome"] == "accepted"
    assert [n["id"] for n in row["graph"]["nodes"]] == ["n1", "n2", "n3"]


def test_evolve_gate_reverts_a_revision_that_hurts_and_remembers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve
    from server.memory.domain.procedure_graph import ProcedureGraph, graph_digest

    row = _pending_row()
    bad_digest = graph_digest(ProcedureGraph.model_validate(row["graph"]))
    # runs on the revision under test used the new edge; that evidence must
    # survive on the shared ids after the revert
    row["graph"]["edges"][0].update({"weight": 6, "ok_weight": 3, "fail_weight": 3})
    _ledger_fixture(monkeypatch, row)
    _stub_by_revision(monkeypatch, {1: (10, 9), 2: (6, 3)})  # 0.9 -> 0.5
    out = asyncio.run(
        handle_procedure_evolve(
            ProcedureEvolveInput(
                org="o", namespace="ns", memory_key="p:ship", gate_mode="margin", gate_min_runs=5
            )
        )
    )
    assert out.gate == "revert"
    assert out.gate_before == {"total": 10, "ok": 9} and out.gate_after == {"total": 6, "ok": 3}
    assert [n["id"] for n in row["graph"]["nodes"]] == ["n1", "n2"]  # prior topology restored
    assert row["graph"]["edges"][0]["ok_weight"] == 3.0  # stats carried back
    assert row["revision"] == 3 and "pending_revision" not in row
    assert row["rejected"][-1]["digest"] == bad_digest
    assert row["rejected"][-1]["steps"] == ["a", "b", "verify"]
    assert row["revisions"][-1]["outcome"] == "reverted"
    assert out.revision == 3


def test_match_exposes_revision_and_rejected_topologies(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    row = _value("ship", total=3, ok=3, graph=_graph_ab())
    row["revision"] = 3
    row["rejected"] = [
        {
            "revision": 2,
            "digest": "abcd",
            "reason": "r",
            "before": {"total": 10, "ok": 9},
            "after": {"total": 6, "ok": 3},
            "steps": ["a", "b", "verify"],
            "ts": "t",
            "by": "x",
        }
    ]
    _stub_search_and_bulk(monkeypatch, {"p:ship": row}, ["p:ship"])
    out = asyncio.run(
        handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    m = out.matches[0]
    assert m.revision == 3
    assert m.rejected_revisions == [
        {
            "revision": 2,
            "digest": "abcd",
            "reason": "r",
            "before": {"total": 10, "ok": 9},
            "after": {"total": 6, "ok": 3},
            "steps": ["a", "b", "verify"],
        }
    ]


# --- trajectory window, tool re-rank, per-agent -------------


def test_match_reranks_by_recent_tool_overlap_within_a_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    def _g(*tools: str) -> dict[str, Any]:
        return {
            "nodes": [{"id": t, "kind": "action", "tool": t, "text": t} for t in tools],
            "edges": [{"src": tools[i], "dst": tools[i + 1]} for i in range(len(tools) - 1)],
        }

    rows = {
        "p:deploy": _value("deploy", total=3, ok=3, graph=_g("build", "push", "verify")),
        "p:tests": _value("tests", total=3, ok=3, graph=_g("run_tests", "read_failures", "fix")),
    }
    # deploy is more similar (first), but the agent has been running tests.
    _stub_search_and_bulk(monkeypatch, rows, ["p:deploy", "p:tests"])
    out = asyncio.run(
        handle_procedure_match(
            ProcedureMatchInput(
                org="o", namespace="ns", situation="x", recent_tools=["run_tests", "read_failures"]
            )
        )
    )
    assert [m.memory_key for m in out.matches] == ["p:tests", "p:deploy"]
    assert out.matches[0].tool_overlap == 1.0 and out.matches[1].tool_overlap == 0.0
    assert out.matches[0].score > out.matches[1].score
    # without recent_tools the similarity order stands and score == similarity
    out = asyncio.run(
        handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    assert [m.memory_key for m in out.matches] == ["p:deploy", "p:tests"]
    assert out.matches[0].score == out.matches[0].similarity


def test_match_reports_success_for_the_calling_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureMatchInput
    from server.memory.app.procedure import handle_procedure_match

    row = _value("ship", total=12, ok=11, template=True, graph=_graph_ab())
    row["run_stats_by_agent"] = {"planner": {"total": 9, "ok": 9}, "coder": {"total": 3, "ok": 0}}
    _stub_search_and_bulk(monkeypatch, {"p:ship": row}, ["p:ship"])
    out = asyncio.run(
        handle_procedure_match(
            ProcedureMatchInput(org="o", namespace="ns", agent="coder", situation="x")
        )
    )
    m = out.matches[0]
    assert m.status == "template" and m.for_agent == {"total": 3, "ok": 0}
    assert m.run_stats_by_agent["planner"] == {"total": 9, "ok": 9}
    out = asyncio.run(
        handle_procedure_match(ProcedureMatchInput(org="o", namespace="ns", situation="x"))
    )
    assert out.matches[0].for_agent == {}  # fan-out agent "*" has no record of its own


def test_evolve_folds_per_agent_stats_from_the_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _value("ship")
    row["steps"] = ["a", "b"]
    rec = _ledger_fixture(monkeypatch, row)
    rec["unfolded"] = [
        {
            "path": ["n1", "n2"],
            "ok": True,
            "note": "",
            "revision": 1,
            "agent": "planner",
            "ts": "2026-09-11 10:00:00.000",
        },
        {
            "path": ["n1", "n2"],
            "ok": False,
            "note": "",
            "revision": 1,
            "agent": "coder",
            "ts": "2026-09-11 10:00:01.000",
        },
        {
            "path": ["n1", "n2"],
            "ok": True,
            "note": "",
            "revision": 1,
            "agent": "coder",
            "ts": "2026-09-11 10:00:02.000",
        },
        {
            "path": ["n1", "n2"],
            "ok": True,
            "note": "",
            "revision": 1,
            "agent": "*",
            "ts": "2026-09-11 10:00:03.000",
        },
    ]
    asyncio.run(
        handle_procedure_evolve(ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship"))
    )
    assert row["run_stats_by_agent"] == {
        "planner": {"total": 1, "ok": 1},
        "coder": {"total": 2, "ok": 1},
    }


def test_locate_reports_loop_and_attaches_failure_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureLocateInput
    from server.memory.app import procedure_ledger
    from server.memory.app.procedure import handle_procedure_locate

    graph = {
        "nodes": [
            {"id": "s", "text": "start"},
            {"id": "t", "kind": "action", "tool": "run_tests", "text": "test"},
            {"id": "f", "kind": "action", "tool": "fix", "text": "fix"},
            {"id": "d", "text": "done"},
        ],
        "edges": [
            {"src": "s", "dst": "t"},
            {"src": "t", "dst": "f"},
            {"src": "f", "dst": "t"},
            {"src": "t", "dst": "d", "ok_weight": 3, "weight": 3},
        ],
    }
    row = _value("ship", total=3, ok=3, graph=graph)
    row["run_stats_by_agent"] = {"coder": {"total": 2, "ok": 1}}

    async def _get(inp: Any) -> Any:
        return NS(value=row, content_sha256="h")

    async def _notes(**kw: Any) -> list[dict[str, Any]]:
        return [
            {
                "src": "t",
                "dst": "f",
                "note": "fix touched generated files; tests never passed",
                "agent": "x",
                "ts": "t1",
            },
            {"src": "zz", "dst": "yy", "note": "irrelevant elsewhere", "agent": "x", "ts": "t0"},
        ]

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    monkeypatch.setattr(procedure_ledger, "failure_notes", _notes)

    out = asyncio.run(
        handle_procedure_locate(
            ProcedureLocateInput(
                org="o",
                namespace="ns",
                agent="coder",
                memory_key="p:ship",
                recent_path=["s", "run_tests", "fix", "run_tests", "fix", "run_tests"],
            )
        )
    )
    assert out.looping is True and out.revisits["t"] == 3
    assert out.exits == ["d"]  # f already visited; d is the untaken (proven) exit
    assert [n["dst"] for n in out.failure_notes] == ["f"]  # only transitions ahead
    assert "Recent failure notes on the transitions ahead:" in out.rendered
    assert "[t] -> [f]: fix touched generated files" in out.rendered
    assert out.for_agent == {"total": 2, "ok": 1}

    # notes off -> no ledger call needed, no section
    out = asyncio.run(
        handle_procedure_locate(
            ProcedureLocateInput(
                org="o", namespace="ns", memory_key="p:ship", at="run_tests", include_notes=False
            )
        )
    )
    assert out.failure_notes == [] and "failure notes" not in out.rendered


# --- sequential gate ----------------------------------------


def test_evolve_default_gate_is_sequential_and_waits_for_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _pending_row()
    _ledger_fixture(monkeypatch, row)
    # 6/6 is enough for the fixed margin but not for the SPRT at these error
    # rates: the drop is not yet ruled out, so it waits.
    _stub_by_revision(monkeypatch, {1: (10, 9), 2: (6, 6)})
    out = asyncio.run(
        handle_procedure_evolve(ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship"))
    )
    assert out.gate == "undecided" and out.gate_mode == "sprt" and out.gate_llr < 0
    assert row.get("pending_revision") is not None

    # Twelve clean successes rule a 0.15 drop out -> accept.
    _stub_by_revision(monkeypatch, {1: (10, 9), 2: (12, 12)})
    out = asyncio.run(
        handle_procedure_evolve(ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship"))
    )
    assert out.gate == "accept" and "pending_revision" not in row


def test_evolve_sequential_gate_reverts_when_the_drop_is_established(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_procedure import ProcedureEvolveInput
    from server.memory.app.procedure_evolve import handle_procedure_evolve

    row = _pending_row()
    _ledger_fixture(monkeypatch, row)
    _stub_by_revision(monkeypatch, {1: (10, 9), 2: (8, 2)})  # 0.9 -> 0.25
    out = asyncio.run(
        handle_procedure_evolve(ProcedureEvolveInput(org="o", namespace="ns", memory_key="p:ship"))
    )
    assert out.gate == "revert" and out.gate_llr > 0
    assert row["revision"] == 3 and row["rejected"][-1]["revision"] == 2


def test_names_without_an_ascii_slug_get_distinct_keys() -> None:
    from server.memory.app.procedure import _procedure_memory_key

    # ASCII names keep the key they always had, so a re-add updates in place.
    assert _procedure_memory_key("Deploy the API!") == "procedure_deploy_the_api"
    keys = {_procedure_memory_key(n) for n in ("日本の手順", "中文流程", "!!!")}
    assert len(keys) == 3 and "procedure_" not in keys
    assert _procedure_memory_key("日本の手順") == _procedure_memory_key(" 日本の手順 ")


def test_list_never_reads_a_high_procedures_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureListInput
    from server.memory.app.procedure import handle_procedure_list

    rows = {"procedure_auth": _value("auth"), "procedure_vault": _value("vault")}
    _stub_search_and_bulk(monkeypatch, rows, [], high=frozenset({"procedure_vault"}))

    async def _list(_inp: Any) -> Any:
        return NS(
            entries=[
                NS(memory_key="procedure_auth", sensitivity="", created_at=datetime(2026, 9, 1)),
                NS(
                    memory_key="procedure_vault",
                    sensitivity="high",
                    created_at=datetime(2026, 9, 1),
                ),
            ]
        )

    async def _get(_inp: Any) -> Any:
        raise AssertionError("procedure.list reads its bodies in one filtered read")

    monkeypatch.setattr("server.memory.app.query.handle_memory_list", _list)
    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    out = asyncio.run(handle_procedure_list(ProcedureListInput(org="o", namespace="ns")))
    by_key = {p.memory_key: p for p in out.procedures}
    assert by_key["procedure_auth"].situation == rows["procedure_auth"]["situation"]
    assert by_key["procedure_vault"].situation == "" and by_key["procedure_vault"].name == "vault"
