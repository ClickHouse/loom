"""code.callers / code.callees say `truncated` whenever the walk stopped short of the graph:
a frontier left unwalked at the node cap, or a hop whose read hit its row limit."""

from __future__ import annotations

from typing import Any

from server.code.api.models_symbol import CodeCallersInput
from server.code.app.symbol.edges import _walk_edges


def _edge(src: str, dst: str, line: int = 1) -> dict[str, Any]:
    return {
        "tier": "scip",
        "edge_kind": "calls",
        "from_symbol_id": src,
        "to_symbol_id": dst,
        "path": "src/a.cpp",
        "line": line,
        "from_qualified_name": f"ns::{src}",
        "to_qualified_name": f"ns::{dst}",
        "confidence": 1.0,
    }


class _Repo:
    def __init__(self, by_target: dict[str, list[dict[str, Any]]]) -> None:
        self.by_target = by_target

    async def edges_for_symbols(self, org: str, ns: str, ids: list[str], **kw: Any) -> Any:
        rows = [e for i in ids for e in self.by_target.get(i, [])]
        return rows[: int(kw["limit"])]


async def _walk(repo: _Repo, **fields: Any) -> tuple[int, bool]:
    inp = CodeCallersInput(org="o", namespace="n", symbol="s", **fields)
    edges, truncated, _hops, _counts = await _walk_edges(
        repo,  # type: ignore[arg-type]
        "o",
        "n",
        inp,
        direction="callers",
        seed_ids=["s"],
        via_for={},
        overlay=None,
        via_member={},
    )
    return len(edges), truncated


async def test_a_frontier_left_at_the_node_cap_is_truncated() -> None:
    repo = _Repo({"s": [_edge("a", "s"), _edge("b", "s")], "a": [_edge("c", "a")]})
    assert await _walk(repo, depth=2, max_nodes=2) == (2, True)


async def test_a_hop_read_cut_at_its_limit_is_truncated() -> None:
    # Ten rows of one call site: the read is full (limit = 2 * max_nodes) though only one
    # distinct edge survives, so rows past the limit may hold callers never seen.
    repo = _Repo({"s": [_edge("a", "s")] * 10})
    assert await _walk(repo, depth=1, max_nodes=5) == (1, True)


async def test_a_complete_walk_is_not_truncated() -> None:
    repo = _Repo({"s": [_edge("a", "s"), _edge("b", "s")]})
    assert await _walk(repo, depth=2, max_nodes=5) == (2, False)


def test_a_truncated_answer_is_never_exact() -> None:
    from server.code.domain.index.epistemic import CAUSE_TRUNCATED, classify

    full = classify(unresolved=0, ambiguous=0, seed_paths=["a.cpp"], scip_paths={"a.cpp"})
    assert full == ("exact", [])
    cut = classify(
        unresolved=0, ambiguous=0, seed_paths=["a.cpp"], scip_paths={"a.cpp"}, truncated=True
    )
    assert cut == ("lower_bound", [CAUSE_TRUNCATED])


def test_code_callers_reports_the_truncation_cause(monkeypatch) -> None:
    """A walk cut at max_nodes over a fully compiler-indexed seed: truncated, and so a
    lower bound with `truncated` among its causes."""
    import asyncio
    from unittest.mock import patch

    from server.code.app.symbol import graph as h
    from server.settings import settings

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    seed = {
        "symbol_id": "s",
        "qualified_name": "ns::s",
        "name": "s",
        "path": "src/a.cpp",
        "start_line": 1,
        "end_line": 9,
        "tier": "scip",
        "kind": "function",
    }

    class _R(_Repo):
        async def symbols_by_name(self, *a, **k):
            return [seed]

        async def symbols_by_ids(self, *a, **k):
            return [seed]

        async def all_state(self, *a, **k):
            return {}

    repo = _R({"s": [_edge("a", "s"), _edge("b", "s", 2), _edge("c", "s", 3)]})

    async def no_dispatch(*_a, **_k):
        return []

    async def zero(*_a, **_k):
        return (0, 0)

    async def covered(*_a, **_k):
        return {"src/a.cpp"}

    async def fresh(*_a, **_k):
        return set()

    from server.code.api.models_symbol import CodeCallersInput

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=repo),
            patch.object(h, "_dispatch_seeds", new=no_dispatch),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
            patch.object(h, "_declined_safe", new=zero),
            patch.object(h, "_scip_coverage", new=covered),
            patch.object(h, "_scip_stale", new=fresh),
        ):
            return await h.handle_code_callers(
                CodeCallersInput(name="ns::s", include_ambiguous=False, max_nodes=2)
            )

    out = asyncio.run(go())
    assert out.truncated is True
    assert out.epistemic == "lower_bound" and "truncated" in out.causes
