"""memory.ask `token_budget`: the synthesis window is cut to an input budget
in estimated tokens, from the tail, never mid-excerpt (memory.domain.ask_budget)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit
from server.memory.domain.ask_budget import estimate_memory_tokens, trim_to_token_budget


def _hit(key: str, excerpt: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        rank_score=0.8,
    )


def test_estimate_uses_the_rendered_line_and_the_prose_rate() -> None:
    m = _hit("k", "x" * 420)
    # 420 excerpt + 1 key + 32 overhead ≈ 453 chars / 4.2 ≈ 108 tokens
    assert 100 <= estimate_memory_tokens(m) <= 115
    assert estimate_memory_tokens(_hit("k", "")) >= 1
    # The excerpt cap the synthesizer applies is respected in the estimate.
    assert estimate_memory_tokens(_hit("k", "y" * 10_000)) == estimate_memory_tokens(
        _hit("k", "y" * 2000)
    )


def test_no_budget_keeps_everything_and_reports_the_estimate() -> None:
    ms = [_hit("a", "x" * 400), _hit("b", "y" * 400)]
    kept, est, dropped = trim_to_token_budget(ms, None)
    assert kept == ms and dropped == 0
    assert est == sum(estimate_memory_tokens(m) for m in ms)
    assert trim_to_token_budget(ms, 0)[0] == ms


def test_budget_drops_from_the_tail_and_keeps_rank_order() -> None:
    ms = [_hit(f"m{i}", "z" * 400) for i in range(6)]  # ≈ 103 tokens each
    kept, est, dropped = trim_to_token_budget(ms, 320)
    assert [m.memory_key for m in kept] == ["m0", "m1", "m2"]
    assert dropped == 3 and est <= 320


def test_the_top_memory_is_always_kept() -> None:
    ms = [_hit("m0", "z" * 1500), _hit("m1", "q" * 10)]
    kept, est, dropped = trim_to_token_budget(ms, 50)
    assert [m.memory_key for m in kept] == ["m0"]
    assert dropped == 1 and est > 50


def test_empty_pool() -> None:
    assert trim_to_token_budget([], 500) == ([], 0, 0)


# ---- through the handler ------------------------------------------------------


class _FakeLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text="Blue [m0].")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace(llm=_FakeLLM())
    e.search_output = MemorySearchOutput(
        results=[_hit(f"m{i}", f"fact number {i} " + "w" * 380) for i in range(8)],
        embedding_model="fake-embed",
    )

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return e.search_output

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    return e


async def _run(**overrides: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "what is my favorite color",
    }
    base.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**base))


async def test_default_is_unbounded_and_reports_the_estimate(env) -> None:
    out = await _run()
    assert out.context_trimmed == 0
    assert out.context_tokens_est > 0
    # All eight excerpts reached the synthesizer.
    assert env.llm.calls[0][1].count("[m") == 8


async def test_budget_cuts_the_window_the_synthesizer_reads(env) -> None:
    out = await _run(token_budget=300)
    assert out.source == "synthesized"
    assert out.context_trimmed >= 4
    assert out.context_tokens_est <= 300
    user = env.llm.calls[0][1]
    assert "[m0]" in user and "[m7]" not in user
    # The caller's raw results are unaffected by the synthesis budget.
    assert len(out.results) == 8


def test_budget_bounds_are_validated() -> None:
    with pytest.raises(ValidationError):
        MemoryAskInput(org="o", namespace="n", query="q", token_budget=10)
    assert MemoryAskInput(org="o", namespace="n", query="q", token_budget=256).token_budget == 256


async def test_summary_hits_counts_summary_leg_rows_in_the_window(env) -> None:
    """How many source="summary" rows the synthesizer saw."""
    hits = [_hit(f"m{i}", f"fact {i} " + "w" * 400) for i in range(3)]  # ≈103 tokens each
    s1 = _hit("summary:s1", "Session digest about colours.")
    s1.source = "summary"
    env.search_output = MemorySearchOutput(results=hits + [s1], embedding_model="fake-embed")
    out = await _run()
    assert out.summary_hits == 1
    out2 = await _run(token_budget=256)  # keeps two facts; the summary row is past the budget
    assert out2.context_trimmed == 2 and out2.summary_hits == 0
