"""server/memory/app/code_grounding.py — the code-plane grounding of
memory.ask runs its independent reads together and the search fallback
contributes every unit it renders."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import code_grounding as g
from server.settings import Settings, settings


def _settings(**over: Any) -> Settings:
    base = dict(
        memory_ask_code_namespace="code-x",
        memory_ask_code_org="o",
        code_org="o",
        memory_ask_pr_temporal_gate=True,
        memory_ask_code_enrichment=True,
        memory_ask_pr_grounding=True,
        memory_ask_code_staleness=True,
    )
    base.update(over)
    return settings.model_copy(update=base)


def _input() -> SimpleNamespace:
    return SimpleNamespace(org="o", namespace="ns", code_namespace="", code_org="", as_of=None)


def _unit(path: str, line: int, symbol: str) -> SimpleNamespace:
    return SimpleNamespace(
        path=path, start_line=line, symbol=symbol, signature="", description="", recent_prs=[]
    )


def _mem(key: str, excerpt: str = "x") -> SimpleNamespace:
    return SimpleNamespace(memory_key=key, memory_type="semantic", content_excerpt=excerpt)


@pytest.fixture
def code_plane(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.code.app.search.handler as hs
    import server.code.app.symbol as hy
    import server.code.domain.idents as cs

    st = SimpleNamespace(symbol_calls=[], search_calls=0, delay=0.0, span=[])
    monkeypatch.setattr(cs, "embedded_idents", lambda q: {"FooBar", "helper_fn"})

    async def fake_symbol(inp: Any) -> Any:
        st.symbol_calls.append(inp.name)
        st.span.append(("symbol", time.perf_counter()))
        await asyncio.sleep(st.delay)
        return SimpleNamespace(definitions=[_unit("src/foo.py", 10, inp.name)])

    async def fake_search(inp: Any) -> Any:
        st.search_calls += 1
        st.span.append(("search", time.perf_counter()))
        await asyncio.sleep(st.delay)
        return SimpleNamespace(
            abstain=False,
            as_of={"branch": "main"},
            results=[
                _unit("src/a.py", 1, "a"),
                _unit("src/b.py", 2, "b"),
                _unit("src/c.py", 3, "c"),
                _unit("src/d.py", 4, "d"),
            ],
        )

    monkeypatch.setattr(hy, "handle_code_symbol", fake_symbol)
    monkeypatch.setattr(hs, "handle_code_search", fake_search)
    return st


async def test_search_fallback_appends_every_rendered_unit(code_plane, monkeypatch) -> None:
    import server.code.domain.idents as cs

    # No exact-shaped identifier → the search fills all three slots, not just
    # the last one.
    monkeypatch.setattr(cs, "embedded_idents", lambda q: {"helper_fn"})
    hits = await g._inject_code_context("where is helper_fn", _input(), _settings())
    assert [h.memory_key for h in hits] == [
        "code:code-x:src/a.py:1",
        "code:code-x:src/b.py:2",
        "code:code-x:src/c.py:3",
    ]
    assert all(h.memory_type == "code" and h.source == "code_index" for h in hits)


async def test_exact_symbol_leads_and_the_search_fills_concurrently(code_plane) -> None:
    code_plane.delay = 0.2
    t0 = time.perf_counter()
    hits = await g._inject_code_context("what does FooBar do", _input(), _settings())
    wall = time.perf_counter() - t0
    # One symbol lookup and the search: both started before either finished.
    assert code_plane.symbol_calls == ["FooBar"] and code_plane.search_calls == 1
    assert wall < 0.36, wall  # one delay, not two
    kinds = [k for k, _t in code_plane.span]
    assert set(kinds) == {"symbol", "search"}
    # The definition leads, the search fills the two remaining slots.
    assert hits[0].memory_key == "code:code-x:src/foo.py:10"
    assert [h.memory_key for h in hits[1:]] == ["code:code-x:src/a.py:1", "code:code-x:src/b.py:2"]


async def test_no_binding_is_a_noop(code_plane) -> None:
    assert (
        await g._inject_code_context("FooBar", _input(), _settings(memory_ask_code_namespace=""))
        == []
    )
    assert code_plane.symbol_calls == [] and code_plane.search_calls == 0


async def test_apply_runs_two_concurrent_rounds_in_the_fixed_order(monkeypatch) -> None:
    timeline: list[tuple[str, str, float]] = []
    pool = [_mem("m1"), _mem("m2")]
    code_hit = SimpleNamespace(memory_key="code:x", memory_type="code", content_excerpt="c")
    pr_hit = SimpleNamespace(memory_key="code_pr:x:1", memory_type="code", content_excerpt="p")
    note = SimpleNamespace(memory_key="code:staleness", memory_type="code", content_excerpt="s")
    seen_pools: dict[str, list[str]] = {}

    def _step(name: str, result: Any, pool_arg: int | None = None):
        async def _run(*a: Any, **k: Any) -> Any:
            if pool_arg is not None:
                seen_pools[name] = [m.memory_key for m in a[pool_arg]]
            timeline.append((name, "start", time.perf_counter()))
            await asyncio.sleep(0.2)
            timeline.append((name, "end", time.perf_counter()))
            return result(a) if callable(result) else result

        return _run

    monkeypatch.setattr(g, "_gate_future_prs", _step("gate", lambda a: a[0][:1], pool_arg=0))
    monkeypatch.setattr(g, "_inject_code_context", _step("ctx", [code_hit]))
    monkeypatch.setattr(g, "_inject_pr_facts", _step("pr", [pr_hit], pool_arg=1))
    monkeypatch.setattr(g, "_annotate_stale_code_memories", _step("stale", note, pool_arg=0))

    t0 = time.perf_counter()
    out = await g.apply_code_grounding("q", _input(), pool, _settings())
    wall = time.perf_counter() - t0

    # Two rounds of ~200 ms, not four.
    assert wall < 0.64, wall
    starts = {n: t for n, ev, t in timeline if ev == "start"}
    ends = {n: t for n, ev, t in timeline if ev == "end"}
    assert starts["ctx"] < ends["gate"] and starts["gate"] < ends["ctx"]
    assert starts["stale"] < ends["pr"] and starts["pr"] < ends["stale"]
    assert min(starts["pr"], starts["stale"]) >= max(ends["gate"], ends["ctx"])
    # The pool each second-round step saw: the gate's survivors plus the code
    # hits — the staleness pass does not see the PR facts ahead of them.
    assert seen_pools["gate"] == ["m1", "m2"]
    assert seen_pools["pr"] == ["m1", "code:x"] and seen_pools["stale"] == ["m1", "code:x"]
    # Output order is the one memory.ask always used.
    assert [m.memory_key for m in out] == ["code:staleness", "code_pr:x:1", "m1", "code:x"]


async def test_apply_setting_gates_skip_their_step(monkeypatch) -> None:
    calls: list[str] = []

    def _rec(name: str, result: Any):
        async def _run(*a: Any, **k: Any) -> Any:
            calls.append(name)
            return result(a) if callable(result) else result

        return _run

    monkeypatch.setattr(g, "_gate_future_prs", _rec("gate", lambda a: a[0]))
    monkeypatch.setattr(g, "_inject_code_context", _rec("ctx", []))
    monkeypatch.setattr(g, "_inject_pr_facts", _rec("pr", []))
    monkeypatch.setattr(g, "_annotate_stale_code_memories", _rec("stale", None))
    pool = [_mem("m1")]
    out = await g.apply_code_grounding(
        "q",
        _input(),
        pool,
        _settings(memory_ask_code_enrichment=False, memory_ask_code_staleness=False),
    )
    assert out == pool and sorted(calls) == ["gate", "pr"]


async def test_the_grounding_reads_use_their_token_budget_settings(monkeypatch) -> None:
    import server.code.app.search.handler as hs
    import server.code.app.symbol as hy
    import server.code.domain.idents as cs

    budgets: dict[str, int] = {}
    monkeypatch.setattr(cs, "embedded_idents", lambda q: {"FooBar"})

    async def fake_symbol(inp: Any) -> Any:
        budgets["symbol"] = inp.token_budget
        return SimpleNamespace(definitions=[_unit("src/foo.py", 10, inp.name)])

    async def fake_search(inp: Any) -> Any:
        budgets["search"] = inp.token_budget
        return SimpleNamespace(abstain=False, as_of={"branch": "main"}, results=[])

    monkeypatch.setattr(hy, "handle_code_symbol", fake_symbol)
    monkeypatch.setattr(hs, "handle_code_search", fake_search)
    over = {"memory_ask_code_symbol_token_budget": 500, "memory_ask_code_search_token_budget": 900}
    await g._inject_code_context("what does FooBar do", _input(), _settings(**over))
    assert budgets == {"symbol": 500, "search": 900}
