"""code.map: the rendered repo map stops at its token budget, is cached per
(org, namespace, prefix, budget) for its TTL, degrades to the include-graph
map when the call-graph read fails, and abstains when no card matches."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.api.models_context import CodeMapInput
from server.code.app import context as hc
from server.settings import settings


def _summary(path: str, in_degree: int = 3) -> dict[str, Any]:
    return {"scope_path": path, "in_degree": in_degree, "pagerank": 0.1, "summary": f"{path} card"}


class _Repo:
    def __init__(
        self,
        summaries: list[dict[str, Any]],
        callers: list[dict[str, Any]] | None = None,
        *,
        callers_fail: bool = False,
    ) -> None:
        self.summaries, self.callers, self.callers_fail = summaries, callers or [], callers_fail
        self.summary_reads: list[tuple[str, str, str]] = []

    async def top_summaries(
        self, org: str, ns: str, model: str, *, module_prefix: str, limit: int
    ) -> list[dict[str, Any]]:
        self.summary_reads.append((ns, model, module_prefix))
        return [r for r in self.summaries if r["scope_path"].startswith(module_prefix)]

    async def top_files_by_callers(
        self, org: str, ns: str, *, path_prefix: str, limit: int
    ) -> list[dict[str, Any]]:
        if self.callers_fail:
            raise RuntimeError("code_edges unavailable")
        return self.callers

    async def cards_for_paths(
        self, org: str, ns: str, model: str, paths: list[str]
    ) -> dict[str, str]:
        return {p: f"{p} called card" for p in paths}

    async def get_state(self, org: str, ns: str, key: str) -> str:
        assert key == "git_head"
        return "abc123"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app import fan_in as code_fan_in
    from server.embedding import embedder

    monkeypatch.setattr(embedder, "get_embedder", lambda: SimpleNamespace(model_name="m1"))

    async def _no_hubs(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []

    # the most-called-symbols section is `top_under`'s subject, not this file's
    monkeypatch.setattr(code_fan_in, "top_under", _no_hubs)


def _render(repo: _Repo, prefix: str = "", budget: int = 2000, ns: str = "n") -> str:
    return asyncio.run(hc._render_map(repo, "o", ns, prefix, budget))  # type: ignore[arg-type]


def test_the_map_reads_with_the_embedder_model_and_lists_the_hubs() -> None:
    repo = _Repo([_summary("src/a"), _summary("src/b")])
    text = _render(repo)
    assert text.splitlines() == [
        "# Repo map (include-graph and call-graph hubs; per-merge fresh)",
        "- **src/a** (included by 3) — src/a card",
        "- **src/b** (included by 3) — src/b card",
    ]
    assert repo.summary_reads == [("n", "m1", "")]


def test_the_map_stops_before_the_entry_that_would_pass_the_budget() -> None:
    from server.code.app.index.packs import estimate_tokens

    repo = _Repo([_summary(f"src/module_{i:02d}") for i in range(40)])
    budget = 120
    text = _render(repo, budget=budget)
    assert estimate_tokens(text) <= budget
    entries = text.count("\n- **")
    assert 0 < entries < 40
    # the next entry would have crossed the budget
    nxt = f"\n- **src/module_{entries:02d}** (included by 3) — src/module_{entries:02d} card"
    assert estimate_tokens(text + nxt) > budget


def test_a_repeat_inside_the_ttl_is_served_from_the_cache() -> None:
    repo = _Repo([_summary("src/a")])
    first = _render(repo)
    repo.summaries = [_summary("src/changed")]
    assert _render(repo) == first
    assert len(repo.summary_reads) == 1


@pytest.mark.parametrize(
    "other",
    [
        {"prefix": "src/"},
        {"budget": 999},
        {"ns": "other-ns"},
    ],
)
def test_the_cache_key_separates_prefix_budget_and_namespace(other: dict[str, Any]) -> None:
    repo = _Repo([_summary("src/a")])
    _render(repo)
    _render(repo, **other)
    assert len(repo.summary_reads) == 2


def test_an_entry_past_the_ttl_is_rebuilt(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    repo = _Repo([_summary("src/a")])
    _render(repo)
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + hc._MAP_CACHE.ttl_s + 1)
    repo.summaries = [_summary("src/changed")]
    assert "src/changed" in _render(repo)
    assert len(repo.summary_reads) == 2


def test_a_failing_call_graph_read_leaves_the_include_graph_map() -> None:
    repo = _Repo([_summary("src/a")], callers_fail=True)
    assert "- **src/a** (included by 3) — src/a card" in _render(repo)


def test_call_graph_hubs_alternate_with_include_hubs() -> None:
    repo = _Repo([_summary("src/a")], callers=[{"path": "src/hub.py", "callers": 9}])
    lines = _render(repo).splitlines()[1:]
    assert lines == [
        "- **src/a** (included by 3) — src/a card",
        "- **src/hub.py** (called from 9 files) — src/hub.py called card",
    ]


def _handle(monkeypatch: pytest.MonkeyPatch, repo: _Repo, **kw: Any) -> tuple[Any, list[Any]]:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(hc, "CodeRepo", lambda: repo)
    traces: list[dict[str, Any]] = []
    monkeypatch.setattr(hc, "schedule_code_trace", lambda **t: traces.append(t))
    out = asyncio.run(hc.handle_code_map(CodeMapInput(org="o", namespace="n", **kw)))
    return out, traces


def test_the_handler_returns_the_head_it_read_and_a_non_abstaining_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, (trace,) = _handle(monkeypatch, _Repo([_summary("src/a")]))
    assert out.commit_sha == "abc123" and "src/a" in out.map
    assert out.tokens_used > 0
    assert trace["op"] == "code.map" and trace["abstain"] is False


def test_a_prefix_no_card_matches_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    out, (trace,) = _handle(monkeypatch, _Repo([_summary("src/a")]), module_prefix="docs/")
    assert out.map.splitlines() == [
        "# Repo map (include-graph and call-graph hubs; per-merge fresh)"
    ]
    assert trace["abstain"] is True and trace["returned_ids"] == []
