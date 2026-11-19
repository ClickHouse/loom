"""Unrelated methods with the same name are not virtual-dispatch evidence."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.api.models_symbol import CodeCallersInput, CodeImpactInput
from server.code.app.symbol import dispatch, graph, impact
from server.settings import settings


def _symbol(sid: str, name: str, *, kind: str = "method") -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "kind": kind,
        "signature": "(self)",
        "path": "src/app.py",
    }


def _edge(frm: str, to: str, *, kind: str = "calls") -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": frm,
        "to_qualified_name": to,
        "edge_kind": kind,
        "path": "src/app.py",
        "line": 10,
        "tier": "treesitter",
        "confidence": 1.0,
    }


class _CollisionRepo:
    """Flask and View share a method name, but only MethodView inherits View."""

    flask = "src.app::Flask::dispatch_request"
    view = "src.views::View::dispatch_request"
    full = "src.app::Flask::full_dispatch_request"
    wsgi = "src.app::Flask::wsgi_app"

    def __init__(self) -> None:
        self.rows = [
            _edge("src.views::MethodView::dispatch_request", self.view, kind="overrides"),
            _edge("src.views::MethodView", "src.views::View", kind="inherits"),
            _edge(self.full, self.flask),
            _edge(self.wsgi, self.full),
        ]

    async def has_live_scip_override_edges(self, org, ns):
        return False

    async def scip_covered_paths(self, org, ns, paths):
        return set()

    async def call_misses_for_names(self, org, ns, names):
        return {}

    async def live_call_misses_for_names(self, org, ns, names, *, limit=2000):
        return []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        assert (org, ns) == ("test-org", "test-code")
        if name in (self.flask, "Flask.dispatch_request"):
            return [_symbol(self.flask, self.flask)]
        if name == self.view:
            return [_symbol(self.view, self.view)]
        if name in ("src.app::Flask", "src.views::View"):
            return [_symbol(name, name, kind="class")]
        return []

    async def methods_named(self, org, ns, method, *, limit=300):
        assert method == "dispatch_request"
        return [_symbol(self.view, self.view)]

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, **kwargs
    ):
        assert (org, ns) == ("test-org", "test-code")
        key = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        return [
            r for r in self.rows if r[key] in ids and r["edge_kind"] in (edge_kinds or ["calls"])
        ][:limit]


@pytest.fixture
def collision_repo(monkeypatch: pytest.MonkeyPatch) -> _CollisionRepo:
    # This test corpus has no compiler overrides; keep the process cache out of it.
    async def no_compiler_overrides(*args):
        return False

    monkeypatch.setattr(dispatch, "_namespace_has_override_edges", no_compiler_overrides)
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    return _CollisionRepo()


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("include_ambiguous", [False, True])
async def test_callers_does_not_join_unrelated_same_name_method(
    collision_repo: _CollisionRepo,
    monkeypatch: pytest.MonkeyPatch,
    depth: int,
    include_ambiguous: bool,
) -> None:
    repo = collision_repo
    monkeypatch.setattr(graph, "CodeRepo", lambda: repo)
    monkeypatch.setattr(graph, "schedule_code_trace", lambda **kwargs: None)
    result = await graph.handle_code_callers(
        CodeCallersInput(
            org="test-org",
            namespace="test-code",
            name="Flask.dispatch_request",
            depth=depth,
            include_ambiguous=include_ambiguous,
        )
    )
    assert [(r.from_symbol_id, r.to_symbol_id) for r in result.edges] == [
        (repo.full, repo.flask),
        *([(repo.wsgi, repo.full)] if depth == 2 else []),
    ]
    assert not result.truncated
    assert result.dispatch_seeds == []
    assert result.seed_symbols == [repo.flask]
    assert "seed is an override" not in result.note
    assert result.counts_floor
    assert "dispatch_seeds_missing" in result.causes
    assert result.by_hop_counts == {1: 1, **({2: 1} if depth == 2 else {})}
    assert (result.candidate_sites is not None) == include_ambiguous


@pytest.mark.asyncio
async def test_impact_does_not_join_unrelated_same_name_method(collision_repo: _CollisionRepo):
    repo = collision_repo
    _, ids, _, dispatch_rows, added = await impact._symbol_seeds(
        repo, "test-org", "test-code", CodeImpactInput(name="Flask.dispatch_request")
    )
    assert ids == [repo.flask]
    assert dispatch_rows == []
    assert added == 0


@pytest.mark.asyncio
async def test_exact_override_still_adds_its_base(collision_repo: _CollisionRepo):
    repo = collision_repo
    child = "src.views::MethodView::dispatch_request"
    rows = await dispatch._dispatch_seeds(repo, "test-org", "test-code", [_symbol(child, child)])
    assert [(r["symbol_id"], r["via"]) for r in rows] == [(repo.view, "overrides")]
